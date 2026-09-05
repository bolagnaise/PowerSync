"""
Optimization coordinator for PowerSync.

Coordinates data collection and runs the built-in LP battery optimizer
to produce a schedule, which the execution layer then applies.
"""
from __future__ import annotations

import asyncio
import calendar
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from ..flow_power import FlowPowerPlanSnapshot

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.exceptions import ConfigEntryNotReady

from .battery_controller import TRUSTED_FOR_PERSIST
from .battery_efficiency import (
    BatteryEfficiencyLearner,
    ResolvedOptimizerParameters,
)
from .battery_optimizer import (
    RTE_ECONOMIC_HOLD_REASON,
    BatteryOptimizer,
    OptimizerResult,
)
from .cost_neutral import (
    CostNeutralBudget,
    CostNeutralPlan,
    elapsed_settlement_seconds,
)
from .export_policy import (
    battery_export_allowed_slots,
    export_price_allows_battery_export,
    normalize_min_export_price,
)
from .external_energy_resource import (
    ExternalEnergyAllocationResult,
    ExternalEnergyLedgerState,
    ExternalEnergyResourceConfig,
    allocate_external_energy,
    expand_external_energy_sessions,
    reduce_external_energy_ledger,
    resolve_external_energy_sessions,
)
from .schedule_reader import OptimizationSchedule, ScheduleAction
from .executor import ScheduleExecutor, ExecutionStatus, BatteryAction
from .load_estimator import LoadEstimator, SolcastForecaster
from .manual_control import (
    ManualControlProjection,
    build_manual_control_projection,
)
from .price_level_projection import (
    PriceLevelProjection,
    build_price_level_projection,
)
from .solar_forecast_learning import SolarForecastLearner
from .solar_provenance import derive_solar_forecast_provenance
from .solar_export import SolarExportHoldController, resolve_solar_export_adapter
from .ev_coordinator import EVCoordinator, EVConfig, EVChargingMode
from ..const import (
    CONF_GENERIC_CHARGER_POWER_ENTITY,
    DEFAULT_OPTIMIZATION_INTERVAL,
    DEFAULT_OPTIMIZATION_MIN_EXPORT_PRICE,
    DEFAULT_OPTIMIZATION_BACKUP_ENERGY_WH,
    DEFAULT_OPTIMIZATION_BACKUP_ENERGY_MAX_POWER_W,
    DEFAULT_OPTIMIZATION_BACKUP_ENERGY_START,
    DEFAULT_OPTIMIZATION_BACKUP_ENERGY_END,
    normalize_grid_charge_blackout_windows,
)
from ..coordinator import normalize_custom_power_kw
from ..currency import currency_for_entry, currency_metadata
from ..settings_metadata import (
    optimizer_settings_groups,
    optimizer_settings_schema,
)
from ..flow_power_pricing import (
    FlowPowerPricingContext,
    calculate_flow_power_pea,
    resolve_flow_power_pricing_context,
)
from ..covau import (
    COVAU_EXPORT_RULE_ID,
    COVAU_IMPORT_RULE_ID,
    CovaUPlanSnapshot,
    covau_price_series,
    covau_provider_contract,
    covau_quota_rules,
    import_price_c_per_kwh,
)
from ..quota import (
    QuotaLedger,
    QuotaLedgerState,
    import_legacy_settled_state,
    tariff_datetime,
)
from ..tariff_quota import (
    CUSTOM_TARIFF_IMPORT_RULE_ID,
    custom_tariff_import_quota_rule,
    custom_tariff_quota_contract,
    custom_tariff_quota_hash,
)
from ..tariff_time import (
    find_matching_tou_period,
    period_entries,
    tariff_components_for_datetime,
)
from ..zerohero import (
    GLOBIRD_PLAN_NOT_ZEROHERO,
    ZeroHeroConfig,
    settle_zerocharge_imports,
    settle_zerohero_series,
    zerohero_config_from_entry,
    zerohero_credit_status,
    zerohero_is_in_window,
    zerohero_window_end_for,
    zerocharge_is_in_window,
    zerocharge_monthly_cap_kwh,
    zerocharge_period_key,
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

# Solar-export refusals that are a site setting rather than a hardware or
# economic limit, so they are worth telling the user about directly.
_SOLAR_EXPORT_LIMIT_REASONS = frozenset(
    {"export_limit_not_configured", "zero_export_site"}
)
# Distinguishes "never synced this run" from "synced, currently clear", so the
# first pass after a restart still clears a repair persisted by an earlier one.
_SOLAR_EXPORT_NOTICE_UNSYNCED = object()

CUSTOM_BATTERY_SYSTEM = "custom"
CUSTOM_BATTERY_LEVEL_ENTITY = "custom_battery_level_entity"
CUSTOM_BATTERY_POWER_ENTITY = "custom_battery_power_entity"
CUSTOM_GRID_POWER_ENTITY = "custom_grid_power_entity"
CUSTOM_SOLAR_POWER_ENTITY = "custom_solar_power_entity"
CUSTOM_LOAD_POWER_ENTITY = "custom_load_power_entity"

COST_STORE_VERSION = 1
COST_STORE_SAVE_DELAY = 300  # Coalesce writes — flush at most every 5 minutes
SOLAR_FORECAST_LEARNING_STORE_VERSION = 1
SOLAR_FORECAST_LEARNING_STORE_SAVE_DELAY = 300
BATTERY_EFFICIENCY_LEARNING_STORE_VERSION = 1
BATTERY_EFFICIENCY_LEARNING_STORE_SAVE_DELAY = 300
SOLAR_EXPORT_HOLD_STORE_VERSION = 1
EXTERNAL_ENERGY_LEDGER_STORE_VERSION = 1
INITIAL_OPTIMIZATION_DELAY_SECONDS = 90.0
FIXED_OPTIMIZATION_INTERVAL_MINUTES = DEFAULT_OPTIMIZATION_INTERVAL
FLOW_POWER_NEM_TZ = timezone(timedelta(hours=10))
EXPORT_ACTIONS = {"discharge", "export"}
SELF_USE_ACTIONS = {"consume", "self_consumption"}
CHARGE_ACTIONS = {"charge"}
FORCED_ACTIONS = CHARGE_ACTIONS | EXPORT_ACTIONS
BOUNDARY_FRESH_SOLVE_GRACE = timedelta(seconds=30)
OPTIMIZER_FORCE_CHARGE_MIN_COMMITMENT = timedelta(minutes=20)
OPTIMIZER_FORCE_DISCHARGE_MIN_COMMITMENT = timedelta(minutes=20)
TESLA_STALE_GRID_CHARGE_RESTORE_COOLDOWN = timedelta(minutes=5)
SUNGROW_INFERRED_RESTORE_COOLDOWN = timedelta(minutes=5)
GLOBIRD_QUOTA_EXPORT_RULE_ID = "globird_zerohero_bonus_export"
GLOBIRD_QUOTA_IMPORT_RULE_ID = "globird_zerocharge_import"
COST_NEUTRAL_OPTION = "cost_neutral_enabled"


def _positive_finite_number(value: Any) -> float | None:
    """Return a finite positive numeric telemetry value."""
    if isinstance(value, bool):
        return None
    try:
        numeric_value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(numeric_value) or numeric_value <= 0:
        return None
    return numeric_value


def _foxess_auto_power_limits(data: dict[str, Any] | None) -> tuple[int, int] | None:
    """Resolve FoxESS optimizer power limits from live power or voltage telemetry."""
    if not isinstance(data, dict):
        return None

    charge_power_w = _positive_finite_number(data.get("battery_max_charge_power_w"))
    discharge_power_w = _positive_finite_number(
        data.get("battery_max_discharge_power_w")
    )
    charge_current_a = _positive_finite_number(data.get("max_charge_current_a"))
    discharge_current_a = _positive_finite_number(data.get("max_discharge_current_a"))

    derived_charge_w: int | None = None
    derived_discharge_w: int | None = None
    if charge_current_a is not None:
        voltage_v = _positive_finite_number(data.get("battery_voltage_v")) or 300.0
        derived_charge_w = int(charge_current_a * voltage_v)
        derived_discharge_w = int(
            (discharge_current_a or charge_current_a) * voltage_v
        )

    charge_w = int(charge_power_w) if charge_power_w is not None else derived_charge_w
    discharge_w = (
        int(discharge_power_w)
        if discharge_power_w is not None
        else derived_discharge_w
    )
    if charge_w is None or discharge_w is None or charge_w <= 0 or discharge_w <= 0:
        return None
    return charge_w, discharge_w


def _grid_status_is_terminal_off_grid(value: Any) -> bool:
    """Return True only for a confirmed terminal off-grid state."""
    if not isinstance(value, str):
        return False
    return value.strip().lower() in {
        "inactive",
        "islanded",
        "off-grid",
        "systemislandedactive",
    }


def sigenergy_capped_optimizer_limit_w(
    raw_limit_w: Any,
    configured_cap_kw: Any,
) -> int | None:
    """Return an optimizer limit bounded by the durable Sigenergy cap.

    The optimizer setting remains the user's raw planning preference. The
    Sigenergy Controls cap is a separate hardware ceiling and must never raise
    that preference when the cap is increased later.
    """
    try:
        raw_w = None if raw_limit_w is None else max(0, int(float(raw_limit_w)))
    except (TypeError, ValueError):
        raw_w = None

    try:
        cap_w = (
            None
            if configured_cap_kw is None
            else max(0, int(round(float(configured_cap_kw) * 1000)))
        )
    except (TypeError, ValueError):
        cap_w = None

    if cap_w is None:
        return raw_w
    if raw_w is None:
        return cap_w
    return min(raw_w, cap_w)


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
    min_export_price: float = DEFAULT_OPTIMIZATION_MIN_EXPORT_PRICE
    grid_charge_soc_cap: float = 1.0
    grid_charge_blackout_windows: list[dict[str, str]] = field(default_factory=list)
    backup_reserve: float = 0.2
    interval_minutes: int = FIXED_OPTIMIZATION_INTERVAL_MINUTES
    horizon_hours: int = 48
    cost_function: str = "cost"
    profit_max_enabled: bool = False
    cost_neutral_enabled: bool = False
    charge_by_time_enabled: bool = False
    charge_by_time_target_time: str = "17:15"
    charge_by_time_target_soc: float = 1.0
    spread_export_enabled: bool = False
    spread_import_enabled: bool = False
    disable_idle_enabled: bool = False
    battery_efficiency_learning_enabled: bool = True
    auto_apply_reserve_enabled: bool = False
    manual_backup_reserve: float | None = None
    backup_energy_wh: int = DEFAULT_OPTIMIZATION_BACKUP_ENERGY_WH
    backup_energy_max_power_w: int = DEFAULT_OPTIMIZATION_BACKUP_ENERGY_MAX_POWER_W
    backup_energy_start: str = DEFAULT_OPTIMIZATION_BACKUP_ENERGY_START
    backup_energy_end: str = DEFAULT_OPTIMIZATION_BACKUP_ENERGY_END

    def __post_init__(self) -> None:
        self.grid_charge_blackout_windows = normalize_grid_charge_blackout_windows(
            self.grid_charge_blackout_windows
        )
        self.min_export_price = normalize_min_export_price(self.min_export_price)


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
                CONF_OPTIMIZATION_MIN_EXPORT_PRICE,
                CONF_OPTIMIZATION_BACKUP_ENERGY_WH,
                CONF_OPTIMIZATION_BACKUP_ENERGY_MAX_POWER_W,
                CONF_OPTIMIZATION_BACKUP_ENERGY_START,
                CONF_OPTIMIZATION_BACKUP_ENERGY_END,
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
            self._config.min_export_price = normalize_min_export_price(
                self._entry.options.get(
                    CONF_OPTIMIZATION_MIN_EXPORT_PRICE,
                    self._entry.data.get(
                        CONF_OPTIMIZATION_MIN_EXPORT_PRICE,
                        DEFAULT_OPTIMIZATION_MIN_EXPORT_PRICE,
                    ),
                )
            )

            def _entry_nonnegative_int(key: str, default: int) -> int:
                try:
                    return max(
                        0,
                        int(
                            float(
                                self._entry.options.get(
                                    key,
                                    self._entry.data.get(key, default),
                                )
                                or 0
                            )
                        ),
                    )
                except (TypeError, ValueError, OverflowError):
                    return default

            self._config.backup_energy_wh = _entry_nonnegative_int(
                CONF_OPTIMIZATION_BACKUP_ENERGY_WH,
                DEFAULT_OPTIMIZATION_BACKUP_ENERGY_WH,
            )
            self._config.backup_energy_max_power_w = _entry_nonnegative_int(
                CONF_OPTIMIZATION_BACKUP_ENERGY_MAX_POWER_W,
                DEFAULT_OPTIMIZATION_BACKUP_ENERGY_MAX_POWER_W,
            )
            self._config.backup_energy_start = str(
                self._entry.options.get(
                    CONF_OPTIMIZATION_BACKUP_ENERGY_START,
                    self._entry.data.get(
                        CONF_OPTIMIZATION_BACKUP_ENERGY_START,
                        DEFAULT_OPTIMIZATION_BACKUP_ENERGY_START,
                    ),
                )
            )
            self._config.backup_energy_end = str(
                self._entry.options.get(
                    CONF_OPTIMIZATION_BACKUP_ENERGY_END,
                    self._entry.data.get(
                        CONF_OPTIMIZATION_BACKUP_ENERGY_END,
                        DEFAULT_OPTIMIZATION_BACKUP_ENERGY_END,
                    ),
                )
            )

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
        self._last_manual_control_projection: dict[str, Any] = {
            "active": False,
        }

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
        self._warned_dual_ev_overlay = False
        self._pending_ev_charge_plan: Any | None = None
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
        self._last_raw_solar_forecast: list[float] | None = None
        self._last_planned_solar_forecast: list[float] | None = None
        self._last_solar_curtailment_forecast: list[float] | None = None
        self._has_solar_forecast: bool | None = None  # None until the first forecast attempt
        self._last_load_forecast: list[float] | None = None     # kW values
        self._last_import_prices: list[float] | None = None     # $/kWh values (LP-adjusted)
        self._last_export_prices: list[float] | None = None     # $/kWh values (LP-adjusted)
        self._last_display_import_prices: list[float] | None = None  # $/kWh actual tariff
        self._last_display_export_prices: list[float] | None = None  # $/kWh actual tariff
        # Contractual rates before optimizer-only overlays and bounded quota
        # bonuses. Cost Neutral uses these to value each local settlement day.
        self._last_settlement_import_prices: list[float] | None = None
        self._last_settlement_export_prices: list[float] | None = None
        self._last_grid_charge_cap_import_prices: list[float] | None = None  # $/kWh hard cap reference
        self._last_export_boost_allowed_slots: list[bool] = []
        self._last_battery_export_allowed_slots: list[bool] = []
        self._last_priority_export_slots: list[bool] = []
        self._last_price_timestamps: list[datetime] | None = None
        self._pending_price_timestamps: list[datetime] | None = None
        self._last_grid_export_limits_w: list[float | None] | None = None
        self._last_planned_ev_load_forecast_w: list[float] | None = None
        self._last_effective_ev_load_forecast_w: list[float] | None = None
        self._last_ev_charge_by_vehicle_w: dict[str, list[float]] | None = None
        self._last_ev_charge_schedule_updated: datetime | None = None
        self._last_ev_source_by_vehicle_w: dict[str, dict[str, list[float]]] | None = None
        self._last_ev_optimizer_policy: dict[str, dict[str, Any]] | None = None
        self._last_smart_schedule_ev_load_w: list[float] | None = None
        self._last_price_level_expected_ev_load_w: list[float] | None = None
        self._last_price_level_projection: dict[str, Any] | None = None
        self._last_zerohero_bonus_prices: list[float] | None = None
        self._last_zerohero_bonus_cap_kwh: float | None = None
        self._last_zerocharge_bonus_prices: list[float] | None = None
        self._last_zerocharge_bonus_cap_kwh: float | None = None
        self._last_import_bonus_group_ids: list[str | None] | None = None
        self._last_export_bonus_group_ids: list[str | None] | None = None
        self._last_import_bonus_caps_by_group: dict[str, float] | None = None
        self._last_export_bonus_caps_by_group: dict[str, float] | None = None
        self._covau_ledger: QuotaLedger | None = None
        self._custom_tariff_quota_ledger: QuotaLedger | None = None
        self._custom_tariff_quota_hash: str | None = None
        self._globird_quota_state: QuotaLedgerState | None = None
        self._covau_snapshot_cache: CovaUPlanSnapshot | None = None
        self._covau_snapshot_hash: str | None = None
        self._last_covau_config_warning: str | None = None
        self._pending_covau_settlement: dict[str, float] = {
            "import": 0.0,
            "export": 0.0,
        }
        self._pending_custom_tariff_quota_settlement = 0.0
        self._solar_nowcast_derate: float = 1.0
        self._last_solar_nowcast_ratio: float | None = None
        self._last_logged_solar_nowcast_derate: float | None = None
        self._last_solar_nowcast_allowance_kwh: float = 0.0
        self._last_solar_effective_error_margin_kwh: float | None = None
        self._solar_forecast_learner = SolarForecastLearner()
        self._battery_efficiency_learner = BatteryEfficiencyLearner()
        self._last_resolved_optimizer_parameters = (
            ResolvedOptimizerParameters.legacy()
        )

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
        self._cost_neutral_status: dict[str, Any] = {
            "enabled": False,
            "effective_mode": "standard",
            "reason": "disabled",
        }
        # Grid-sourced battery charging only (excludes solar charging and
        # house-load import). Their ratio is the true $/kWh acquisition cost of
        # stored grid energy used by the export-profitability gate.
        self._actual_grid_charge_kwh_today = 0.0
        self._actual_grid_charge_cost_today = 0.0
        self._grid_charge_tracking_known = True
        self._actual_zerohero_import_kwh_today = 0.0
        self._actual_zerohero_export_kwh_today = 0.0
        self._actual_zerohero_bonus_export_kwh_today = 0.0
        self._actual_zerohero_base_export_earnings_today = 0.0
        self._actual_zerohero_bonus_export_earnings_today = 0.0
        self._actual_zerohero_credit_value_today = 0.0
        self._actual_zerocharge_import_kwh_today = 0.0
        self._actual_zerocharge_credit_value_today = 0.0
        # ZeroCharge uses a local calendar-month pool.  The legacy ``*_today``
        # fields above remain as public/serialized aliases for compatibility,
        # while these explicit fields carry the month-to-date state.
        self._actual_zerocharge_period_key: str | None = None
        self._actual_zerocharge_import_kwh_month = 0.0
        self._actual_zerocharge_credit_value_month = 0.0
        self._baseline_zerohero_import_kwh_today = 0.0
        self._baseline_zerohero_bonus_export_kwh_today = 0.0
        self._baseline_zerohero_credit_value_today = 0.0
        self._baseline_zerocharge_import_kwh_today = 0.0
        self._baseline_zerocharge_credit_value_today = 0.0
        self._baseline_zerocharge_period_key: str | None = None
        self._baseline_zerocharge_import_kwh_month = 0.0
        self._baseline_zerocharge_credit_value_month = 0.0
        self._cost_store = Store(
            hass,
            COST_STORE_VERSION,
            f"power_sync.costs.{entry_id}",
        )
        self._solar_forecast_learning_store = Store(
            hass,
            SOLAR_FORECAST_LEARNING_STORE_VERSION,
            f"power_sync.solar_forecast_learning.{entry_id}",
        )
        self._battery_efficiency_learning_store = Store(
            hass,
            BATTERY_EFFICIENCY_LEARNING_STORE_VERSION,
            f"power_sync.battery_efficiency_learning.{entry_id}",
        )
        self._solar_export_hold = SolarExportHoldController(
            Store(
                hass,
                SOLAR_EXPORT_HOLD_STORE_VERSION,
                f"power_sync.solar_export_hold.{entry_id}",
            ),
            resolve_solar_export_adapter(battery_system, energy_coordinator),
        )
        self._external_energy_ledger_store = Store(
            hass,
            EXTERNAL_ENERGY_LEDGER_STORE_VERSION,
            f"power_sync.external_energy.{entry_id}",
        )
        self._external_energy_ledger = ExternalEnergyLedgerState()
        self._external_energy_ledger_loaded = False
        self._last_external_energy_allocation = ExternalEnergyAllocationResult(
            reason="disabled"
        )
        self._last_profit_max_solar_export_slots: list[bool] = []
        self._solar_export_capability_status: dict[str, Any] = {
            "supported": False,
            "reason": "not_evaluated",
        }
        self._last_solar_export_upstream_outage: tuple[str, str, str] | None = None

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
        # The billing interval that caused the last non-AEMO price-triggered
        # solve.  A new settled current interval is material even when it
        # arrives shortly after the preceding solve, whereas two callbacks for
        # the same interval are ordinarily duplicate usage/spot updates.
        self._last_price_triggered_generation: tuple[str, ...] | None = None

        # Track last executed action for mode transitions and status reporting.
        self._last_executed_action: str | None = None
        self._last_executed_planned_action: str | None = None
        # A cached boundary action owns its wall-clock slot. A slow periodic
        # solve may publish a newer plan during that slot, but it must not
        # reverse an accepted non-force boundary action into a new force mode
        # until the next boundary. This is transient execution metadata only;
        # the optimizer schedule remains the source of truth for planning.
        self._boundary_execution: dict[str, Any] | None = None
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
        self._idle_no_discharge_active = False
        self._scheduled_ev_no_discharge_active = False
        # User's real backup reserve captured ONCE on startup, before any
        # IDLE modifies it. Used as the authoritative restore value.
        self._startup_backup_reserve: int | None = None
        # Last reserve target successfully written by optimizer self-consumption
        # in this runtime. Used to distinguish our own SOC clamp from a manual
        # or stale hardware reserve on a later cycle.
        self._last_optimizer_self_consumption_reserve_target: int | None = None
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
        from ..const import CONF_MONITORING_MODE, DOMAIN

        entry_data = self.hass.data.get(DOMAIN, {}).get(self.entry_id, {})
        if isinstance(entry_data, dict) and entry_data.get(
            "_monitoring_handoff_active", False
        ):
            return True
        if not self._entry:
            return False

        return bool(
            self._entry.options.get(
                CONF_MONITORING_MODE,
                self._entry.data.get(CONF_MONITORING_MODE, False),
            )
        )

    async def _call_optimizer_energy_control(
        self, method_name: str, *args: Any
    ) -> Any:
        """Call a direct energy control method for an optimizer action."""
        method = getattr(self.energy_coordinator, method_name)
        if self.battery_system == "solaredge":
            return await method(*args, automatic=True)
        return await method(*args)

    async def _restore_pre_idle_backup_reserve(
        self, battery, context: str = "", bypass_monitoring: bool = False
    ) -> bool:
        """Restore pre-IDLE backup reserve with retry. Only clears on success."""
        if self._pre_idle_backup_reserve is None:
            self._idle_hold_reserve = None
            return True
        if not hasattr(battery, "set_backup_reserve"):
            self._pre_idle_backup_reserve = None
            self._idle_hold_reserve = None
            return True
        if not bypass_monitoring and self._monitoring_mode_active():
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
                ok = await self._call_optimizer_energy_control(
                    "restore_work_mode_from_idle"
                )
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

        # Sungrow IDLE is implemented with a temporary zero-discharge cap.
        # Raising the separate off-grid backup reserve is redundant and can
        # strand an elevated off-grid reserve when firmware accepts register
        # 13099 writes but does not expose a readable value to restore.
        if self.battery_system == "sungrow":
            if not self.energy_coordinator or not hasattr(
                self.energy_coordinator, "set_backup_mode"
            ):
                _LOGGER.warning(
                    "Optimizer: Sungrow IDLE discharge-cap control is unavailable"
                )
                return False
            if await self.energy_coordinator.set_backup_mode() is False:
                return False
            _LOGGER.info(
                "Optimizer: IDLE — Sungrow discharge-cap hold at %d%% SOC "
                "(backup reserve unchanged)",
                soc_pct,
            )
            return True

        # Fronius IDLE already holds SOC with temporary 0 W PV-charge and
        # discharge limits. Raising the persistent minimum-SOC entity as well
        # is redundant and can strand the inverter at the current SOC after
        # Auto mode is restored.
        if self.battery_system == "fronius_reserva":
            if not self.energy_coordinator or not hasattr(
                self.energy_coordinator, "set_backup_mode"
            ):
                _LOGGER.warning(
                    "Optimizer: Fronius IDLE power-limit control is unavailable"
                )
                return False
            if await self.energy_coordinator.set_backup_mode() is False:
                return False
            _LOGGER.info(
                "Optimizer: IDLE — Fronius power-limit hold at %d%% SOC "
                "(minimum SOC unchanged)",
                soc_pct,
            )
            return True

        # SAJ H2 holds SOC through its passive AppMode control. It has no
        # writable backup reserve, and the queued upstream switch transition
        # can explicitly fail verification. Preserve that result so the
        # executor retries instead of reporting an IDLE hold that never engaged.
        if self.battery_system == "saj_h2":
            if not self.energy_coordinator or not hasattr(
                self.energy_coordinator, "set_backup_mode"
            ):
                _LOGGER.warning(
                    "Optimizer: SAJ H2 IDLE passive control is unavailable"
                )
                return False
            if await self.energy_coordinator.set_backup_mode() is False:
                return False
            _LOGGER.info(
                "Optimizer: IDLE — SAJ H2 passive hold at %d%% SOC "
                "(backup reserve unchanged)",
                soc_pct,
            )
            self._idle_hold_reserve = None
            return True

        if (
            preserve_charge
            and self.energy_coordinator
            and hasattr(self.energy_coordinator, "set_no_discharge_mode")
        ):
            if getattr(self, "_idle_no_discharge_active", False):
                return True
            if await self.energy_coordinator.set_no_discharge_mode() is False:
                return False
            self._idle_no_discharge_active = True
            self._idle_hold_reserve = None
            _LOGGER.info(
                "Optimizer: IDLE — discharge blocked while charging remains allowed"
            )
            return True

        if self.battery_system == "goodwe":
            if preserve_charge:
                if not self.energy_coordinator or not hasattr(
                    self.energy_coordinator, "set_backup_mode"
                ):
                    _LOGGER.warning(
                        "Optimizer: GoodWe IDLE conserve control is unavailable"
                    )
                    return False
                if await self.energy_coordinator.set_backup_mode() is False:
                    return False
                _LOGGER.info(
                    "Optimizer: IDLE — GoodWe conserve hold at %d%% SOC "
                    "(DOD unchanged)",
                    soc_pct,
                )
            else:
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
            backup_mode_result = await self._call_optimizer_energy_control(
                "set_backup_mode"
            )
            if self.battery_system == "solaredge" and backup_mode_result is False:
                _LOGGER.warning(
                    "Optimizer: SolarEdge IDLE hold mode was not confirmed; "
                    "keeping the previous action marker"
                )
                return False
            if hasattr(battery, "set_backup_reserve") and self.battery_system != "sigenergy":
                self._idle_reserve_adjustment = True
                try:
                    reserve_result = await battery.set_backup_reserve(
                        non_tesla_hold_pct
                    )
                finally:
                    self._idle_reserve_adjustment = False
                if self.battery_system == "solaredge" and reserve_result is False:
                    _LOGGER.warning(
                        "Optimizer: SolarEdge IDLE backup reserve was not confirmed; "
                        "keeping the previous action marker"
                    )
                    return False
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
            mode_result = await battery.set_self_consumption_mode()
            if self.battery_system == "solaredge" and mode_result is False:
                return False
            _LOGGER.info("Optimizer: IDLE — self-consumption (no set_backup_reserve)")
            self._idle_hold_reserve = None
            return True
        if hasattr(battery, "restore_normal"):
            restore_result = await battery.restore_normal()
            if self.battery_system == "solaredge" and restore_result is False:
                return False
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
    def cost_neutral_enabled(self) -> bool:
        """Return whether the local-day Cost Neutral mode is active."""
        return bool(self._config.cost_neutral_enabled)

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
    def battery_efficiency_learning_enabled(self) -> bool:
        """Return whether learned physical efficiency is applied to solves."""
        return bool(
            getattr(self._config, "battery_efficiency_learning_enabled", True)
        )

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
        enabled = bool(enabled)
        cost_neutral_changed = enabled and self._config.cost_neutral_enabled
        if self._config.profit_max_enabled == enabled and not cost_neutral_changed:
            return False
        self._config.profit_max_enabled = enabled
        if cost_neutral_changed:
            self._config.cost_neutral_enabled = False
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
            if cost_neutral_changed:
                async_dispatcher_send(
                    self.hass,
                    f"{DOMAIN}_{self.entry_id}_cost_neutral",
                    False,
                )
        if self._entry:
            from ..const import CONF_PROFIT_MAX_ENABLED, DOMAIN
            new_options = dict(self._entry.options)
            new_options[CONF_PROFIT_MAX_ENABLED] = enabled
            if cost_neutral_changed:
                new_options[COST_NEUTRAL_OPTION] = False
            self.hass.data.setdefault(DOMAIN, {}).setdefault(self.entry_id, {})["_skip_reload"] = True
            self.hass.config_entries.async_update_entry(self._entry, options=new_options)
        return True

    def set_cost_neutral_enabled(self, enabled: bool) -> bool:
        """Enable Cost Neutral and atomically disable Profit Max."""
        enabled = bool(enabled)
        profit_changed = enabled and self._config.profit_max_enabled
        if self._config.cost_neutral_enabled == enabled and not profit_changed:
            return False
        self._config.cost_neutral_enabled = enabled
        if profit_changed:
            self._config.profit_max_enabled = False
            if self._optimizer:
                self._optimizer.terminal_weight = self._profit_max_terminal_weight()
        if self._load_estimator:
            self._load_estimator.invalidate_cache()
        _LOGGER.info("Cost Neutral mode %s", "ENABLED" if enabled else "DISABLED")
        if self.hass and self.entry_id:
            from homeassistant.helpers.dispatcher import async_dispatcher_send
            from ..const import DOMAIN

            async_dispatcher_send(
                self.hass,
                f"{DOMAIN}_{self.entry_id}_cost_neutral",
                enabled,
            )
            if profit_changed:
                async_dispatcher_send(
                    self.hass,
                    f"{DOMAIN}_{self.entry_id}_profit_max_mode",
                    False,
                )
        if self._entry:
            from ..const import CONF_PROFIT_MAX_ENABLED, DOMAIN

            new_options = dict(self._entry.options)
            new_options[COST_NEUTRAL_OPTION] = enabled
            if profit_changed:
                new_options[CONF_PROFIT_MAX_ENABLED] = False
            self.hass.data.setdefault(DOMAIN, {}).setdefault(self.entry_id, {})[
                "_skip_reload"
            ] = True
            self.hass.config_entries.async_update_entry(
                self._entry, options=new_options
            )
        return True

    def set_charge_by_time_enabled(
        self,
        enabled: bool,
        *,
        publish: bool = True,
    ) -> bool:
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
        if publish:
            self.async_set_updated_data(self.get_api_data())
        return True

    def set_disable_idle_enabled(self, enabled: bool) -> bool:
        """Enable or disable no-idle mode."""
        enabled = bool(enabled)
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
            persisted_before = (dict(new_data), dict(new_options))
            new_data[CONF_OPTIMIZATION_DISABLE_IDLE] = enabled
            new_options[CONF_OPTIMIZATION_DISABLE_IDLE] = enabled
            if (new_data, new_options) != persisted_before:
                self.hass.data.setdefault(DOMAIN, {}).setdefault(
                    self.entry_id, {}
                )["_skip_reload"] = True
                self.hass.config_entries.async_update_entry(
                    self._entry,
                    data=new_data,
                    options=new_options,
                )
        return True

    def set_battery_efficiency_learning_enabled(self, enabled: bool) -> bool:
        """Enable or disable applying learned battery efficiency."""
        enabled = bool(enabled)
        if self._config.battery_efficiency_learning_enabled == enabled:
            return False
        self._config.battery_efficiency_learning_enabled = enabled
        _LOGGER.info(
            "Battery efficiency learning application %s",
            "ENABLED" if enabled else "DISABLED",
        )
        if self._entry:
            from ..const import (
                CONF_OPTIMIZATION_BATTERY_EFFICIENCY_LEARNING,
                DOMAIN,
            )

            new_data = dict(self._entry.data)
            new_options = dict(self._entry.options)
            new_data[CONF_OPTIMIZATION_BATTERY_EFFICIENCY_LEARNING] = enabled
            new_options[CONF_OPTIMIZATION_BATTERY_EFFICIENCY_LEARNING] = enabled
            self.hass.data.setdefault(DOMAIN, {}).setdefault(
                self.entry_id, {}
            )["_skip_reload"] = True
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
        _persisted_before = (dict(new_data), dict(new_options))
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

        if (new_data, new_options) != _persisted_before:
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
        manual_reserve = self._reserve_ratio(
            getattr(self, "_manual_backup_reserve", None),
            None,
        )
        manual_percent = (
            manual_reserve * 100.0 if manual_reserve is not None else 0.0
        )
        target_percent = max(
            float(hardware_percent),
            manual_percent,
            min(100.0, suggested_percent),
        )
        return max(0.0, min(1.0, target_percent / 100.0))

    def _force_discharge_reserve_floor(self, action: Any | None = None) -> float:
        """Return the software floor used before force discharge/export commands."""
        # Auto-Apply may update the configured optimizer reserve, but there is
        # no second hidden home-load bridge floor. Runtime export protection
        # therefore uses the same active reserve the solver modeled.
        floor = self._reserve_ratio(self._config.backup_reserve, 0.0) or 0.0
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
                scan_action = actions[scan_idx]
                scan_discharge_w = float(
                    getattr(scan_action, "battery_discharge_w", None)
                    or getattr(scan_action, "power_w", 0.0)
                    or 0.0
                )
                if (
                    getattr(scan_action, "action", None) in EXPORT_ACTIONS
                    and scan_discharge_w > 100.0
                ):
                    # Another real export run starts here -- it covers its
                    # own home load, so stop bridging without treating this
                    # as a charge opportunity for run 1.
                    break
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

    def _set_forecast_bridge_reserve_recommendation(
        self,
        result: OptimizerResult,
        reference_export_windows: list[tuple[int, int, str]],
        solar_forecast: list[float] | None,
        load_forecast: list[float] | None,
    ) -> None:
        """Set a seed-independent reserve that preserves the manual buffer.

        The optimizer reserve only constrains intentional export; natural home
        consumption can continue to the hardware reserve. Auto-Apply therefore
        has to leave enough energy at the end of an eligible export window to
        cover forecast net home load until the next charge opportunity, plus
        the user's saved manual buffer.

        Each reference window is frozen from the manual-baseline schedule before
        Spread Export rewrites it. Bounded priority windows extend that episode;
        generic export permission does not, because a flat positive tariff may
        leave export permitted for the entire forecast horizon.
        """
        if not self.auto_apply_reserve_enabled:
            return

        schedule = getattr(result, "schedule", None)
        actions = list(getattr(schedule, "actions", None) or [])
        slot_count = len(actions)
        if slot_count <= 0:
            return

        manual_reserve = self._reserve_ratio(
            getattr(self, "_manual_backup_reserve", None),
            self._config.backup_reserve,
        )
        if manual_reserve is None:
            return
        baseline = max(self._hardware_reserve_ratio(), manual_reserve)

        capacity_kwh = max(
            0.0,
            float(getattr(self._config, "battery_capacity_wh", 0) or 0)
            / 1000.0,
        )
        efficiency = max(
            0.001,
            float(
                getattr(getattr(self, "_optimizer", None), "efficiency", 0.95)
                or 0.95
            ),
        )
        interval_hours = max(
            1,
            int(getattr(self._config, "interval_minutes", 5) or 5),
        ) / 60.0

        recommendation = dict(
            getattr(result, "reserve_recommendation", {}) or {}
        )
        best_target = baseline
        best_meta: dict[str, Any] = {}

        def _forecast_kw(values: list[float] | None, index: int) -> float:
            if not values or index >= len(values):
                return 0.0
            try:
                return max(0.0, float(values[index]))
            except (TypeError, ValueError):
                return 0.0

        current_local_now = dt_util.now()

        def _is_current_local_day(window_start: int) -> bool:
            """Keep the scalar reserve recommendation scoped to today's episode."""
            window_timestamp = getattr(actions[window_start], "timestamp", None)
            if (
                not isinstance(window_timestamp, datetime)
                or window_timestamp.tzinfo is None
                or not isinstance(current_local_now, datetime)
                or current_local_now.tzinfo is None
            ):
                # An unknown calendar day must not raise today's scalar floor.
                return False
            try:
                return (
                    dt_util.as_local(window_timestamp).date()
                    == dt_util.as_local(current_local_now).date()
                )
            except (TypeError, ValueError, OverflowError):
                return False

        for raw_start, raw_end, boundary_source in reference_export_windows:
            window_start = int(raw_start)
            if window_start < 0 or window_start >= slot_count:
                continue
            if not _is_current_local_day(window_start):
                continue
            window_end = max(window_start + 1, min(slot_count, int(raw_end)))

            next_charge_idx: int | None = None
            next_charge_reason: str | None = None
            for scan_idx in range(window_end, slot_count):
                action = actions[scan_idx]
                if float(getattr(action, "battery_charge_w", 0.0) or 0.0) > 100.0:
                    next_charge_idx = scan_idx
                    next_charge_reason = (
                        "scheduled_grid_charge"
                        if getattr(action, "action", None) == "charge"
                        else "forecast_solar_surplus"
                    )
                    break
                if (
                    _forecast_kw(solar_forecast, scan_idx)
                    - _forecast_kw(load_forecast, scan_idx)
                    > 0.1
                ):
                    next_charge_idx = scan_idx
                    next_charge_reason = "forecast_solar_surplus"
                    break

            bridge_end = (
                next_charge_idx if next_charge_idx is not None else slot_count
            )
            bridge_kwh = sum(
                max(
                    0.0,
                    _forecast_kw(load_forecast, bridge_idx)
                    - _forecast_kw(solar_forecast, bridge_idx),
                )
                * interval_hours
                for bridge_idx in range(window_end, bridge_end)
            )
            bridge_soc = (
                bridge_kwh / max(capacity_kwh * efficiency, 0.001)
                if capacity_kwh > 0
                else 0.0
            )
            target = max(baseline, min(1.0, baseline + bridge_soc))
            if target <= best_target + 0.0001:
                continue

            best_target = target
            protects_until_idx = (
                next_charge_idx
                if next_charge_idx is not None
                else slot_count - 1
            )
            best_meta = {
                "forecast_bridge_kwh": round(bridge_kwh, 3),
                "forecast_bridge_reserve_percent": int(
                    math.ceil(bridge_soc * 100 - 1e-9)
                ),
                "forecast_bridge_export_window_start": actions[
                    window_start
                ].timestamp.isoformat(),
                "forecast_bridge_export_window_end": actions[
                    window_end - 1
                ].timestamp.isoformat(),
                "forecast_bridge_boundary_source": boundary_source,
                "protects_until": actions[protects_until_idx].timestamp.isoformat(),
                "next_charge_reason": (
                    next_charge_reason or "no_charge_in_horizon"
                ),
            }

        recommendation.update(best_meta)
        recommendation["manual_optimizer_reserve_percent"] = int(
            round(manual_reserve * 100)
        )
        recommendation["suggested_optimizer_reserve_percent"] = int(
            math.ceil(best_target * 100 - 1e-9)
        )
        recommendation["needs_optimizer_reserve_raise"] = (
            best_target > baseline + 0.0001
        )
        result.reserve_recommendation = recommendation

    def _reference_export_bridge_windows(
        self,
        schedule: OptimizationSchedule | None,
        export_allowed: list[bool],
        priority_export_slots: list[bool],
    ) -> list[tuple[int, int, str]]:
        """Freeze manual-baseline export episodes for reserve bridge planning."""
        actions = list(getattr(schedule, "actions", None) or [])
        if not actions:
            return []

        slot_count = len(actions)
        allowed = [bool(value) for value in export_allowed[:slot_count]]
        priority = [bool(value) for value in priority_export_slots[:slot_count]]
        allowed.extend([False] * (slot_count - len(allowed)))
        priority.extend([False] * (slot_count - len(priority)))
        effective_priority = [
            allowed[idx] and priority[idx] for idx in range(slot_count)
        ]

        priority_windows: list[tuple[int, int]] = []
        idx = 0
        while idx < slot_count:
            if not effective_priority[idx]:
                idx += 1
                continue
            start = idx
            while idx < slot_count and effective_priority[idx]:
                idx += 1
            priority_windows.append((start, idx))

        def _is_real_export(action: Any) -> bool:
            if getattr(action, "action", None) not in EXPORT_ACTIONS:
                return False
            discharge_w = float(
                getattr(action, "battery_discharge_w", None)
                or getattr(action, "power_w", 0.0)
                or 0.0
            )
            return discharge_w > 100.0

        windows: list[tuple[int, int, str]] = []
        idx = 0
        while idx < slot_count:
            if not _is_real_export(actions[idx]):
                idx += 1
                continue
            run_start = idx
            while idx < slot_count and _is_real_export(actions[idx]):
                idx += 1
            run_end = idx
            boundary_end = run_end
            boundary_source = "manual_baseline_export_episode"
            for priority_start, priority_end in priority_windows:
                if priority_start < run_end and priority_end > run_start:
                    boundary_end = max(boundary_end, priority_end)
                    boundary_source = "bounded_priority_window"
            windows.append((run_start, boundary_end, boundary_source))
        return windows

    def _targetless_export_safe_duration(
        self,
        action: Any,
        soc_now: float | None,
        reserve: float,
        requested_minutes: int,
    ) -> tuple[int, float | None]:
        """Return a reserve-safe full-power duration for targetless export."""
        if self._supports_target_export_power():
            return max(0, int(requested_minutes)), None
        try:
            capacity_wh = float(
                getattr(self._config, "battery_capacity_wh", 0.0) or 0.0
            )
            command_w = float(self._export_command_power_w(action))
            efficiency = float(
                getattr(
                    getattr(self, "_optimizer", None),
                    "efficiency",
                    0.92,
                )
                or 0.92
            )
            requested = max(0, int(requested_minutes))
        except (TypeError, ValueError, OverflowError):
            return 0, None
        if (
            soc_now is None
            or capacity_wh <= 0
            or command_w <= 0
            or not 0 < efficiency <= 1
        ):
            return 0, None

        headroom_wh = max(0.0, (soc_now - reserve) * capacity_wh)
        safe_minutes_raw = headroom_wh * efficiency / command_w * 60.0
        safe_minutes = max(0, math.floor(safe_minutes_raw + 1e-6))
        hardware_projected_soc = soc_now - (
            command_w * requested / 60.0 / efficiency / capacity_wh
        )
        return min(requested, safe_minutes), hardware_projected_soc

    def _targetless_charge_safe_duration(
        self,
        action: Any,
        soc_now: float | None,
        charge_cap: float,
        requested_minutes: int,
    ) -> tuple[int, float | None]:
        """Return a cap-safe full-power duration for targetless charging."""
        if self._supports_target_charge_power():
            return max(0, int(requested_minutes)), None
        try:
            capacity_wh = float(
                getattr(self._config, "battery_capacity_wh", 0.0) or 0.0
            )
            command_w = float(
                getattr(self._config, "max_charge_w", 0.0) or 0.0
            )
            requested_w = max(
                0.0,
                float(getattr(action, "power_w", 0.0) or 0.0),
            )
            efficiency = float(
                getattr(
                    getattr(self, "_optimizer", None),
                    "efficiency",
                    0.92,
                )
                or 0.92
            )
            requested = max(0, int(requested_minutes))
        except (TypeError, ValueError, OverflowError):
            return 0, None
        if command_w <= 0 or requested_w <= 0 or not 0 < efficiency <= 1:
            return 0, None

        # The default 100% cap intentionally preserves the historical command
        # contract. Hardware/BMS tapering owns the physical full-SOC limit.
        if charge_cap >= 0.999:
            return requested, None

        # Preserve the scheduled energy by translating a stale fractional
        # target into whole minutes at the hardware's fixed command rate.
        energy_safe_minutes = max(
            0,
            math.floor(requested_w * requested / command_w + 1e-6),
        )
        hardware_projected_soc: float | None = None
        cap_safe_minutes = requested
        if charge_cap < 0.999:
            if soc_now is None or capacity_wh <= 0:
                return 0, None
            headroom_wh = max(0.0, (charge_cap - soc_now) * capacity_wh)
            cap_safe_minutes = max(
                0,
                math.floor(
                    headroom_wh / (command_w * efficiency) * 60.0 + 1e-6
                ),
            )
            hardware_projected_soc = soc_now + (
                command_w * requested / 60.0 * efficiency / capacity_wh
            )

        return min(requested, energy_safe_minutes, cap_safe_minutes), (
            hardware_projected_soc
        )

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
        if not self._supports_target_export_power():
            interval_minutes = max(
                1,
                int(getattr(self._config, "interval_minutes", 5) or 5),
            )
            safe_minutes, hardware_projected_soc = (
                self._targetless_export_safe_duration(
                    action,
                    soc_now,
                    reserve,
                    interval_minutes,
                )
            )
            if safe_minutes < interval_minutes:
                return True, (
                    min(projected_soc, hardware_projected_soc)
                    if projected_soc is not None
                    and hardware_projected_soc is not None
                    else (
                        projected_soc
                        if projected_soc is not None
                        else hardware_projected_soc
                    )
                )
        # A modeled export slot may legitimately finish exactly on the active
        # reserve when the battery can honor that slot's requested power.
        # Targetless force modes run at their configured maximum, so the
        # hardware projection above must also remain at or above the floor.
        if projected_soc is not None and projected_soc < reserve - 0.0001:
            return True, projected_soc
        return False, projected_soc

    def _apply_auto_reserve_recommendation(
        self,
        result: OptimizerResult,
    ) -> bool:
        """Apply one forecast optimizer reserve update after a solve."""
        if not bool(getattr(self, "_auto_apply_reserve_enabled", False)):
            return False
        # Never act on an infeasible safety fallback. It deliberately returns
        # no economic reserve recommendation, so Auto-Apply must wait for the
        # next successful solve rather than ratcheting from a degraded plan.
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
        return normalize_custom_power_kw(value, unit)

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
        if not math.isfinite(value):
            if self._last_custom_energy_warning != entity_id:
                _LOGGER.warning(
                    "Custom battery telemetry entity %s is not finite",
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
        """Return the same canonical non-EV Home Load used by HA sensors."""
        custom_data = self._read_custom_energy_data()
        data = custom_data or getattr(self.energy_coordinator, "data", None)
        if not isinstance(data, dict):
            return None

        from ..ev_load import (
            EvLoadObservation,
            EvLoadQuality,
            EvMeasurementKind,
            ObservedEvLoadSnapshot,
            aggregate_ev_load,
            normalize_energy_data,
            reconcile_ev_load_snapshot,
        )

        now = dt_util.utcnow()
        entry_data = self.hass.data.get("power_sync", {}).get(
            getattr(self._entry, "entry_id", ""), {}
        )
        ev_snapshot = entry_data.get("observed_ev_load_snapshot")
        physical_fallbacks = data.get("ev_power_fallback_by_physical_key")
        if physical_fallbacks:
            ev_snapshot = reconcile_ev_load_snapshot(
                (
                    ev_snapshot
                    if isinstance(ev_snapshot, ObservedEvLoadSnapshot)
                    else None
                ),
                at=now,
                fallback_power_kw=data.get("ev_power", 0.0),
                fallback_by_physical_key=physical_fallbacks,
                fallback_observed_at=data.get("last_update"),
            )
        elif isinstance(ev_snapshot, ObservedEvLoadSnapshot):
            age = now - ev_snapshot.observed_at
            if not (timedelta(0) <= age <= timedelta(seconds=90)):
                ev_snapshot = ObservedEvLoadSnapshot(
                    power_kw=0.0,
                    components=(),
                    observed_at=now,
                    quality=EvLoadQuality.INCOMPLETE,
                    unavailable_active_keys=tuple(
                        item.physical_load_key for item in ev_snapshot.components
                    ) or ev_snapshot.unavailable_active_keys,
                )
        else:
            embedded_ev = data.get("ev_power")
            if embedded_ev is not None:
                ev_snapshot = aggregate_ev_load(
                    [
                        EvLoadObservation(
                            physical_load_key="coordinator:embedded_ev",
                            source_key="energy_coordinator",
                            power_kw=embedded_ev,
                            observed_at=now,
                            active=abs(float(embedded_ev or 0.0)) > 0.05,
                            measurement_kind=EvMeasurementKind.INTEGRATED_CHARGER,
                            supports_bidirectional_power=(
                                getattr(self, "battery_system", "") == "sigenergy"
                            ),
                        )
                    ],
                    at=now,
                )
            else:
                ev_snapshot = ObservedEvLoadSnapshot(
                    power_kw=0.0,
                    components=(),
                    observed_at=now,
                    quality=EvLoadQuality.COMPLETE,
                )
        return normalize_energy_data(
            data,
            battery_system=getattr(self, "battery_system", ""),
            ev_load=ev_snapshot,
            at=now,
        )

    def _energy_telemetry_ready(self) -> bool:
        """Return False only when a coordinator explicitly reports stale telemetry."""
        checker = getattr(self.energy_coordinator, "startup_control_ready", None)
        if callable(checker):
            return bool(checker())
        data = self._get_energy_data()
        return not (
            isinstance(data, dict)
            and data.get("telemetry_ready") is False
        )

    def _energy_uses_native_battery_integration(self) -> bool:
        """Return whether battery control is delegated to another HA integration."""
        coordinator = self.energy_coordinator
        if not getattr(coordinator, "uses_native_battery_integration", False):
            return False
        enabled = getattr(coordinator, "_native_integration_enabled", None)
        return bool(enabled()) if callable(enabled) else True

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

        # Controls used a private key before hardware reserve gained one
        # canonical owner. Prefer that newer user choice while old entries are
        # being migrated by the next physical reserve write.
        persisted_user_reserve = self._reserve_percent(
            self._entry.options.get("_user_backup_reserve")
        )
        if persisted_user_reserve is not None and (
            persisted_user_reserve > 0 or self.battery_system != "tesla"
        ):
            return persisted_user_reserve, "persisted user backup reserve"

        hw_reserve = self._reserve_percent(
            self._entry.data.get(
                CONF_HARDWARE_BACKUP_RESERVE,
                self._entry.options.get(CONF_HARDWARE_BACKUP_RESERVE),
            )
        )
        if hw_reserve is not None:
            return hw_reserve, "hardware backup reserve config"

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

    async def resolve_restore_target(self) -> int | None:
        """Resolve a trustworthy backup-reserve value to restore after a
        temporary window (e.g. ``schedule_max_backup``) ends.

        Ordering is load-bearing (S3/PW-4): the optimizer's
        ``_startup_backup_reserve`` and the persisted ``_user_backup_reserve``
        are provenance-clean and preferred over even a trusted live read,
        because a LIVE tag certifies freshness, not overlay integrity -- a
        PW-3/PW-4 offset-corrupted local snapshot is still "fresh", and
        this value feeds a ``source="user"`` restore that would clobber
        the persisted user reserve with it. A trusted (LIVE/CLOUD_FRESH)
        reading is the final fallback; an untrusted (CLOUD_STALE/ENTITY)
        reading is never used, and a legacy battery with no
        ``read_backup_reserve`` accessor is not trusted via a raw
        ``get_backup_reserve()`` read either. Returns ``None`` only if
        nothing usable exists.
        """
        startup = self._reserve_percent(getattr(self, "_startup_backup_reserve", None))
        if startup is not None:
            return startup

        persisted = (
            self._reserve_percent(self._entry.options.get("_user_backup_reserve"))
            if self._entry
            else None
        )
        if persisted is not None:
            return persisted

        battery = self._executor.battery_controller if self._executor else None
        if battery is not None and hasattr(battery, "read_backup_reserve"):
            try:
                reading = await battery.read_backup_reserve()
            except Exception as exc:
                _LOGGER.debug(
                    "resolve_restore_target: read_backup_reserve failed: %s", exc
                )
            else:
                if reading.trust in TRUSTED_FOR_PERSIST:
                    return self._reserve_percent(reading.percent)

        return None

    def _provider_key(self) -> str:
        """Return the configured electricity provider key."""
        if not self._entry:
            return ""
        from ..const import CONF_ELECTRICITY_PROVIDER

        return self._entry.options.get(
            CONF_ELECTRICITY_PROVIDER,
            self._entry.data.get(CONF_ELECTRICITY_PROVIDER, ""),
        )

    def _covau_snapshot(self) -> CovaUPlanSnapshot | None:
        """Return the validated immutable CovaU plan snapshot for this entry."""
        if self._provider_key() != "covau" or not self._entry:
            return None

        from ..const import CONF_COVAU_PLAN_SNAPSHOT

        raw = self._entry.options.get(
            CONF_COVAU_PLAN_SNAPSHOT,
            self._entry.data.get(CONF_COVAU_PLAN_SNAPSHOT),
        )
        if not isinstance(raw, dict):
            warning = "CovaU plan snapshot is missing"
            if warning != getattr(self, "_last_covau_config_warning", None):
                _LOGGER.warning("%s; quota-aware pricing is unavailable", warning)
                self._last_covau_config_warning = warning
            return None

        try:
            snapshot = CovaUPlanSnapshot.from_dict(
                raw,
                timezone_token=getattr(
                    getattr(self.hass, "config", None),
                    "time_zone",
                    None,
                ),
            )
            # Rebuild the rules as a structural validation step. This catches
            # incomplete/invalid snapshots before they reach the LP.
            covau_quota_rules(snapshot)
        except (KeyError, TypeError, ValueError) as err:
            warning = f"Invalid CovaU plan snapshot: {err}"
            if warning != getattr(self, "_last_covau_config_warning", None):
                _LOGGER.warning("%s; quota-aware pricing is unavailable", warning)
                self._last_covau_config_warning = warning
            return None

        cached_hash = getattr(self, "_covau_snapshot_hash", None)
        if cached_hash is not None and cached_hash != snapshot.content_hash:
            # A plan change defines a new settlement contract. Never carry a
            # previous plan's consumed quota into the new immutable snapshot.
            self._covau_ledger = None
        self._covau_snapshot_cache = snapshot
        self._covau_snapshot_hash = snapshot.content_hash
        self._last_covau_config_warning = None
        return snapshot

    def _ensure_covau_ledger(
        self,
        state: QuotaLedgerState | None = None,
        *,
        now: datetime | None = None,
    ) -> tuple[CovaUPlanSnapshot, QuotaLedger] | None:
        """Return the runtime CovaU snapshot/ledger pair, lazily initialized."""
        snapshot = self._covau_snapshot()
        if snapshot is None:
            return None
        from ..const import DOMAIN

        runtime_entry_id = getattr(
            self,
            "entry_id",
            getattr(getattr(self, "_entry", None), "entry_id", ""),
        )
        shared_runtime = (
            self.hass.data.get(DOMAIN, {})
            .get(runtime_entry_id, {})
            .get("covau_quota_runtime")
        )
        if (
            shared_runtime is not None
            and shared_runtime.snapshot.content_hash == snapshot.content_hash
        ):
            if state is not None:
                shared_runtime.adopt_legacy_state(state)
            shared_runtime.ledger.advance_to(now or dt_util.now())
            self._covau_ledger = shared_runtime.ledger
            return shared_runtime.snapshot, shared_runtime.ledger
        ledger = getattr(self, "_covau_ledger", None)
        if ledger is None or state is not None:
            ledger = QuotaLedger(covau_quota_rules(snapshot), state)
            self._covau_ledger = ledger
        ledger.advance_to(now or dt_util.now())
        return snapshot, ledger

    def _covau_energy_entity_id(self, direction: str) -> str | None:
        if not self._entry:
            return None
        from ..const import (
            CONF_COVAU_EXPORT_ENERGY_ENTITY,
            CONF_COVAU_IMPORT_ENERGY_ENTITY,
        )

        key = (
            CONF_COVAU_IMPORT_ENERGY_ENTITY
            if direction == "import"
            else CONF_COVAU_EXPORT_ENERGY_ENTITY
        )
        value = self._entry.options.get(key, self._entry.data.get(key))
        return str(value).strip() if value else None

    @staticmethod
    def _covau_energy_state_kwh(state: Any) -> float | None:
        """Normalize a cumulative HA energy state to kWh."""
        if state is None or str(getattr(state, "state", "")).lower() in {
            "",
            "none",
            "unknown",
            "unavailable",
        }:
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        attrs = getattr(state, "attributes", {}) or {}
        unit = str(attrs.get("unit_of_measurement") or "kWh").strip().lower()
        if unit == "wh":
            value /= 1000.0
        elif unit == "mwh":
            value *= 1000.0
        elif unit != "kwh":
            return None
        return max(0.0, value)

    def _settle_covau_measurements(
        self,
        now: datetime,
        grid_import_kw: float,
        grid_export_kw: float,
    ) -> dict[str, float]:
        """Settle CovaU quotas from PCC meters, falling back to grid power."""
        runtime = self._ensure_covau_ledger(now=now)
        if runtime is None:
            return {"import": 0.0, "export": 0.0}
        snapshot, ledger = runtime
        from ..const import DOMAIN

        shared_runtime = (
            self.hass.data.get(DOMAIN, {})
            .get(
                getattr(
                    self,
                    "entry_id",
                    getattr(getattr(self, "_entry", None), "entry_id", ""),
                ),
                {},
            )
            .get("covau_quota_runtime")
        )
        if shared_runtime is not None and shared_runtime.ledger is ledger:
            return shared_runtime.consume_pending_settled()
        settled = {"import": 0.0, "export": 0.0}
        for direction, power_kw in (
            ("import", grid_import_kw),
            ("export", grid_export_kw),
        ):
            entity_id = self._covau_energy_entity_id(direction)
            if not entity_id:
                settled[direction] = ledger.observe_power(
                    direction,
                    max(0.0, power_kw) * 1000.0,
                    now,
                )
                continue

            state = self.hass.states.get(entity_id)
            total_kwh = self._covau_energy_state_kwh(state)
            if total_kwh is None:
                ledger.mark_unknown(f"{direction} cumulative energy meter unavailable")
                continue
            # Reading a monotonic total now is itself a current sample even if
            # its numeric value has not changed since yesterday.  Using the HA
            # state's last_updated timestamp would deadlock the new tariff day
            # whenever one direction legitimately records zero energy around
            # the local tariff-day reset.
            observed_at = now
            settled[direction] = ledger.observe_cumulative(
                direction,
                total_kwh,
                observed_at,
            )
        return settled

    def _capture_covau_measurements_before_plan(self) -> None:
        """Settle the latest PCC measurement before calculating quota caps."""
        if self._provider_key() != "covau":
            return
        data = self._get_energy_data()
        if not data:
            return
        try:
            grid_power_kw = float(data.get("grid_power", 0) or 0)
        except (TypeError, ValueError):
            return
        delta = self._settle_covau_measurements(
            dt_util.now(),
            max(0.0, grid_power_kw),
            max(0.0, -grid_power_kw),
        )
        pending = getattr(
            self,
            "_pending_covau_settlement",
            {"import": 0.0, "export": 0.0},
        )
        self._pending_covau_settlement = {
            "import": pending.get("import", 0.0) + delta["import"],
            "export": pending.get("export", 0.0) + delta["export"],
        }

    def _custom_tariff(self) -> dict[str, Any] | None:
        """Return the entry's editable custom tariff, when available."""
        from ..const import DOMAIN

        runtime_entry_id = getattr(
            self,
            "entry_id",
            getattr(getattr(self, "_entry", None), "entry_id", ""),
        )
        store = (
            self.hass.data.get(DOMAIN, {})
            .get(runtime_entry_id, {})
            .get("automation_store")
        )
        tariff = store.get_custom_tariff() if store is not None else None
        return tariff if isinstance(tariff, dict) else None

    def _ensure_custom_tariff_quota_ledger(
        self,
        state: QuotaLedgerState | None = None,
        *,
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], Any, QuotaLedger, str] | None:
        """Return the current custom-tariff quota contract and ledger."""
        tariff = self._custom_tariff()
        if tariff is None:
            return None
        rule = custom_tariff_import_quota_rule(
            tariff,
            default_timezone=getattr(
                getattr(self.hass, "config", None),
                "time_zone",
                "LOCAL",
            ),
        )
        if rule is None:
            return None
        content_hash = custom_tariff_quota_hash(tariff, rule)
        cached_hash = getattr(self, "_custom_tariff_quota_hash", None)
        if cached_hash is not None and cached_hash != content_hash:
            self._custom_tariff_quota_ledger = None
        ledger = getattr(self, "_custom_tariff_quota_ledger", None)
        if ledger is None or state is not None:
            ledger = QuotaLedger((rule,), state)
            self._custom_tariff_quota_ledger = ledger
        self._custom_tariff_quota_hash = content_hash
        ledger.advance_to(now or dt_util.now())
        return tariff, rule, ledger, content_hash

    @staticmethod
    def _energy_summary_total_kwh(
        data: dict[str, Any],
        direction: str,
    ) -> float | None:
        """Return a validated daily cumulative import/export total."""
        summary = data.get("energy_summary")
        if not isinstance(summary, dict):
            return None
        value = summary.get(f"grid_{direction}_today_kwh")
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) and parsed >= 0 else None

    def _full_day_battery_energy_summary(
        self,
    ) -> tuple[float, float, float] | None:
        """Return validated full-day battery totals from the main coordinator.

        The optimizer's private cost counters can be absent after a reload or
        when optimization is enabled part-way through a day.  The main energy
        coordinator keeps independent cumulative totals that let us prove a
        conservative lower bound on solar-origin inventory in that case.
        """
        data = getattr(getattr(self, "energy_coordinator", None), "data", None)
        if not isinstance(data, dict):
            return None
        summary = data.get("energy_summary")
        if not isinstance(summary, dict):
            return None

        values: list[float] = []
        for key in (
            "charge_today_kwh",
            "discharge_today_kwh",
            "grid_import_today_kwh",
        ):
            raw_value = summary.get(key)
            if isinstance(raw_value, bool):
                return None
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(value) or value < 0:
                return None
            values.append(value)
        return values[0], values[1], values[2]

    def _settle_custom_tariff_quota_measurements(
        self,
        now: datetime,
        grid_import_kw: float,
    ) -> float:
        """Settle the custom import allowance from measured site energy."""
        runtime = self._ensure_custom_tariff_quota_ledger(now=now)
        if runtime is None:
            return 0.0
        _tariff, _rule, ledger, _content_hash = runtime
        data = self._get_energy_data() or {}
        total_kwh = self._energy_summary_total_kwh(data, "import")
        if total_kwh is not None:
            settled = ledger.observe_cumulative("import", total_kwh, now)
        else:
            settled = ledger.observe_power(
                "import",
                max(0.0, grid_import_kw) * 1000.0,
                now,
            )
        self._schedule_cost_save()
        return settled

    def _flow_power_snapshot(self) -> "FlowPowerPlanSnapshot | None":
        """Resolve the selected Flow Power contract and preserved legacy fallback."""
        if self._provider_key() != "flow_power" or not self._entry:
            return None
        from ..const import (
            CONF_FLOW_POWER_EXPORT_RATE,
            CONF_FLOW_POWER_HAPPY_HOUR_END,
            CONF_FLOW_POWER_STATE,
            FLOW_POWER_EXPORT_RATES,
        )
        flow_power_plan_key = "flow_power_plan"

        raw = self._entry.options.get(
            flow_power_plan_key,
            self._entry.data.get(flow_power_plan_key),
        )
        # Entries created before the versioned contract existed stay on the
        # exact legacy scalar path, including its established test/runtime
        # behavior. Only an explicit selection activates the plan adapter.
        if not isinstance(raw, dict):
            return None
        from ..flow_power import resolve_flow_power_plan

        state = self._entry.options.get(
            CONF_FLOW_POWER_STATE,
            self._entry.data.get(CONF_FLOW_POWER_STATE, ""),
        )
        configured_rate = self._entry.options.get(
            CONF_FLOW_POWER_EXPORT_RATE,
            self._entry.data.get(CONF_FLOW_POWER_EXPORT_RATE),
        )
        try:
            legacy_rate = (
                float(configured_rate) / 100.0
                if configured_rate not in (None, "")
                else FLOW_POWER_EXPORT_RATES.get(state, 0.0)
            )
        except (TypeError, ValueError):
            legacy_rate = FLOW_POWER_EXPORT_RATES.get(state, 0.0)
        try:
            snapshot = resolve_flow_power_plan(
                raw,
                timezone_token=getattr(
                    getattr(self.hass, "config", None),
                    "time_zone",
                    "Australia/Sydney",
                ),
                legacy_export_rate_dollars=legacy_rate,
                legacy_happy_hour_end=self._entry.options.get(
                    CONF_FLOW_POWER_HAPPY_HOUR_END,
                    self._entry.data.get(CONF_FLOW_POWER_HAPPY_HOUR_END),
                ),
            )
        except (TypeError, ValueError) as err:
            warning = f"Invalid Flow Power plan selection: {err}"
            if warning != getattr(self, "_last_flow_power_config_warning", None):
                _LOGGER.warning("%s; retaining legacy tariff behavior", warning)
                self._last_flow_power_config_warning = warning
            snapshot = resolve_flow_power_plan(
                None,
                timezone_token=getattr(
                    getattr(self.hass, "config", None),
                    "time_zone",
                    "Australia/Sydney",
                ),
                legacy_export_rate_dollars=legacy_rate,
                legacy_happy_hour_end=self._entry.options.get(
                    CONF_FLOW_POWER_HAPPY_HOUR_END,
                    self._entry.data.get(CONF_FLOW_POWER_HAPPY_HOUR_END),
                ),
            )
        cached_hash = getattr(self, "_flow_power_plan_hash", None)
        if cached_hash is not None and cached_hash != snapshot.plan_hash:
            self._flow_power_ledger = None
        self._flow_power_plan_hash = snapshot.plan_hash
        return snapshot

    def _ensure_flow_power_ledger(
        self,
        state: QuotaLedgerState | None = None,
        *,
        now: datetime | None = None,
    ) -> tuple["FlowPowerPlanSnapshot", QuotaLedger | None] | None:
        """Return the Flow Power snapshot and its measured quota ledger."""
        snapshot = self._flow_power_snapshot()
        if snapshot is None:
            return None
        from ..flow_power import flow_power_quota_rules
        rules = flow_power_quota_rules(snapshot)
        if not rules:
            return snapshot, None
        ledger = getattr(self, "_flow_power_ledger", None)
        if ledger is None or state is not None:
            ledger = QuotaLedger(rules, state)
            self._flow_power_ledger = ledger
        ledger.advance_to(now or dt_util.now())
        return snapshot, ledger

    def _settle_flow_power_measurements(
        self,
        now: datetime,
        grid_import_kw: float,
        grid_export_kw: float,
    ) -> dict[str, float]:
        """Settle official Flow Power buckets from site totals or grid power."""
        runtime = self._ensure_flow_power_ledger(now=now)
        if runtime is None or runtime[1] is None:
            return {"import": 0.0, "export": 0.0}
        _snapshot, ledger = runtime
        data = self._get_energy_data() or {}
        settled = {"import": 0.0, "export": 0.0}
        directions = {rule.direction for rule in ledger.rules}
        for direction, power_kw in (
            ("import", grid_import_kw),
            ("export", grid_export_kw),
        ):
            if direction not in directions:
                continue
            total_kwh = self._energy_summary_total_kwh(data, direction)
            if total_kwh is not None:
                # The energy summary is a daily counter (including the
                # Sigenergy software accumulator), not a lifetime PCC meter.
                # Its expected midnight reset needs its own provenance-aware
                # settlement path.
                settled[direction] = ledger.observe_daily_total(
                    direction, total_kwh, now
                )
            else:
                settled[direction] = ledger.observe_power(
                    direction, max(0.0, power_kw) * 1000.0, now
                )
        self._schedule_cost_save()
        return settled

    def _capture_flow_power_measurements_before_plan(self) -> None:
        """Settle Flow Power measured usage immediately before planning."""
        data = self._get_energy_data()
        if not data:
            return
        try:
            grid_power_kw = float(data.get("grid_power", 0) or 0)
        except (TypeError, ValueError):
            return
        delta = self._settle_flow_power_measurements(
            dt_util.now(),
            max(0.0, grid_power_kw),
            max(0.0, -grid_power_kw),
        )
        pending = getattr(
            self, "_pending_flow_power_settlement", {"import": 0.0, "export": 0.0}
        )
        self._pending_flow_power_settlement = {
            "import": pending.get("import", 0.0) + delta["import"],
            "export": pending.get("export", 0.0) + delta["export"],
        }

    def _apply_flow_power_optimizer_inputs(
        self,
        import_prices: list[float],
        export_prices: list[float],
    ) -> None:
        """Apply official Flow Power quota bonuses through existing LP inputs."""
        n = min(len(import_prices), len(export_prices))
        self._last_zerocharge_bonus_prices = [0.0] * n
        self._last_zerocharge_bonus_cap_kwh = 0.0
        self._last_zerohero_bonus_prices = [0.0] * n
        self._last_zerohero_bonus_cap_kwh = 0.0
        self._last_import_bonus_group_ids = None
        self._last_export_bonus_group_ids = None
        self._last_import_bonus_caps_by_group = None
        self._last_export_bonus_caps_by_group = None
        runtime = self._ensure_flow_power_ledger(now=dt_util.now())
        if runtime is None or n <= 0:
            return
        snapshot, ledger = runtime
        from ..flow_power import flow_power_price_series

        series = flow_power_price_series(
            snapshot,
            self._price_timestamps(n),
            import_prices[:n],
            ledger=ledger,
        )
        self._last_settlement_import_prices = list(series.settlement_import)
        self._last_settlement_export_prices = list(series.settlement_export)
        self._last_display_import_prices = list(series.marginal_import)
        self._last_display_export_prices = list(series.marginal_export)
        self._last_grid_charge_cap_import_prices = list(series.marginal_import)
        self._last_zerocharge_bonus_prices = list(series.import_bonus)
        self._last_zerohero_bonus_prices = list(series.export_bonus)
        self._last_import_bonus_group_ids = list(series.import_group_ids)
        self._last_export_bonus_group_ids = list(series.export_group_ids)
        self._last_import_bonus_caps_by_group = dict(series.import_group_caps_kwh)
        self._last_export_bonus_caps_by_group = dict(series.export_group_caps_kwh)
        self._last_zerocharge_bonus_cap_kwh = sum(series.import_group_caps_kwh.values())
        self._last_zerohero_bonus_cap_kwh = sum(series.export_group_caps_kwh.values())

    def _flow_power_planned_quota_kwh(
        self,
        ledger: QuotaLedger | None,
    ) -> dict[str, float]:
        """Return current-day forecast reservations without mutating settlement."""
        result = getattr(self, "_last_optimizer_result", None)
        if result is None or ledger is None or ledger.state.tariff_day is None:
            return {}
        offset = self._get_forecast_offset()
        dt_hours = self._config.interval_minutes / 60.0
        planned: dict[str, float] = {}
        for values, groups in (
            (
                getattr(result, "grid_import_w", None) or [],
                self._last_import_bonus_group_ids or [],
            ),
            (
                getattr(result, "grid_export_w", None) or [],
                self._last_export_bonus_group_ids or [],
            ),
        ):
            for idx in range(offset, min(len(values), len(groups))):
                group = groups[idx]
                if not group or not group.startswith(f"{ledger.state.tariff_day}:"):
                    continue
                rule_id = group.split(":", 1)[1]
                planned[rule_id] = planned.get(rule_id, 0.0) + (
                    max(0.0, float(values[idx] or 0.0)) / 1000.0 * dt_hours
                )
        return {
            rule.rule_id: min(
                max(0.0, planned.get(rule.rule_id, 0.0)),
                ledger.remaining_kwh(rule.rule_id),
            )
            for rule in ledger.rules
        }

    def _capture_provider_quota_measurements_before_plan(self) -> None:
        """Settle the active provider allowance before calculating caps."""
        if self._provider_key() == "covau":
            self._capture_covau_measurements_before_plan()
            return
        if self._provider_key() == "flow_power":
            self._capture_flow_power_measurements_before_plan()
            return
        runtime = self._ensure_custom_tariff_quota_ledger(now=dt_util.now())
        if runtime is None:
            return
        data = self._get_energy_data()
        if not data:
            return
        try:
            grid_power_kw = float(data.get("grid_power", 0) or 0)
        except (TypeError, ValueError):
            return
        settled = self._settle_custom_tariff_quota_measurements(
            dt_util.now(),
            max(0.0, grid_power_kw),
        )
        self._pending_custom_tariff_quota_settlement = (
            getattr(self, "_pending_custom_tariff_quota_settlement", 0.0)
            + settled
        )

    def _covau_price_forecast(self) -> tuple[list[float], list[float]] | None:
        """Build local-time CovaU base prices and marginal quota bonuses."""
        runtime = self._ensure_covau_ledger(now=dt_util.now())
        if runtime is None:
            return None
        snapshot, ledger = runtime
        interval = max(1, int(self._config.interval_minutes or 5))
        raw_now = dt_util.now()
        start = raw_now.replace(
            minute=(raw_now.minute // interval) * interval,
            second=0,
            microsecond=0,
        )
        n_steps = int(self._config.horizon_hours * 60) // interval
        timestamps = self._interval_timestamps(start, n_steps, interval)
        (
            import_prices,
            export_prices,
            import_bonus,
            export_bonus,
            import_cap,
            export_cap,
        ) = covau_price_series(snapshot, timestamps, ledger)
        self._set_covau_bonus_groups(
            snapshot,
            ledger,
            timestamps,
            import_bonus,
            export_bonus,
        )

        # The LP consumes base prices plus bounded bonus variables. User-facing
        # prices show the current marginal tariff while quota remains.
        self._last_display_import_prices = [
            max(0.0, base - bonus)
            for base, bonus in zip(import_prices, import_bonus, strict=False)
        ]
        self._last_display_export_prices = [
            base + bonus
            for base, bonus in zip(export_prices, export_bonus, strict=False)
        ]
        self._last_settlement_import_prices = list(import_prices)
        self._last_settlement_export_prices = list(export_prices)
        self._last_grid_charge_cap_import_prices = list(
            self._last_display_import_prices
        )
        self._pending_price_timestamps = timestamps
        self._last_zerocharge_bonus_prices = import_bonus
        self._last_zerocharge_bonus_cap_kwh = sum(
            (self._last_import_bonus_caps_by_group or {}).values()
        )
        self._last_zerohero_bonus_prices = export_bonus
        self._last_zerohero_bonus_cap_kwh = sum(
            (self._last_export_bonus_caps_by_group or {}).values()
        )
        return import_prices, export_prices

    def _apply_covau_optimizer_inputs(
        self,
        import_prices: list[float],
        export_prices: list[float],
    ) -> None:
        """Refresh CovaU marginal bonus arrays immediately before each solve."""
        runtime = self._ensure_covau_ledger(now=dt_util.now())
        n = min(len(import_prices), len(export_prices))
        self._last_zerocharge_bonus_prices = [0.0] * n
        self._last_zerocharge_bonus_cap_kwh = 0.0
        self._last_zerohero_bonus_prices = [0.0] * n
        self._last_zerohero_bonus_cap_kwh = 0.0
        self._last_import_bonus_group_ids = None
        self._last_export_bonus_group_ids = None
        self._last_import_bonus_caps_by_group = None
        self._last_export_bonus_caps_by_group = None
        if runtime is None or n <= 0:
            return
        snapshot, ledger = runtime
        (
            _base_import,
            _base_export,
            import_bonus,
            export_bonus,
            import_cap,
            export_cap,
        ) = covau_price_series(snapshot, self._price_timestamps(n), ledger)
        timestamps = self._price_timestamps(n)
        self._set_covau_bonus_groups(
            snapshot,
            ledger,
            timestamps,
            import_bonus,
            export_bonus,
        )
        self._last_zerocharge_bonus_prices = import_bonus
        self._last_zerocharge_bonus_cap_kwh = sum(
            (self._last_import_bonus_caps_by_group or {}).values()
        )
        self._last_zerohero_bonus_prices = export_bonus
        self._last_zerohero_bonus_cap_kwh = sum(
            (self._last_export_bonus_caps_by_group or {}).values()
        )
        if import_cap > 0 and any(import_bonus):
            _LOGGER.info("CovaU optimizer: %.2fkWh free-import quota remaining", import_cap)
        if export_cap > 0 and any(export_bonus):
            _LOGGER.info("CovaU optimizer: %.2fkWh premium-export quota remaining", export_cap)

    def _apply_custom_tariff_quota_optimizer_inputs(
        self,
        import_prices: list[float],
        export_prices: list[float],
    ) -> None:
        """Apply a custom tariff's capped import allowance to the solve."""
        n = min(len(import_prices), len(export_prices))
        self._last_zerocharge_bonus_prices = [0.0] * n
        self._last_zerocharge_bonus_cap_kwh = 0.0
        self._last_zerohero_bonus_prices = [0.0] * n
        self._last_zerohero_bonus_cap_kwh = 0.0
        self._last_import_bonus_group_ids = None
        self._last_export_bonus_group_ids = None
        self._last_import_bonus_caps_by_group = None
        self._last_export_bonus_caps_by_group = None
        runtime = self._ensure_custom_tariff_quota_ledger(now=dt_util.now())
        if runtime is None or n <= 0:
            return
        _tariff, rule, ledger, _content_hash = runtime
        if ledger.state.confidence == "unknown":
            return

        timestamps = self._price_timestamps(n)
        current_day = ledger.state.tariff_day
        bonuses = [0.0] * n
        groups: list[str | None] = []
        caps: dict[str, float] = {}
        bonus_price = rule.bonus_price_c_per_kwh / 100.0
        for idx, timestamp in enumerate(timestamps):
            if not rule.contains(timestamp):
                groups.append(None)
                continue
            day = tariff_datetime(timestamp, rule.timezone_token).date().isoformat()
            remaining_kwh = (
                ledger.remaining_kwh(CUSTOM_TARIFF_IMPORT_RULE_ID)
                if day == current_day
                else rule.daily_cap_kwh
            )
            if remaining_kwh <= 1e-9:
                groups.append(None)
                continue
            groups.append(day)
            bonuses[idx] = bonus_price
            caps[day] = remaining_kwh
            if idx < len(self._last_display_import_prices or []):
                self._last_display_import_prices[idx] = max(
                    0.0,
                    import_prices[idx] - bonus_price,
                )

        self._last_zerocharge_bonus_prices = bonuses
        self._last_import_bonus_group_ids = groups
        self._last_import_bonus_caps_by_group = caps
        self._last_zerocharge_bonus_cap_kwh = sum(caps.values())
        if self._last_display_import_prices:
            self._last_grid_charge_cap_import_prices = list(
                self._last_display_import_prices
            )
        if caps and any(bonuses):
            _LOGGER.info(
                "Custom tariff optimizer: %.2fkWh import allowance remaining today",
                ledger.remaining_kwh(CUSTOM_TARIFF_IMPORT_RULE_ID),
            )

    def _set_covau_bonus_groups(
        self,
        snapshot: CovaUPlanSnapshot,
        ledger: QuotaLedger,
        timestamps: list[datetime],
        import_bonus: list[float],
        export_bonus: list[float],
    ) -> None:
        """Partition marginal allowances by local tariff day."""
        import_rule, export_rule = covau_quota_rules(snapshot)
        current_day = ledger.state.tariff_day
        import_groups: list[str | None] = []
        export_groups: list[str | None] = []
        import_caps: dict[str, float] = {}
        export_caps: dict[str, float] = {}
        for idx, timestamp in enumerate(timestamps):
            day = tariff_datetime(timestamp, snapshot.timezone_token).date().isoformat()
            import_group = day if idx < len(import_bonus) and import_bonus[idx] > 0 else None
            export_group = day if idx < len(export_bonus) and export_bonus[idx] > 0 else None
            import_groups.append(import_group)
            export_groups.append(export_group)
            if import_group is not None:
                import_caps[day] = (
                    ledger.remaining_kwh(COVAU_IMPORT_RULE_ID)
                    if day == current_day
                    else import_rule.daily_cap_kwh
                )
            if export_group is not None:
                export_caps[day] = (
                    ledger.remaining_kwh(COVAU_EXPORT_RULE_ID)
                    if day == current_day
                    else export_rule.daily_cap_kwh
                )
        self._last_import_bonus_group_ids = import_groups
        self._last_export_bonus_group_ids = export_groups
        self._last_import_bonus_caps_by_group = import_caps
        self._last_export_bonus_caps_by_group = export_caps

    def _apply_provider_quota_optimizer_inputs(
        self,
        import_prices: list[float],
        export_prices: list[float],
    ) -> None:
        """Apply capped-tariff inputs without changing provider semantics."""
        if self._provider_key() == "covau":
            self._apply_covau_optimizer_inputs(import_prices, export_prices)
            return
        if self._provider_key() == "flow_power":
            self._apply_flow_power_optimizer_inputs(import_prices, export_prices)
            return
        if self._ensure_custom_tariff_quota_ledger(now=dt_util.now()) is not None:
            self._apply_custom_tariff_quota_optimizer_inputs(
                import_prices,
                export_prices,
            )
            return
        self._apply_zerohero_optimizer_inputs(import_prices, export_prices)

    def _covau_export_window_slots(self, n: int) -> list[bool]:
        runtime = self._ensure_covau_ledger(now=dt_util.now())
        if runtime is None or n <= 0:
            return [False] * max(0, n)
        snapshot, ledger = runtime
        if ledger.state.confidence == "unknown":
            return [False] * n
        groups = self._last_export_bonus_group_ids or []
        bonuses = self._last_zerohero_bonus_prices or []
        return [
            idx < len(groups)
            and groups[idx] is not None
            and idx < len(bonuses)
            and bonuses[idx] > 0
            for idx in range(n)
        ]

    def _covau_planned_quota_kwh(self) -> tuple[float, float]:
        runtime = self._ensure_covau_ledger(now=dt_util.now())
        result = getattr(self, "_last_optimizer_result", None)
        if runtime is None or result is None:
            return 0.0, 0.0
        snapshot, _ledger = runtime
        import_rule, export_rule = covau_quota_rules(snapshot)
        imports = getattr(result, "grid_import_w", None) or []
        exports = getattr(result, "grid_export_w", None) or []
        n = max(len(imports), len(exports))
        timestamps = self._price_timestamps(n)
        offset = self._get_forecast_offset()
        dt_hours = self._config.interval_minutes / 60.0
        planned_import = sum(
            max(0.0, float(imports[idx])) / 1000.0 * dt_hours
            for idx in range(offset, min(len(imports), len(timestamps)))
            if import_rule.contains(timestamps[idx])
            and tariff_datetime(timestamps[idx], snapshot.timezone_token)
            .date()
            .isoformat()
            == _ledger.state.tariff_day
        )
        planned_export = sum(
            max(0.0, float(exports[idx])) / 1000.0 * dt_hours
            for idx in range(offset, min(len(exports), len(timestamps)))
            if export_rule.contains(timestamps[idx])
            and tariff_datetime(timestamps[idx], snapshot.timezone_token)
            .date()
            .isoformat()
            == _ledger.state.tariff_day
        )
        return (
            min(
                planned_import,
                _ledger.remaining_kwh(COVAU_IMPORT_RULE_ID),
            ),
            min(
                planned_export,
                _ledger.remaining_kwh(COVAU_EXPORT_RULE_ID),
            ),
        )

    def get_provider_contract(self) -> dict[str, Any] | None:
        """Return the stable provider/runtime contract used by HA and mobile."""
        if self._provider_key() == "flow_power":
            runtime = self._ensure_flow_power_ledger(now=dt_util.now())
            if runtime is None:
                return None
            snapshot, ledger = runtime
            current_import = 0.0
            prices = getattr(self, "_last_settlement_import_prices", None) or []
            offset = self._get_forecast_offset()
            if offset < len(prices):
                current_import = max(0.0, float(prices[offset] or 0.0))
            from ..flow_power import flow_power_provider_contract

            return flow_power_provider_contract(
                snapshot,
                at=dt_util.now(),
                import_price=current_import,
                ledger=ledger,
                planned_kwh=self._flow_power_planned_quota_kwh(ledger),
            )
        runtime = self._ensure_covau_ledger(now=dt_util.now())
        if runtime is not None:
            snapshot, ledger = runtime
            planned_import, planned_export = self._covau_planned_quota_kwh()
            return covau_provider_contract(
                snapshot,
                ledger,
                planned_import_kwh=planned_import,
                planned_export_kwh=planned_export,
                now=dt_util.now(),
                import_energy_entity=self._covau_energy_entity_id("import"),
                export_energy_entity=self._covau_energy_entity_id("export"),
            )
        custom_runtime = self._ensure_custom_tariff_quota_ledger(
            now=dt_util.now()
        )
        if custom_runtime is None:
            return None
        tariff, rule, ledger, _content_hash = custom_runtime
        return custom_tariff_quota_contract(tariff, rule, ledger)

    def _zerohero_config(self) -> ZeroHeroConfig | None:
        """Return resolved GloBird ZeroHero settings for this entry."""
        if self._provider_key() != "globird":
            return None
        tariff_schedule = getattr(self, "_tariff_schedule", None)
        if getattr(self, "hass", None) is not None:
            tariff_schedule = self._get_tou_tariff_schedule()
        config = zerohero_config_from_entry(self._entry, tariff_schedule)
        if config is not None:
            raw_plan = None
            if self._entry is not None:
                raw_plan = self._entry.options.get(
                    "globird_plan",
                    self._entry.data.get("globird_plan"),
                )
            if raw_plan in (None, "", GLOBIRD_PLAN_NOT_ZEROHERO):
                logged_plan = getattr(self, "_logged_inferred_zerohero_plan", None)
                if logged_plan != config.plan:
                    _LOGGER.info(
                        "GloBird ZeroHero plan auto-detected from tariff: %s",
                        config.plan,
                    )
                    self._logged_inferred_zerohero_plan = config.plan
        return config

    def _cost_neutral_settlement_prices(
        self,
        import_prices: list[float],
        export_prices: list[float],
    ) -> tuple[list[float], list[float]]:
        """Return billable prices, excluding optimizer-only price overlays."""

        display_import = (
            getattr(self, "_last_settlement_import_prices", None)
            or getattr(self, "_last_display_import_prices", None)
            or []
        )
        display_export = (
            getattr(self, "_last_settlement_export_prices", None)
            or getattr(self, "_last_display_export_prices", None)
            or []
        )
        fallback_import = display_import[-1] if display_import else None
        fallback_export = display_export[-1] if display_export else None
        settlement_import = [
            float(display_import[idx])
            if idx < len(display_import)
            else float(fallback_import if fallback_import is not None else value)
            for idx, value in enumerate(import_prices)
        ]
        settlement_export = [
            float(display_export[idx])
            if idx < len(display_export)
            else float(fallback_export if fallback_export is not None else value)
            for idx, value in enumerate(export_prices)
        ]
        return settlement_import, settlement_export

    def _price_timestamps(self, n: int) -> list[datetime]:
        """Return local timestamps aligned with the current optimizer interval."""
        pending_timestamps = getattr(self, "_pending_price_timestamps", None)
        if pending_timestamps and len(pending_timestamps) >= n:
            return pending_timestamps[:n]
        if self._last_price_timestamps and len(self._last_price_timestamps) >= n:
            return self._last_price_timestamps[:n]

        raw_now = dt_util.now()
        interval = self._config.interval_minutes
        start = raw_now.replace(
            minute=(raw_now.minute // interval) * interval,
            second=0,
            microsecond=0,
        )
        return self._interval_timestamps(start, n, interval)

    @staticmethod
    def _interval_timestamps(
        local_start: datetime,
        count: int,
        interval_minutes: int,
    ) -> list[datetime]:
        """Build an instant-contiguous timeline rendered in the HA timezone."""

        if local_start.tzinfo is None:
            return [
                local_start + timedelta(minutes=idx * interval_minutes)
                for idx in range(count)
            ]
        utc_start = local_start.astimezone(timezone.utc)
        return [
            (
                utc_start + timedelta(minutes=idx * interval_minutes)
            ).astimezone(local_start.tzinfo)
            for idx in range(count)
        ]

    def _commit_price_forecast_cache(
        self,
        import_prices: list[float],
        export_prices: list[float],
    ) -> None:
        """Atomically accept prices and their staged interval grid."""
        self._last_import_prices = import_prices
        self._last_export_prices = export_prices
        pending_timestamps = getattr(self, "_pending_price_timestamps", None)
        if pending_timestamps is not None:
            self._last_price_timestamps = pending_timestamps
        self._pending_price_timestamps = None

    def _zerohero_window_slots(self, n: int) -> list[bool]:
        """Return optimizer slots inside the configured ZeroHero window."""
        config = self._zerohero_config()
        if config is None or n <= 0:
            return [False] * max(0, n)
        return [
            zerohero_is_in_window(ts, config)
            for ts in self._price_timestamps(n)
        ]

    def _zerohero_bonus_window_slots(self, n: int) -> list[bool]:
        """Return ZeroHero window slots with a positive daily bonus quota.

        The scalar ``_last_zerohero_bonus_cap_kwh`` is intentionally the
        current-day remainder for public/runtime compatibility.  A forecast
        can also contain future local days, so use the staged per-day quota
        groups when they are available; this keeps a spent current day from
        hiding a still-available future-day window.
        """
        config = self._zerohero_config()
        if config is None or n <= 0:
            return [False] * max(0, n)

        group_ids = getattr(self, "_last_export_bonus_group_ids", None)
        caps_by_group = getattr(self, "_last_export_bonus_caps_by_group", None)
        if group_ids is not None and caps_by_group is not None:
            timestamps = self._price_timestamps(n)
            return [
                idx < len(group_ids)
                and group_ids[idx] is not None
                and max(
                    0.0,
                    float(caps_by_group.get(str(group_ids[idx]), 0.0) or 0.0),
                )
                > 1e-6
                and zerohero_is_in_window(timestamps[idx], config)
                for idx in range(n)
            ]

        # Lightweight callers/tests predating quota grouping may set only the
        # scalar cap; retain their established window behaviour.
        try:
            scalar_cap = float(
                getattr(self, "_last_zerohero_bonus_cap_kwh", 0.0) or 0.0
            )
        except (TypeError, ValueError):
            scalar_cap = 0.0
        if scalar_cap <= 1e-6:
            return [False] * n
        return self._zerohero_window_slots(n)

    def _zerocharge_window_slots(self, n: int) -> list[bool]:
        """Return optimizer slots inside the configured ZeroCharge window."""
        config = self._zerohero_config()
        if config is None or n <= 0 or not config.zerocharge_enabled:
            return [False] * max(0, n)
        return [
            zerocharge_is_in_window(ts, config)
            for ts in self._price_timestamps(n)
        ]

    @staticmethod
    def _zerocharge_tariff_day(ts: datetime) -> str:
        """Return the local calendar month owning a ZeroCharge allowance.

        The method name is retained for compatibility with older callers, but
        ZeroCharge allowances are now grouped by ``YYYY-MM`` rather than day.
        """
        return zerocharge_period_key(ts)

    @staticmethod
    def _zerocharge_period_key(ts: datetime) -> str:
        """Return the local calendar month owning a ZeroCharge allowance."""
        return zerocharge_period_key(ts)

    def _ensure_zerocharge_period_state(
        self,
        now: datetime,
        *,
        baseline: bool = False,
    ) -> tuple[str, float, float]:
        """Return and normalize actual/baseline ZeroCharge month state.

        Legacy lightweight coordinators and pre-monthly persisted state only
        expose the ``*_today`` counters.  Adopt those once when no explicit
        period exists, then keep the explicit month fields authoritative.
        """
        prefix = "_baseline_zerocharge" if baseline else "_actual_zerocharge"
        period_attr = f"{prefix}_period_key"
        import_attr = f"{prefix}_import_kwh_month"
        credit_attr = f"{prefix}_credit_value_month"
        legacy_import_attr = f"{prefix}_import_kwh_today"
        legacy_credit_attr = f"{prefix}_credit_value_today"
        period = getattr(self, period_attr, None)
        key = self._zerocharge_period_key(now)
        if period != key:
            # A missing period is a conservative migration point for old
            # in-memory/test coordinators.  Persisted legacy data is migrated
            # in _restore_cost_data only when its stored day is today.
            legacy_import = max(0.0, float(getattr(self, legacy_import_attr, 0.0)))
            legacy_credit = max(0.0, float(getattr(self, legacy_credit_attr, 0.0)))
            if period is None and (legacy_import or legacy_credit):
                imported = legacy_import
                credit = legacy_credit
            else:
                imported = 0.0
                credit = 0.0
            setattr(self, period_attr, key)
            setattr(self, import_attr, imported)
            setattr(self, credit_attr, credit)
        else:
            imported = max(0.0, float(getattr(self, import_attr, 0.0)))
            credit = max(0.0, float(getattr(self, credit_attr, 0.0)))
            # Older callers may still update the legacy alias directly (the
            # lightweight coordinators in the regression suite do this).  A
            # larger legacy value is safe to adopt without ever reducing the
            # explicit month-to-date state.
            imported = max(
                imported,
                max(0.0, float(getattr(self, legacy_import_attr, 0.0))),
            )
            credit = max(
                credit,
                max(0.0, float(getattr(self, legacy_credit_attr, 0.0))),
            )
            setattr(self, import_attr, imported)
            setattr(self, credit_attr, credit)
        # Keep the historical public/serialized keys compatible.
        setattr(self, legacy_import_attr, imported)
        setattr(self, legacy_credit_attr, credit)
        return key, imported, credit

    def _set_zerocharge_period_state(
        self,
        *,
        period_key: str,
        import_kwh: float,
        credit_value: float,
        baseline: bool = False,
    ) -> None:
        prefix = "_baseline_zerocharge" if baseline else "_actual_zerocharge"
        setattr(self, f"{prefix}_period_key", period_key)
        setattr(self, f"{prefix}_import_kwh_month", max(0.0, import_kwh))
        setattr(self, f"{prefix}_credit_value_month", max(0.0, credit_value))
        setattr(self, f"{prefix}_import_kwh_today", max(0.0, import_kwh))
        setattr(self, f"{prefix}_credit_value_today", max(0.0, credit_value))

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
        self._last_import_bonus_group_ids = None
        self._last_export_bonus_group_ids = None
        self._last_import_bonus_caps_by_group = None
        self._last_export_bonus_caps_by_group = None

        config = self._zerohero_config()
        if config is None or n <= 0:
            return

        timestamps = self._price_timestamps(n)
        if config.zerocharge_enabled:
            current_period, current_import_used, _current_credit = (
                self._ensure_zerocharge_period_state(dt_util.now())
            )
            current_import_cap = max(
                0.0,
                zerocharge_monthly_cap_kwh(config, current_period)
                - current_import_used,
            )
            import_groups: list[str | None] = [None] * n
            import_caps: dict[str, float] = {}
            for idx, ts in enumerate(timestamps):
                if not zerocharge_is_in_window(ts, config):
                    continue
                tariff_day = self._zerocharge_period_key(ts)
                remaining_import_cap = (
                    current_import_cap
                    if tariff_day == current_period
                    else zerocharge_monthly_cap_kwh(config, ts)
                )
                if remaining_import_cap <= 1e-9:
                    continue
                import_groups[idx] = tariff_day
                import_caps[tariff_day] = remaining_import_cap
                self._last_zerocharge_bonus_prices[idx] = max(
                    0.0,
                    import_prices[idx] if idx < len(import_prices) else 0.0,
                )
            self._last_import_bonus_group_ids = import_groups
            self._last_import_bonus_caps_by_group = import_caps
            self._last_zerocharge_bonus_cap_kwh = sum(import_caps.values())
            if import_caps and any(self._last_zerocharge_bonus_prices):
                _LOGGER.info(
                    "ZeroCharge optimizer: %.2fkWh free-import capacity across "
                    "%d calendar month(s), %s-%s",
                    self._last_zerocharge_bonus_cap_kwh,
                    len(import_caps),
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
        current_day = dt_util.now().date().isoformat()
        export_groups: list[str | None] = [None] * n
        export_caps: dict[str, float] = {}
        for idx, ts in enumerate(timestamps):
            if not zerohero_is_in_window(ts, config):
                continue
            # Keep planned grid import out of every no-import window, even
            # when that local day has exhausted its export bonus allowance.
            import_prices[idx] += 5.0
            tariff_day = ts.date().isoformat()
            if tariff_day == current_day:
                day_cap = remaining_cap
            elif tariff_day > current_day:
                day_cap = max(0.0, config.export_cap_kwh)
            else:
                # A stale forecast must not spend a prior day's allowance.
                continue
            if day_cap <= 1e-9:
                continue
            export_groups[idx] = tariff_day
            export_caps[tariff_day] = day_cap
            base_fit = max(0.0, export_prices[idx] if idx < len(export_prices) else 0.0)
            self._last_zerohero_bonus_prices[idx] = max(
                0.0,
                config.super_export_rate - base_fit,
            )

        self._last_export_bonus_group_ids = export_groups
        self._last_export_bonus_caps_by_group = export_caps
        self._last_zerohero_bonus_cap_kwh = remaining_cap
        if remaining_cap > 0 and any(self._last_zerohero_bonus_prices):
            _LOGGER.info(
                "ZeroHero optimizer: %.2fkWh bonus cap remaining, %.1fc/kWh Super Export target",
                remaining_cap,
                config.super_export_rate * 100,
            )

    def _zerohero_cost_breakdown(self) -> dict[str, Any]:
        """Return API-visible ZeroHero settlement status."""
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
        zerocharge_period = None
        if config.zerocharge_enabled:
            zerocharge_period, zerocharge_used, zerocharge_credit = (
                self._ensure_zerocharge_period_state(dt_util.now())
            )
            remaining_zerocharge = max(
                0.0,
                zerocharge_monthly_cap_kwh(config, zerocharge_period)
                - zerocharge_used,
            )
        else:
            zerocharge_used = 0.0
            zerocharge_credit = 0.0
            remaining_zerocharge = 0.0
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
            "zerocharge_import_cap_mode": "daily_average_monthly_pool",
            "zerocharge_period_key": zerocharge_period,
            "zerocharge_monthly_cap_kwh": round(
                zerocharge_monthly_cap_kwh(config, zerocharge_period)
                if zerocharge_period
                else 0.0,
                4,
            ),
            "zerocharge_import_kwh_used": round(
                zerocharge_used,
                4,
            ),
            "zerocharge_import_kwh_remaining": round(remaining_zerocharge, 4),
            "zerocharge_credit_value": round(
                zerocharge_credit,
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
            "history_diagnostics": dict(
                getattr(self._load_estimator, "_history_diagnostics", {}) or {}
            ) if self._load_estimator else {},
            "recent_load_diagnostics": dict(
                getattr(self._load_estimator, "_recent_load_diagnostics", {}) or {}
            ) if self._load_estimator else {},
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

        # A zero-charge hold is persistent hardware state. Clear anything left
        # by a restart before a new solve or command can take ownership.
        if not await self._solar_export_hold.async_reconcile_startup():
            _LOGGER.error(
                "Profit Max: stale solar-export hold could not be cleared; "
                "solar-export scheduling will remain disabled"
            )

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
            target_charge_power_supported=self._supports_target_charge_power(),
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
                CONF_OPTIMIZATION_GRID_CHARGE_BLACKOUT_WINDOWS,
                CONF_OPTIMIZATION_BATTERY_EFFICIENCY_LEARNING,
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
            try:
                self._config.grid_charge_blackout_windows = (
                    normalize_grid_charge_blackout_windows(
                        self._entry.options.get(
                            CONF_OPTIMIZATION_GRID_CHARGE_BLACKOUT_WINDOWS,
                            self._entry.data.get(
                                CONF_OPTIMIZATION_GRID_CHARGE_BLACKOUT_WINDOWS,
                                [],
                            ),
                        )
                    )
                )
            except ValueError:
                _LOGGER.warning(
                    "Invalid persisted grid-charge blackout windows; using no blackout"
                )
                self._config.grid_charge_blackout_windows = []
            self._config.battery_efficiency_learning_enabled = bool(
                self._entry.options.get(
                    CONF_OPTIMIZATION_BATTERY_EFFICIENCY_LEARNING,
                    self._entry.data.get(
                        CONF_OPTIMIZATION_BATTERY_EFFICIENCY_LEARNING,
                        True,
                    ),
                )
            )
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
            self._config.disable_idle_enabled = raw_disable_idle
            if self._should_disable_idle_schedule():
                _LOGGER.info("No Idle mode: ENABLED")

            profit_max = self._entry.options.get(
                CONF_PROFIT_MAX_ENABLED,
                self._entry.data.get(CONF_PROFIT_MAX_ENABLED, False),
            )
            cost_neutral = self._entry.options.get(
                COST_NEUTRAL_OPTION,
                self._entry.data.get(COST_NEUTRAL_OPTION, False),
            )
            # Cost Neutral is the restrictive deterministic winner for legacy
            # entries that somehow persisted both mutually-exclusive modes.
            self._config.cost_neutral_enabled = bool(cost_neutral)
            self._config.profit_max_enabled = bool(
                profit_max and not self._config.cost_neutral_enabled
            )
            if bool(profit_max) and self._config.cost_neutral_enabled:
                repaired = dict(self._entry.options)
                repaired[CONF_PROFIT_MAX_ENABLED] = False
                repaired[COST_NEUTRAL_OPTION] = True
                self.hass.config_entries.async_update_entry(
                    self._entry, options=repaired
                )
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
        await self._restore_solar_forecast_learning()
        await self._restore_battery_efficiency_learning()

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
            "agl",
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

    def _price_update_generation_key(self) -> tuple[str, ...] | None:
        """Return the current dynamic-price interval identity when available.

        Amber-format providers expose their settled current rows in ``current``
        with an interval end (normally ``nemTime``).  Do not use price values
        in the key: an in-window correction should still coalesce to one solve,
        while crossing into the next billing interval must not be discarded by
        the elapsed-time duplicate guard.
        """
        data = getattr(self.price_coordinator, "data", None)
        current = data.get("current") if isinstance(data, dict) else None
        if not isinstance(current, list):
            return None

        ends = {
            str(end)
            for row in current
            if isinstance(row, dict)
            and row.get("type", "CurrentInterval") == "CurrentInterval"
            and (end := row.get("nemTime") or row.get("valid_to") or row.get("end"))
        }
        return tuple(sorted(ends)) or None

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
        # and repeated force mode commands inside the same interval, but do
        # not suppress a settled next billing interval that arrives shortly
        # after a boundary solve.
        now = dt_util.utcnow()
        generation = self._price_update_generation_key()
        min_interval_seconds = (self._config.interval_minutes if self._config else 5) * 60
        if self._last_price_triggered_optimization is not None:
            elapsed = (now - self._last_price_triggered_optimization).total_seconds()
            same_generation = (
                generation is None
                or generation == getattr(self, "_last_price_triggered_generation", None)
            )
            if elapsed < min_interval_seconds and same_generation:
                _LOGGER.debug(
                    "Price update: skipping LP (last ran %.0fs ago, interval %ds)",
                    elapsed, min_interval_seconds,
                )
                return
        self._last_price_triggered_optimization = now
        self._last_price_triggered_generation = generation

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

        # A previous disable may have failed to release the temporary
        # no-discharge cap used by a charge-preserving IDLE hold.  Retry that
        # hardware cleanup before starting the executor or scheduling a new
        # solve; otherwise the next optimizer session can inherit a stale
        # zero-discharge limit even though its first action is not IDLE.
        if getattr(self, "_idle_no_discharge_active", False):
            if self._monitoring_mode_active():
                _LOGGER.info(
                    "Optimizer enable: monitoring mode active — retaining pending "
                    "IDLE no-discharge cleanup without hardware writes"
                )
            elif not await self._restore_idle_no_discharge_mode(
                "optimizer enable retry"
            ):
                _LOGGER.error(
                    "Cannot enable optimization - pending IDLE no-discharge "
                    "cleanup still failed"
                )
                return False

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

    def _sync_brand_restore_targets(self, reserve_pct: int) -> None:
        """Push a live backup-reserve change to the brand's hardware restore
        target (OB-22, Sigenergy-only).

        Sigenergy's ``restore_normal()`` writes hardware from a separate
        ``SigenergyController`` instance's ``_restore_backup_reserve_pct``,
        which is only ever set at initial ``async_setup_entry`` and is not
        the same object as ``self._executor.battery_controller`` (a thin
        ``BatteryControllerWrapper``). Without this, a live reserve change
        made without a reload survives only until the next force/restore
        cycle, when hardware is written back to the stale value. No-op for
        every other brand.
        """
        if getattr(self, "battery_system", None) != "sigenergy":
            return
        from ..const import DOMAIN as _SYNC_DOMAIN
        entry_data = self.hass.data.get(_SYNC_DOMAIN, {}).get(self.entry_id, {})
        sigenergy_coordinator = entry_data.get("sigenergy_coordinator")
        ctrl = getattr(sigenergy_coordinator, "_controller", None)
        if ctrl is not None and hasattr(ctrl, "_restore_backup_reserve_pct"):
            ctrl._restore_backup_reserve_pct = int(reserve_pct)

    async def _deferred_enable_restore(self) -> None:
        """Restore backup reserve and work mode in the background.

        Runs as a background task so Modbus operations (which may retry /
        time-out) don't block async_setup_entry and risk HA bootstrap
        stage 2 cancellation.
        """
        if not self._enabled:
            return
        attempt = 0
        while self._enabled and not self._energy_telemetry_ready():
            attempt += 1
            if attempt == 1:
                _LOGGER.info(
                    "Optimizer startup: native battery integration is not "
                    "ready — waiting before mode writes"
                )
            await asyncio.sleep(min(30, 5 * attempt))
        if not self._enabled:
            return
        # Start in self-consumption mode so the battery serves home load
        # immediately. Without this, the first LP action might be IDLE
        # (especially at night with no solar), forcing grid import until
        # the optimizer completes its first run.
        battery = self._executor.battery_controller if self._executor else None
        if battery:
            async def _set_startup_self_consumption() -> bool:
                if (
                    self._energy_uses_native_battery_integration()
                    and self.energy_coordinator
                    and hasattr(self.energy_coordinator, "restore_normal")
                ):
                    return bool(await self.energy_coordinator.restore_normal())
                if hasattr(battery, "set_self_consumption_mode"):
                    return bool(await battery.set_self_consumption_mode())
                return False

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
                self._sync_brand_restore_targets(startup_reserve)
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
                            self._sync_brand_restore_targets(startup_reserve)
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
                            self._sync_brand_restore_targets(startup_reserve)
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
                    if await _set_startup_self_consumption():
                        _LOGGER.info("Optimizer startup: set self-consumption mode (stale force restore)")
                    else:
                        _LOGGER.warning(
                            "Optimizer startup: stale force self-consumption restore failed"
                        )
                except Exception as e:
                    _LOGGER.warning("Failed to set self-consumption during stale force restore: %s", e)
            elif _force_active:
                _LOGGER.info("Optimizer startup: force mode active — skipping self-consumption mode set")
            else:
                try:
                    if await _set_startup_self_consumption():
                        _LOGGER.info("Optimizer startup: set self-consumption mode (battery serves load)")
                    else:
                        _LOGGER.warning(
                            "Optimizer startup: self-consumption restore failed"
                        )
                except Exception as e:
                    _LOGGER.warning("Failed to set self-consumption on startup: %s", e)

        # FoxESS/Sungrow/Sigenergy: also ensure normal work mode (exit any
        # leftover IDLE hold mode from a previous HA restart)
        if (
            self.energy_coordinator
            and hasattr(self.energy_coordinator, "restore_work_mode_from_idle")
            and not self._energy_uses_native_battery_integration()
            and not _monitoring
            and not _force_active
        ):
            try:
                restored = await self._call_optimizer_energy_control(
                    "restore_work_mode_from_idle"
                )
                if self.battery_system == "solaredge" and restored is False:
                    raise RuntimeError(
                        "SolarEdge did not confirm the startup work-mode restore"
                    )
                _LOGGER.info("Optimizer startup: ensured normal operation mode")
            except Exception as e:
                _LOGGER.warning("Failed to restore work mode on enable: %s", e)
                if self.battery_system == "solaredge":
                    raise

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
                        gs = _coord.data.grid_status
                        if _grid_status_is_terminal_off_grid(gs):
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

    async def _wait_for_deferred_enable_restore(self) -> bool:
        """Wait for startup hardware restoration before running a solve.

        The restore and first optimization remain background tasks so Home
        Assistant setup stays responsive, but hardware writes must be ordered.
        Otherwise a late startup self-consumption restore can cancel a force
        action that the first optimizer solve has just issued.
        """
        restore_task = getattr(self, "_deferred_restore_task", None)
        if restore_task is None or restore_task is asyncio.current_task():
            return self._enabled

        if not restore_task.done():
            _LOGGER.debug(
                "Optimizer: waiting for startup hardware restoration before solving"
            )
        try:
            await restore_task
        except asyncio.CancelledError:
            if not self._enabled:
                return False
            raise
        except Exception as err:
            _LOGGER.warning(
                "Optimizer: startup hardware restoration failed; skipping solve: %s",
                err,
            )
            return False
        return self._enabled

    async def disable(self) -> None:
        """Disable optimization."""
        solar_export_hold = getattr(self, "_solar_export_hold", None)
        if solar_export_hold and solar_export_hold.active:
            if not await solar_export_hold.clear("optimizer_disable"):
                _LOGGER.error(
                    "Profit Max: solar-export hold restore failed during disable; "
                    "persisted cleanup will retry on startup"
                )
        if not self._enabled:
            return

        monitoring_mode = self._monitoring_mode_active()
        # Safety: restore any pending pre-IDLE backup_reserve before shutting
        # down. Gated on the pending reserve itself, not on
        # _last_executed_action == "idle" — Tesla's scheduled EV-preserve
        # path (no set_no_discharge_mode primitive) also elevates the
        # reserve via _set_idle_hold_mode(preserve_charge=True) but records
        # _last_executed_action = "no_discharge", not "idle". Runs BEFORE
        # the EV no-discharge release below, which only restores work mode
        # and must not be assumed to have already handled the reserve.
        if not monitoring_mode and self._pre_idle_backup_reserve is not None:
            if self.battery_controller:
                await self._restore_pre_idle_backup_reserve(
                    self.battery_controller,
                    "optimizer disable",
                )
        elif monitoring_mode and self._pre_idle_backup_reserve is not None:
            _LOGGER.info(
                "Optimizer shutdown: monitoring mode active — skipping "
                "pre-IDLE backup reserve restore"
            )

        idle_work_mode_restore_failed = False

        # FoxESS/Sungrow: restore from IDLE hold mode to normal operation.
        # Stays gated on _last_executed_action == "idle" only — the EV
        # no-discharge path restores its own work mode via
        # _release_scheduled_ev_no_discharge_mode below, and widening this
        # gate would double-fire restore_work_mode_from_idle.
        if not monitoring_mode and self._last_executed_action == "idle":
            if getattr(self, "_idle_no_discharge_active", False):
                await self._restore_idle_no_discharge_mode("optimizer disable")
            elif (
                self.energy_coordinator
                and hasattr(self.energy_coordinator, "restore_work_mode_from_idle")
            ):
                try:
                    restored = await self._call_optimizer_energy_control(
                        "restore_work_mode_from_idle"
                    )
                    if self.battery_system == "solaredge" and restored is False:
                        idle_work_mode_restore_failed = True
                        _LOGGER.warning(
                            "Optimizer disable: SolarEdge work-mode restore was "
                            "not confirmed; retaining the IDLE action marker"
                        )
                    else:
                        _LOGGER.info(
                            "Optimizer disable: restored work mode from IDLE"
                        )
                except Exception as e:
                    if self.battery_system == "solaredge":
                        idle_work_mode_restore_failed = True
                    _LOGGER.warning("Failed to restore work mode on disable: %s", e)
        elif monitoring_mode and self._last_executed_action == "idle":
            _LOGGER.info(
                "Optimizer shutdown: monitoring mode active — skipping IDLE cleanup writes"
            )
        if self._scheduled_ev_no_discharge_active:
            if monitoring_mode:
                _LOGGER.info(
                    "Optimizer shutdown: monitoring mode active — skipping scheduled EV no-discharge release"
                )
            else:
                await self._release_scheduled_ev_no_discharge_mode("optimizer disabled")
        self._last_optimizer_self_consumption_reserve_target = None
        if not idle_work_mode_restore_failed:
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
                    "Optimizer shutdown: monitoring mode active — skipping executor restore writes"
                )
            await self._executor.stop(restore_normal=not monitoring_mode)

        if self._ev_coordinator:
            await self._ev_coordinator.stop()

        # Flush cost data to disk before shutdown
        await self._cost_store.async_save(self._cost_data_to_save())

        _LOGGER.info("Optimization disabled")

    async def _restore_idle_no_discharge_mode(self, context: str) -> bool:
        """Release a temporary IDLE discharge cap, preserving retry state."""
        if not getattr(self, "_idle_no_discharge_active", False):
            return True
        coordinator = self.energy_coordinator
        if not coordinator or not hasattr(coordinator, "restore_no_discharge_mode"):
            _LOGGER.warning(
                "%s: cannot release IDLE no-discharge mode; coordinator unavailable",
                context,
            )
            return False
        try:
            restored = await coordinator.restore_no_discharge_mode()
        except Exception as e:
            _LOGGER.warning(
                "%s: failed to release IDLE no-discharge mode: %s",
                context,
                e,
            )
            return False
        if restored is False:
            _LOGGER.warning("%s: failed to release IDLE no-discharge mode", context)
            return False
        self._idle_no_discharge_active = False
        _LOGGER.info("%s: released IDLE no-discharge mode", context)
        return True

    def _acquisition_reference_prices_for_run(
        self,
        lp_import_prices: list[float],
    ) -> list[float]:
        """Return real provider prices without synthetic LP tail padding.

        Dynamic providers commonly expose less than the configured 48-hour LP
        horizon.  The price builder repeats the final real slot so the solver
        still has a complete array.  Those copied future slots are useful to the
        LP, but they are not evidence about what energy already in the battery
        cost to acquire and must not dominate its fallback provenance proxy.
        """
        reference_prices = getattr(
            self,
            "_last_acquisition_reference_import_prices",
            None,
        )
        if reference_prices:
            return list(reference_prices)
        return list(lp_import_prices)

    def _acquisition_cost_for_run(
        self,
        *,
        import_prices: list[float],
        current_soc: float,
        capacity_wh: float,
    ) -> float:
        """Return the best supported acquisition cost for stored energy."""
        median_import_cost = (
            sorted(import_prices)[len(import_prices) // 2]
            if import_prices
            else 0.0
        )
        tracking_known = bool(
            getattr(self, "_grid_charge_tracking_known", False)
        )
        grid_charge_kwh = max(
            0.0,
            float(getattr(self, "_actual_grid_charge_kwh_today", 0.0) or 0.0),
        )
        grid_charge_cost = float(
            getattr(self, "_actual_grid_charge_cost_today", 0.0) or 0.0
        )
        measured_grid_unit_cost = (
            grid_charge_cost / grid_charge_kwh
            if tracking_known and grid_charge_kwh > 1e-6
            else None
        )

        current_stored_energy_kwh = (
            max(0.0, min(1.0, float(current_soc)))
            * max(0.0, float(capacity_wh))
            / 1000.0
        )
        proven_solar_candidates: list[float] = []
        summary_totals: tuple[float, float, float] | None = None

        def _finish(
            cost: float,
            source: str,
            **components: float,
        ) -> float:
            self._last_acquisition_cost_diagnostics = {
                "cost_kwh": round(float(cost), 8),
                "source": source,
                "reference_price_slots": len(import_prices),
                "reference_price_median_kwh": round(
                    float(median_import_cost), 8
                ),
                "tracking_known": tracking_known,
                "current_stored_energy_kwh": round(
                    current_stored_energy_kwh, 6
                ),
                "actual_charge_kwh_today": round(
                    float(getattr(self, "_actual_charge_kwh_today", 0.0) or 0.0),
                    6,
                ),
                "actual_discharge_kwh_today": round(
                    float(
                        getattr(self, "_actual_discharge_kwh_today", 0.0) or 0.0
                    ),
                    6,
                ),
                "actual_grid_charge_kwh_today": round(grid_charge_kwh, 6),
                "actual_grid_charge_cost_today": round(grid_charge_cost, 8),
                "measured_grid_unit_cost_kwh": (
                    round(measured_grid_unit_cost, 8)
                    if measured_grid_unit_cost is not None
                    else None
                ),
                "full_day_energy_summary": (
                    {
                        "charge_kwh": round(summary_totals[0], 6),
                        "discharge_kwh": round(summary_totals[1], 6),
                        "grid_import_kwh": round(summary_totals[2], 6),
                    }
                    if summary_totals is not None
                    else None
                ),
                "proven_solar_candidates_kwh": [
                    round(value, 6) for value in proven_solar_candidates
                ],
                **{
                    key: round(float(value), 6)
                    for key, value in components.items()
                },
            }
            return cost

        if tracking_known:
            # Known private counters are an independently authoritative lower
            # bound for the intervals they recorded.  Keep that candidate
            # separate from the main summary: either source may cover only a
            # partial day, and taking the maximum of lower bounds remains safe.
            total_charge_kwh = max(
                0.0,
                float(getattr(self, "_actual_charge_kwh_today", 0.0) or 0.0),
            )
            total_discharge_kwh = max(
                0.0,
                float(getattr(self, "_actual_discharge_kwh_today", 0.0) or 0.0),
            )
            proven_solar_candidates.append(
                max(
                    0.0,
                    total_charge_kwh - grid_charge_kwh - total_discharge_kwh,
                )
            )

        # If private same-day provenance is unavailable or incomplete (for
        # example after a reload or part-way through a day), use the main
        # coordinator's full-day totals. Site grid import is an upper bound on
        # possible grid charging, so subtracting it yields a conservative
        # lower bound on remaining solar-origin inventory. Keep the larger of
        # the independent lower bounds, then blend once below.
        summary_totals = self._full_day_battery_energy_summary()
        if summary_totals is not None:
            (
                total_charge_kwh,
                total_discharge_kwh,
                grid_import_kwh,
            ) = summary_totals
            proven_solar_candidates.append(
                max(
                    0.0,
                    total_charge_kwh - total_discharge_kwh - grid_import_kwh,
                )
            )

        if current_stored_energy_kwh > 0.1 and proven_solar_candidates:
            # Decompose the current inventory into the strongest proven solar
            # lower bound, measured grid-origin energy that can still fit in
            # the remainder, and unknown carry-over.  The counters are daily
            # totals, so a measured grid charge may already have been used or
            # discharged; cap that priced portion to the inventory still left
            # after the proven solar portion instead of pricing all storage at
            # the measured grid rate.
            proven_solar_kwh = min(
                current_stored_energy_kwh,
                max(proven_solar_candidates),
            )
            remaining_inventory_kwh = max(
                0.0,
                current_stored_energy_kwh - proven_solar_kwh,
            )
            measured_grid_kwh = min(
                remaining_inventory_kwh,
                grid_charge_kwh if measured_grid_unit_cost is not None else 0.0,
            )
            unknown_carry_over_kwh = max(
                0.0,
                remaining_inventory_kwh - measured_grid_kwh,
            )
            blended_cost = (
                measured_grid_kwh * (measured_grid_unit_cost or 0.0)
                + unknown_carry_over_kwh * median_import_cost
            ) / current_stored_energy_kwh
            return _finish(
                blended_cost,
                "blended_provenance",
                proven_solar_kwh=proven_solar_kwh,
                measured_grid_kwh=measured_grid_kwh,
                unknown_carry_over_kwh=unknown_carry_over_kwh,
            )

        # Keep the measured rate for a genuinely all-grid/no-solar inventory
        # (or when the live SOC is too small for a meaningful decomposition).
        if measured_grid_unit_cost is not None:
            return _finish(
                measured_grid_unit_cost,
                "measured_grid_charge",
                measured_grid_kwh=grid_charge_kwh,
            )

        # With no measured charge provenance for the current day, retain the
        # conservative proxy for energy that may have carried over overnight.
        return _finish(
            median_import_cost,
            "median_reference_fallback",
            unknown_carry_over_kwh=current_stored_energy_kwh,
        )

    def _optimizer_solve_debug_record(
        self,
        *,
        solve_timestamp: datetime,
        schedule_timestamps: list[datetime],
        acquisition_cost_kwh: float,
        effective_acquisition_costs: list[float],
        import_prices: list[float],
        export_prices: list[float],
        export_bonus_prices: list[float] | None,
        import_bonus_prices: list[float] | None,
        battery_export_allowed: list[bool],
        priority_export_slots: list[bool],
        hard_battery_charge_blocked: list[bool],
        profit_max_solar_export_slots: list[bool],
        battery_charge_blocked: list[bool],
        grid_charge_allowed: list[bool],
        result: OptimizerResult,
    ) -> dict[str, Any]:
        """Build one machine-readable DEBUG record for the final solve.

        Keep the large slot-level evidence out of entity attributes while making
        a bounded pair of adjacent DEBUG records sufficient to explain export
        plan creation or removal.
        """

        def _rounded(values: list[float] | None) -> list[float | None]:
            rounded: list[float | None] = []
            for raw in values or []:
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    rounded.append(None)
                    continue
                rounded.append(round(value, 8) if math.isfinite(value) else None)
            return rounded

        def _mask(values: list[bool] | None) -> list[int]:
            return [1 if bool(value) else 0 for value in values or []]

        schedule = getattr(result, "schedule", None)
        actions = getattr(schedule, "actions", None) or []
        effective_export_prices = [
            float(price or 0.0)
            + float(
                export_bonus_prices[idx]
                if export_bonus_prices and idx < len(export_bonus_prices)
                else 0.0
            )
            for idx, price in enumerate(export_prices)
        ]
        base_acquisition_blocked = [
            bool(battery_export_allowed[idx])
            and acquisition_cost_kwh > 0
            and idx < len(effective_acquisition_costs)
            and effective_export_prices[idx] < effective_acquisition_costs[idx]
            for idx in range(
                min(len(battery_export_allowed), len(effective_export_prices))
            )
        ]
        return {
            "schema": "powersync_optimizer_solve_v1",
            "solve_at": solve_timestamp.isoformat(),
            "slot_timestamps": [value.isoformat() for value in schedule_timestamps],
            "acquisition": {
                **dict(
                    getattr(self, "_last_acquisition_cost_diagnostics", {}) or {}
                ),
                "cost_kwh": round(float(acquisition_cost_kwh), 8),
                "reference_import_prices": _rounded(
                    getattr(
                        self,
                        "_last_acquisition_reference_import_prices",
                        None,
                    )
                ),
            },
            "lp_inputs": {
                "import_prices": _rounded(import_prices),
                "export_prices": _rounded(export_prices),
                "effective_export_prices": _rounded(effective_export_prices),
                "base_effective_acquisition_costs": _rounded(
                    effective_acquisition_costs
                ),
                "base_acquisition_blocked": _mask(base_acquisition_blocked),
                "export_bonus_prices": _rounded(export_bonus_prices),
                "import_bonus_prices": _rounded(import_bonus_prices),
                "battery_export_allowed": _mask(battery_export_allowed),
                "priority_export": _mask(priority_export_slots),
                "hard_battery_charge_blocked": _mask(
                    hard_battery_charge_blocked
                ),
                "profit_max_solar_export": _mask(
                    profit_max_solar_export_slots
                ),
                "combined_battery_charge_blocked": _mask(
                    battery_charge_blocked
                ),
                "grid_charge_allowed": _mask(grid_charge_allowed),
            },
            "result": {
                "solver": getattr(result, "solver_used", None),
                "feasible": bool(getattr(result, "feasible", False)),
                "objective_value": round(
                    float(getattr(result, "objective_value", 0.0) or 0.0), 8
                ),
                "battery_export_constraints": dict(
                    (getattr(result, "lp_stats", {}) or {}).get(
                        "battery_export_constraints", {}
                    )
                    or {}
                ),
                "actions": [getattr(action, "action", None) for action in actions],
                "battery_to_grid_w": _rounded(
                    getattr(result, "battery_to_grid_w", None)
                ),
                "grid_export_w": _rounded(
                    getattr(result, "grid_export_w", None)
                ),
            },
        }

    async def _async_load_external_energy_ledger(self) -> None:
        """Load the planning-only external-resource session ledger once."""
        if getattr(self, "_external_energy_ledger_loaded", False):
            return
        self._external_energy_ledger_loaded = True
        store = getattr(self, "_external_energy_ledger_store", None)
        if store is None or not hasattr(store, "async_load"):
            self._external_energy_ledger = ExternalEnergyLedgerState()
            return
        try:
            raw = await store.async_load()
            self._external_energy_ledger = ExternalEnergyLedgerState.from_dict(raw)
        except Exception:
            _LOGGER.exception(
                "External energy ledger could not be loaded; active sessions fail closed"
            )
            self._external_energy_ledger = ExternalEnergyLedgerState(
                corrupt=True,
                reason="ledger_load_failed",
            )

    def _external_energy_resource_config(
        self,
    ) -> ExternalEnergyResourceConfig | None:
        """Return the single generic planning-only backup/V2X resource."""
        try:
            usable_energy_wh = max(
                0,
                int(float(getattr(self._config, "backup_energy_wh", 0) or 0)),
            )
        except (TypeError, ValueError, OverflowError):
            return None
        if usable_energy_wh <= 0:
            return None
        try:
            max_power_w = max(
                0,
                int(
                    float(
                        getattr(self._config, "backup_energy_max_power_w", 0)
                        or 0
                    )
                ),
            )
        except (TypeError, ValueError, OverflowError):
            return None
        timezone_name = getattr(
            getattr(getattr(self, "hass", None), "config", None),
            "time_zone",
            "UTC",
        )
        config = ExternalEnergyResourceConfig(
            resource_id="configured_backup_v2x",
            usable_energy_wh=usable_energy_wh,
            max_power_w=max_power_w,
            start_local=getattr(
                self._config,
                "backup_energy_start",
                DEFAULT_OPTIMIZATION_BACKUP_ENERGY_START,
            ),
            end_local=getattr(
                self._config,
                "backup_energy_end",
                DEFAULT_OPTIMIZATION_BACKUP_ENERGY_END,
            ),
            timezone=timezone_name,
            config_entry_id=self.entry_id,
        )
        if config.validate() is not None:
            return None
        return config

    async def _apply_external_energy_plan(
        self,
        result: OptimizerResult,
        *,
        timestamps: list[datetime],
        native_home_load_w: list[float],
        solar_forecast_kw: list[float],
        avoided_import_prices: list[float],
        now: datetime,
    ) -> OptimizerResult:
        """Apply the constrained second-stage import-offset-only resource plan."""
        config = self._external_energy_resource_config()
        if config is None or not timestamps:
            self._last_external_energy_allocation = ExternalEnergyAllocationResult(
                reason="disabled"
            )
            return result

        await self._async_load_external_energy_ledger()
        interval = timedelta(minutes=max(1, int(self._config.interval_minutes or 5)))
        horizon_end = timestamps[-1] + interval
        raw_sessions = expand_external_energy_sessions(
            config,
            timestamps[0],
            horizon_end,
            slot_duration=interval,
        )
        ledger = getattr(
            self,
            "_external_energy_ledger",
            ExternalEnergyLedgerState(),
        )
        # First settle elapsed slots from the previous plan. This prevents every
        # rolling solve or restart from granting the active session a fresh budget.
        for session in raw_sessions:
            ledger = reduce_external_energy_ledger(session, ledger, now=now)
        sessions = resolve_external_energy_sessions(
            (config,),
            timestamps[0],
            horizon_end,
            slot_duration=interval,
            ledger=ledger,
        )
        n = min(
            len(timestamps),
            len(getattr(result, "grid_import_w", []) or []),
            len(native_home_load_w),
            len(solar_forecast_kw),
        )
        if n <= 0:
            self._last_external_energy_allocation = ExternalEnergyAllocationResult(
                reason="empty_horizon"
            )
            return result
        base_import_kw = [
            max(0.0, float(value or 0.0) / 1000.0)
            for value in result.grid_import_w[:n]
        ]
        base_export_kw = [
            max(0.0, float(value or 0.0) / 1000.0)
            for value in (getattr(result, "grid_export_w", []) or [])[:n]
        ]
        if len(base_export_kw) < n:
            base_export_kw.extend([0.0] * (n - len(base_export_kw)))
        native_deficit_kw = [
            max(
                0.0,
                float(native_home_load_w[idx] or 0.0) / 1000.0
                - max(0.0, float(solar_forecast_kw[idx] or 0.0)),
            )
            for idx in range(n)
        ]
        eligible_import_kw = [
            min(base_import_kw[idx], native_deficit_kw[idx])
            for idx in range(n)
        ]
        allocation = allocate_external_energy(
            sessions,
            eligible_native_home_import_kw=eligible_import_kw,
            avoided_import_price=avoided_import_prices[:n],
            slot_duration_hours=interval.total_seconds() / 3600.0,
            grid_import_without_resource_kw=base_import_kw,
            grid_export_without_resource_kw=base_export_kw,
        )
        result.grid_import_w[:n] = [
            round(value * 1000.0, 3)
            for value in allocation.grid_import_with_resource_kw
        ]
        avoided_cost = sum(
            max(0.0, before - after)
            * (interval.total_seconds() / 3600.0)
            * max(0.0, float(avoided_import_prices[idx] or 0.0))
            for idx, (before, after) in enumerate(
                zip(
                    allocation.grid_import_without_resource_kw,
                    allocation.grid_import_with_resource_kw,
                    strict=False,
                )
            )
            if idx < len(avoided_import_prices)
        )
        result.schedule.predicted_cost = round(
            float(result.schedule.predicted_cost or 0.0) - avoided_cost,
            2,
        )
        result.schedule.predicted_savings = round(
            float(result.schedule.predicted_savings or 0.0) + avoided_cost,
            2,
        )
        result.lp_stats["external_energy"] = {
            "planning_mode": "import_offset_only",
            "control_capability": "planning_assumption",
            "planned_energy_kwh": round(allocation.external_energy_kwh, 4),
            "avoided_import_cost": round(avoided_cost, 4),
        }
        plan_by_session = {plan.session_id: plan for plan in allocation.plans}
        for session in sessions:
            plan = plan_by_session.get(session.session_id)
            ledger = reduce_external_energy_ledger(
                session,
                ledger,
                now=now,
                planned_discharge_w=(
                    plan.planned_discharge_w if plan is not None else None
                ),
            )
        self._external_energy_ledger = ledger
        store = getattr(self, "_external_energy_ledger_store", None)
        if store is not None and hasattr(store, "async_save"):
            try:
                await store.async_save(ledger.to_dict())
            except Exception:
                _LOGGER.exception("External energy ledger save failed")
        self._last_external_energy_allocation = allocation
        return result

    async def _run_optimization(
        self,
        force: bool = False,
        *,
        execution_trigger: str | None = None,
    ) -> bool:
        """Run the built-in LP optimizer with current forecast data.

        When ``force`` is True (user-initiated re-optimization), queue behind
        any in-flight solve instead of skipping, so the request is never
        silently dropped.
        """
        if not self._optimizer or not self._enabled:
            return False
        if not self._energy_telemetry_ready():
            _LOGGER.info(
                "Optimizer: battery telemetry is not ready — skipping this run"
            )
            return False

        if not await self._wait_for_deferred_enable_restore():
            return False

        if await self._wait_for_restart_force_restore():
            return False

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
                return False
            _LOGGER.debug("Optimization in progress — queuing forced re-optimization")
        await self._optimization_lock.acquire()
        try:
            self._pending_price_timestamps = None

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

            # Resolve exactly once for this solve. Any closed cycle accepted
            # later in the run becomes eligible on the next solve, preventing
            # a mixture of old/new conversion values within one plan.
            solve_timestamp = dt_util.now()
            topology_changed = self._battery_efficiency_learner.ensure_topology(
                self._battery_efficiency_topology_fingerprint(
                    self._config.battery_capacity_wh
                )
            )
            if topology_changed:
                self._schedule_battery_efficiency_learning_save()
            resolved_parameters = (
                self._battery_efficiency_learner.resolved_parameters(
                    application_enabled=(
                        self.battery_efficiency_learning_enabled
                    ),
                    now=solve_timestamp,
                )
            )
            self._last_resolved_optimizer_parameters = resolved_parameters
            self._optimizer.apply_resolved_parameters(resolved_parameters)

            if self._ev_integration_enabled:
                await self._refresh_ev_forecast_inputs()

            # Collect forecast data
            self._last_export_boost_allowed_slots = []
            self._capture_provider_quota_measurements_before_plan()
            # Price builders keep their real, unpadded provider horizon in the
            # display series.  Preserve it before any provider quota mutation so
            # unknown stored-energy provenance is not valued from repeated LP
            # tail padding.
            self._last_acquisition_reference_import_prices = []
            prices = await self._get_price_forecast()
            if prices:
                self._last_acquisition_reference_import_prices = list(
                    getattr(self, "_last_display_import_prices", None) or []
                )
            solar = await self._get_solar_forecast()
            load = await self._get_load_forecast()
            # Preserve the canonical house-only series before any planned EV
            # overlay. The planning-only backup/V2X allowance may offset this
            # native-home import, never EV charging or battery charging.
            native_home_load_w = list(load or [])
            soc, capacity = await self._get_battery_state()
            self._observe_battery_efficiency(
                timestamp=solve_timestamp,
                soc=soc,
                capacity_wh=capacity,
            )

            # Overlay EV charging plan onto load forecast
            ev_peak_kw = 0.0
            self._pending_ev_charge_plan = None
            self._last_planned_ev_load_forecast_w = None
            self._last_effective_ev_load_forecast_w = None
            self._last_ev_charge_by_vehicle_w = None
            self._last_ev_charge_schedule_updated = None
            self._last_ev_source_by_vehicle_w = None
            self._last_ev_optimizer_policy = None
            self._last_smart_schedule_ev_load_w = None
            self._last_price_level_expected_ev_load_w = None
            self._last_price_level_projection = None
            if load:
                n_ev = len(load)
                price_projection = await self._build_price_level_projection(n_ev)
                smart_components = (
                    self._get_ev_planned_load_components(n_ev)
                    if self._ev_integration_enabled
                    else {}
                )
                external_ev_load_w = self._get_planned_ev_load_forecast(n_ev)
                zeros = [0.0] * n_ev

                if external_ev_load_w:
                    effective_ev_load_w = list(external_ev_load_w)
                    display_projection = price_projection.with_suppressed_expected(
                        suppressed_by="external_planned_load",
                        reason="External planned EV load is authoritative",
                    )
                    smart_w = zeros
                    price_expected_w = zeros
                    effective_source = "external"
                    if (
                        smart_components or any(price_projection.expected_w)
                    ) and not self._warned_dual_ev_overlay:
                        _LOGGER.warning(
                            "Optimizer: planned_ev_load_entity is authoritative; "
                            "ignoring internal Smart Schedule and Price-Level "
                            "expected load to avoid double-counting EV demand."
                        )
                        self._warned_dual_ev_overlay = True
                else:
                    (
                        effective_ev_load_w,
                        smart_w,
                        price_expected_w,
                    ) = self._merge_internal_ev_load_components(
                        n_intervals=n_ev,
                        smart_components=smart_components,
                        price_components=price_projection.expected_by_loadpoint,
                    )
                    display_projection = price_projection
                    external_ev_load_w = zeros
                    effective_source = "internal"

                self._last_smart_schedule_ev_load_w = list(smart_w)
                self._last_price_level_expected_ev_load_w = list(
                    price_expected_w
                )
                self._last_price_level_projection = (
                    self._price_level_projection_payload(
                        projection=display_projection,
                        effective_source=effective_source,
                        external_w=list(external_ev_load_w),
                        smart_w=list(smart_w),
                        price_expected_w=list(price_expected_w),
                    )
                )

                # The LP's ev_charge decision variable and this load overlay
                # model the same demand. Running both would count the car
                # twice, so they are mutually exclusive: prefer the decision
                # variable, which chooses timing against prices and the import
                # limit instead of fixing it to the planner's chosen windows.
                # An explicitly configured planned-load entity is already an
                # authoritative EV forecast. Do not also hand the internal
                # Smart Schedule demand to the LP: that would add a second EV
                # decision variable on top of the external load overlay.
                self._pending_ev_charge_plan = (
                    self._build_ev_charge_plan(self._price_timestamps(n_ev))
                    if effective_source == "internal"
                    else None
                )
                if self._pending_ev_charge_plan is not None:
                    _LOGGER.debug(
                        "EV load overlay: superseded by LP co-optimization "
                        "(%.1f kWh across %d vehicle(s))",
                        sum(
                            plan.energy_needed_kwh
                            for plan in self._pending_ev_charge_plan
                        ),
                        len(self._pending_ev_charge_plan),
                    )
                    effective_ev_load_w = zeros

                if any(value > 0 for value in effective_ev_load_w):
                    load = [
                        base + ev
                        for base, ev in zip(load, effective_ev_load_w)
                    ]
                    self._last_planned_ev_load_forecast_w = list(
                        effective_ev_load_w
                    )
                    self._last_effective_ev_load_forecast_w = list(
                        effective_ev_load_w
                    )
                    ev_peak_kw = max(
                        ev_peak_kw, max(effective_ev_load_w) / 1000
                    )
                elif (
                    smart_components
                    and not self._warned_dual_ev_overlay
                    and self._planned_ev_load_entity_id
                ):
                    _LOGGER.warning(
                        "Optimizer: planned_ev_load_entity has no usable future "
                        "values; using the internal EV plan for this solve."
                    )
                    self._warned_dual_ev_overlay = True

            import_prices = prices[0] if prices else []
            export_prices = prices[1] if prices else []

            # Convert forecasts from Watts (forecaster output) to kW (LP input)
            solar_forecast = [v / 1000.0 for v in solar] if solar else []
            load_forecast = [v / 1000.0 for v in load] if load else []
            raw_solar_forecast = list(solar_forecast)

            if solar_forecast:
                self._observe_solar_forecast_accuracy(solar_forecast, soc)
                solar_forecast = self._apply_solar_nowcast_derate(solar_forecast, soc)
            adjusted_solar_forecast = list(solar_forecast)

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
                    from ..curtailment_config import get_curtailment_price_thresholds

                    # Curtailment activates below the configured export value
                    # when the battery is full, matching runtime curtailment.
                    # While battery has room, solar charges it (no curtailment).
                    # Use forward SOC projection to estimate when battery fills.
                    curtail_threshold_cents, _ = get_curtailment_price_thresholds(
                        self._entry
                    )
                    curtail_threshold = curtail_threshold_cents / 100.0
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

            solver_solar_forecast = list(solar_forecast)

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

            # Use measured grid cost for grid-charged energy, blend measured
            # solar charging with conservative unknown carry-over, and use the
            # median import proxy when today's provenance is unavailable.
            acq_cost = self._acquisition_cost_for_run(
                import_prices=self._acquisition_reference_prices_for_run(
                    import_prices
                ),
                current_soc=soc,
                capacity_wh=capacity,
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
            forecast_source = getattr(
                self._solar_forecaster, "last_forecast_source", None
            )
            learned_margin_kwh = self._solar_forecast_learner.allowance_kwh(
                forecast_source
            )
            learned_margin_kwh, nowcast_allowance_kwh = (
                self._solar_error_margin_after_nowcast(
                    learned_margin_kwh=learned_margin_kwh,
                    raw_solar_forecast=raw_solar_forecast,
                    adjusted_solar_forecast=solar_forecast,
                    deadline_slot=_target_slot,
                    interval_minutes=self._config.interval_minutes,
                )
            )
            self._last_solar_nowcast_allowance_kwh = nowcast_allowance_kwh
            self._last_solar_effective_error_margin_kwh = learned_margin_kwh
            self._optimizer.pre_window_solar_error_margin_kwh = learned_margin_kwh
            self._optimizer.pre_window_solar_learning_confidence = (
                self._solar_forecast_learner.confidence(forecast_source)
            )
            # Cost Neutral accounts using billable tariff rates. The price
            # builder keeps those separately from export boosts, saving-session
            # overlays, demand penalties, and confidence decay used only by the
            # LP objective.
            (
                cost_neutral_import_prices,
                cost_neutral_export_prices,
            ) = self._cost_neutral_settlement_prices(
                import_prices,
                export_prices,
            )
            self._apply_provider_quota_optimizer_inputs(import_prices, export_prices)
            battery_export_allowed = self._battery_export_allowed_slots(
                len(import_prices),
                cost_neutral_export_prices,
            )
            battery_charge_blocked = self._battery_charge_blocked_slots(
                len(import_prices),
            )
            hard_battery_charge_blocked = list(battery_charge_blocked)
            grid_charge_cap_import_prices = self._grid_charge_cap_import_prices(
                import_prices
            )
            grid_charge_allowed = self._grid_charge_allowed_slots(
                grid_charge_cap_import_prices,
                solar_forecast,
                load_forecast,
                soc,
            )
            grid_charge_allowed = self._apply_custom_tariff_quota_grid_charge_limit(
                grid_charge_allowed,
                solar_forecast,
                load_forecast,
            )
            price_cap = self._coerce_optional_price(
                self._config.max_grid_charge_price
            )
            eligible_grid_charge_slots = (
                sum(bool(slot) for slot in grid_charge_allowed)
                if self._config.allow_grid_charge
                else 0
            )
            _LOGGER.debug(
                "Grid charge eligibility: %d/%d slots allowed "
                "(enabled=%s, max_price=%s, soc_cap=%.0f%%)",
                eligible_grid_charge_slots,
                len(grid_charge_allowed),
                self._config.allow_grid_charge,
                (
                    f"{price_cap * 100:.1f}c/kWh"
                    if price_cap is not None
                    else "disabled"
                ),
                self._soc_ratio(self._config.grid_charge_soc_cap, 1.0) * 100,
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
            manual_control_projection = build_manual_control_projection(
                self._get_active_force_state(),
                schedule_timestamps,
                current_soc=soc,
                capacity_wh=capacity,
                max_charge_w=self._config.max_charge_w,
                max_discharge_w=self._config.max_discharge_w,
                hardware_reserve=getattr(
                    self._optimizer,
                    "hardware_reserve",
                    self._config.backup_reserve,
                ),
                efficiency=getattr(self._optimizer, "efficiency", 1.0),
                interval_minutes=self._config.interval_minutes,
            )
            self._last_manual_control_projection = (
                manual_control_projection.status_payload()
                if manual_control_projection is not None
                else {"active": False}
            )
            manual_control_payload = (
                manual_control_projection.optimizer_payload()
                if manual_control_projection is not None
                else None
            )
            grid_export_limits_w: list[float | None] | None = None
            published_grid_export_limits_w: list[float | None] | None = None
            network_manager = self.hass.data.get("power_sync", {}).get(
                self.entry_id, {}
            ).get("network_envelope_manager")
            network_snapshot = (
                network_manager.snapshot if network_manager is not None else None
            )
            if network_snapshot is not None and network_snapshot.mode != "off":
                from ..network_envelope import optimizer_slot_limits

                published_grid_export_limits_w = optimizer_slot_limits(
                    network_snapshot,
                    schedule_timestamps,
                    self._config.interval_minutes,
                )
                if network_snapshot.mode == "monitoring":
                    # Monitoring is deny-only: expose the certified envelope but
                    # do not let PowerSync intentionally schedule battery export.
                    battery_export_allowed = [False] * len(import_prices)
                elif network_snapshot.active_export_permitted:
                    grid_export_limits_w = published_grid_export_limits_w
                else:
                    # Active mode fails closed while any arming gate is false.
                    battery_export_allowed = [False] * len(import_prices)
                    grid_export_limits_w = [0.0] * len(import_prices)
                    published_grid_export_limits_w = grid_export_limits_w
            self._last_grid_export_limits_w = published_grid_export_limits_w
            profit_max_solar_export_slots = self._profit_max_solar_export_slots(
                import_prices,
                export_prices,
                solar_forecast,
                load_forecast,
                soc,
                hard_battery_charge_blocked,
                grid_charge_allowed,
                grid_export_limits_w,
            )
            # Preserve provider-owned blocks as the hard mask and add Profit
            # Max holds as a distinct reason mask. Hard blocks are never
            # relabelled as solar-export intent.
            profit_max_solar_export_slots = [
                bool(profit) and not bool(hard)
                for profit, hard in zip(
                    profit_max_solar_export_slots,
                    hard_battery_charge_blocked,
                    strict=False,
                )
            ]
            battery_charge_blocked = [
                bool(hard) or bool(profit)
                for hard, profit in zip(
                    hard_battery_charge_blocked,
                    profit_max_solar_export_slots,
                    strict=False,
                )
            ]
            self._last_profit_max_solar_export_slots = list(
                profit_max_solar_export_slots
            )
            spread_import_blocked = [
                bool(blocked) or not bool(allowed)
                for blocked, allowed in zip(
                    battery_charge_blocked,
                    grid_charge_allowed,
                    strict=False,
                )
            ]
            priority_export_slots = self._priority_export_slots_for_run(
                len(import_prices),
                cost_neutral_export_prices,
            )
            # Keep the exact fresh-solve masks used by execution commitment
            # checks. Recomputing these later can observe a different quota or
            # provider state from the schedule currently being executed.
            self._last_battery_export_allowed_slots = list(
                battery_export_allowed
            )
            self._last_priority_export_slots = list(priority_export_slots)
            spread_export_prices = self._spread_export_prices_for_run(
                export_prices,
            )
            quota_group_setter = getattr(
                self._optimizer,
                "set_quota_bonus_groups",
                None,
            )
            if callable(quota_group_setter):
                quota_group_setter(
                    import_group_ids=self._last_import_bonus_group_ids,
                    import_caps_by_group=self._last_import_bonus_caps_by_group,
                    export_group_ids=self._last_export_bonus_group_ids,
                    export_caps_by_group=self._last_export_bonus_caps_by_group,
                )

            cost_neutral_plan, cost_neutral_status = (
                self._cost_neutral_solve_inputs(
                    now=dt_util.now(),
                    timestamps=schedule_timestamps,
                    import_prices=cost_neutral_import_prices,
                    export_prices=cost_neutral_export_prices,
                    export_bonus_prices=self._last_zerohero_bonus_prices,
                    export_bonus_cap_kwh=self._last_zerohero_bonus_cap_kwh,
                    import_bonus_prices=self._last_zerocharge_bonus_prices,
                    import_bonus_cap_kwh=self._last_zerocharge_bonus_cap_kwh,
                    solar=solar_forecast,
                    load=load_forecast,
                    current_soc=soc,
                )
            )
            self._cost_neutral_status = cost_neutral_status
            current_cost_neutral_day = (
                cost_neutral_plan.current_day
                if cost_neutral_plan is not None
                else None
            )
            cost_neutral_cap = (
                cost_neutral_plan.earnings_caps_by_day.get(
                    current_cost_neutral_day,
                    0.0,
                )
                if cost_neutral_plan is not None
                and current_cost_neutral_day is not None
                else None
            )
            cost_neutral_slots = (
                [day is not None for day in cost_neutral_plan.day_ids]
                if cost_neutral_plan is not None
                else [False] * len(schedule_timestamps)
            )
            cost_neutral_forecast_import_cost = (
                cost_neutral_plan.forecast_import_costs_by_day.get(
                    current_cost_neutral_day,
                    0.0,
                )
                if cost_neutral_plan is not None
                and current_cost_neutral_day is not None
                else 0.0
            )
            cost_neutral_fixed_cost_allowance = (
                cost_neutral_plan.fixed_cost_allowances_by_day.get(
                    current_cost_neutral_day,
                    0.0,
                )
                if cost_neutral_plan is not None
                and current_cost_neutral_day is not None
                else None
            )

            reference_reserve_floor = (
                self._reserve_ratio(self._config.backup_reserve, 0.0) or 0.0
            )
            if self.auto_apply_reserve_enabled:
                reference_reserve_floor = (
                    self._reserve_ratio(
                        getattr(self, "_manual_backup_reserve", None),
                        reference_reserve_floor,
                    )
                    or 0.0
                )
            solve_reserve_override = (
                reference_reserve_floor
                if self.auto_apply_reserve_enabled
                and not math.isclose(
                    reference_reserve_floor,
                    self._config.backup_reserve,
                    abs_tol=0.0001,
                )
                else None
            )

            ev_charge_plan = getattr(self, "_pending_ev_charge_plan", None)
            if ev_charge_plan:
                _LOGGER.debug(
                    "EV demand in this solve: %s",
                    ", ".join(
                        f"{plan.energy_needed_kwh:.1f} kWh for {plan.vehicle_id}"
                        for plan in ev_charge_plan
                    ),
                )

            async def _run_optimizer_once(
                reserve_floor: float | None = None,
                export_reserve_floor: float | list[float] | None = None,
                charge_blocked_slots: list[bool] | None = None,
                solar_export_slots: list[bool] | None = None,
                grid_charge_slots: list[bool] | None = None,
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
                        charge_blocked_slots or battery_charge_blocked,
                        self._config.allow_grid_charge,
                        grid_charge_slots or grid_charge_allowed,
                        self._last_zerohero_bonus_prices,
                        self._last_zerohero_bonus_cap_kwh,
                        self._last_zerocharge_bonus_prices,
                        self._last_zerocharge_bonus_cap_kwh,
                        export_reserve_floor,
                        schedule_timestamps,
                        priority_export_slots,
                        any(priority_export_slots),
                        self._should_disable_idle_schedule(),
                        grid_export_limits_w,
                        bool(
                            self._last_import_bonus_group_ids
                            or self._last_export_bonus_group_ids
                        ),
                        cost_neutral_cap,
                        cost_neutral_slots,
                        cost_neutral_forecast_import_cost,
                        cost_neutral_fixed_cost_allowance,
                        cost_neutral_plan,
                        solar_export_slots or profit_max_solar_export_slots,
                        manual_control_payload,
                        ev_charge_plan,
                    )
                finally:
                    if reserve_floor is not None:
                        self._optimizer.update_config(
                            backup_reserve=self._config.backup_reserve
                        )

            # Run LP in executor thread to avoid blocking event loop
            result: OptimizerResult = await _run_optimizer_once(
                solve_reserve_override
            )
            if not result.feasible and any(profit_max_solar_export_slots):
                _LOGGER.warning(
                    "Profit Max solar-export hold made the solve infeasible; "
                    "retrying with provider charge blocks only"
                )
                profit_max_solar_export_slots = [False] * len(import_prices)
                battery_charge_blocked = list(hard_battery_charge_blocked)
                self._last_profit_max_solar_export_slots = list(
                    profit_max_solar_export_slots
                )
                result = await _run_optimizer_once(
                    solve_reserve_override,
                    charge_blocked_slots=hard_battery_charge_blocked,
                    solar_export_slots=[False] * len(import_prices),
                )
            self._charge_by_time_blackout_diagnostic = None
            blackout_slots = list(
                getattr(self, "_last_grid_charge_blackout_slots", []) or []
            )
            target_slot = getattr(self._optimizer, "pre_window_slot", None)
            affected_before_deadline = [
                idx
                for idx, blocked in enumerate(blackout_slots)
                if blocked and (target_slot is None or idx <= target_slot)
            ]
            if (
                not result.feasible
                and self.charge_by_time_enabled
                and affected_before_deadline
            ):
                counterfactual = await _run_optimizer_once(
                    solve_reserve_override,
                    grid_charge_slots=list(
                        getattr(
                            self,
                            "_last_grid_charge_pre_blackout_allowed",
                            grid_charge_allowed,
                        )
                    ),
                )
                if counterfactual.feasible:
                    self._charge_by_time_blackout_diagnostic = {
                        "reason": "charge_by_time_blackout_infeasible",
                        "target_soc": int(round(self._charge_by_time_target_soc() * 100)),
                        "deadline": self._config.charge_by_time_target_time,
                        "affected_slots": affected_before_deadline,
                        "affected_windows": self._grid_charge_blackout_windows(),
                        "eligible_slots_before_blackout": sum(
                            bool(slot)
                            for slot in getattr(
                                self,
                                "_last_grid_charge_pre_blackout_allowed",
                                [],
                            )
                        ),
                        "eligible_slots": sum(bool(slot) for slot in grid_charge_allowed),
                    }
                    _LOGGER.warning(
                        "Charge By Time is infeasible because configured grid-charge "
                        "blackout windows remove %d eligible slot(s) before %s",
                        len(affected_before_deadline),
                        self._config.charge_by_time_target_time,
                    )
            if self._charge_by_time_blackout_diagnostic:
                result.lp_stats["charge_by_time_blackout_infeasible"] = dict(
                    self._charge_by_time_blackout_diagnostic
                )
            result.lp_stats["grid_charge_blackout"] = dict(
                getattr(self, "_grid_charge_blackout_status", {}) or {}
            )
            if result.feasible and any(profit_max_solar_export_slots):
                revised_solar_export_slots = self._revise_solar_export_holds(
                    result,
                    import_prices,
                    export_prices,
                    profit_max_solar_export_slots,
                )
                if revised_solar_export_slots != profit_max_solar_export_slots:
                    dropped = sum(profit_max_solar_export_slots) - sum(
                        revised_solar_export_slots
                    )
                    revised_charge_blocked = [
                        bool(hard) or bool(profit)
                        for hard, profit in zip(
                            hard_battery_charge_blocked,
                            revised_solar_export_slots,
                            strict=False,
                        )
                    ]
                    revised_result = await _run_optimizer_once(
                        solve_reserve_override,
                        charge_blocked_slots=revised_charge_blocked,
                        solar_export_slots=revised_solar_export_slots,
                    )
                    if revised_result.feasible:
                        # INFO, not WARNING: the selector re-proposes the same
                        # holds every cycle, so an affected site would log this
                        # on every solve. The plan it emits is already correct.
                        _LOGGER.info(
                            "Profit Max solar-export hold would be repaid by "
                            "costlier grid charging; dropped %s of %s held "
                            "slots and re-solved",
                            dropped,
                            sum(profit_max_solar_export_slots),
                        )
                        revision_reason = (
                            "grid_replenishment_costlier_than_export"
                        )
                        previous_status = (
                            getattr(self, "_solar_export_capability_status", {})
                            or {}
                        )
                        current_slot_status = previous_status.get("current_slot")
                        if (
                            isinstance(current_slot_status, dict)
                            and revised_solar_export_slots
                            and not revised_solar_export_slots[0]
                        ):
                            current_slot_status = {
                                **current_slot_status,
                                "selected": False,
                                "reason": revision_reason,
                            }
                        self._solar_export_capability_status = {
                            **previous_status,
                            "post_solve_revision": {
                                "reason": revision_reason,
                                "dropped_slots": dropped,
                                "retained_slots": sum(revised_solar_export_slots),
                            },
                            "selected_slots": sum(revised_solar_export_slots),
                            "current_slot": current_slot_status,
                        }
                        profit_max_solar_export_slots = revised_solar_export_slots
                        battery_charge_blocked = revised_charge_blocked
                        self._last_profit_max_solar_export_slots = list(
                            profit_max_solar_export_slots
                        )
                        spread_import_blocked = [
                            bool(blocked) or not bool(allowed)
                            for blocked, allowed in zip(
                                battery_charge_blocked,
                                grid_charge_allowed,
                                strict=False,
                            )
                        ]
                        result = revised_result
                    else:
                        # Relaxing charge blocks cannot shrink the feasible
                        # set, so this is defensive only: never trade a
                        # working plan for an infeasible one.
                        _LOGGER.warning(
                            "Profit Max solar-export hold revision was "
                            "infeasible; keeping the original solve"
                        )
            used_reference_override = solve_reserve_override is not None

            schedule = result.schedule
            if self._should_spread_import_schedule():
                schedule = self._spread_import_schedule(
                    schedule,
                    import_prices,
                    spread_import_blocked,
                    soc,
                    solar_forecast=solar_forecast,
                    load_forecast=load_forecast,
                )
            reference_export_windows = self._reference_export_bridge_windows(
                schedule,
                battery_export_allowed,
                priority_export_slots,
            )
            post_solve_export_floor = self._merge_export_protection_floors(
                reference_reserve_floor,
                result.future_export_protection_floor_slots,
                len(schedule.actions or []),
            )
            if self._should_spread_export_schedule():
                schedule = self._spread_export_schedule(
                    schedule,
                    battery_export_allowed,
                    export_reserve_floor=post_solve_export_floor,
                    export_prices=spread_export_prices,
                )
            schedule = self._bridge_short_export_gaps(
                schedule,
                cost_neutral_export_prices,
                authoritative_reserve_floor=post_solve_export_floor,
            )
            self._last_update_time = dt_util.now()

            # Apply off-grid curtailment overlay if enabled — converts
            # eligible SELF_CONSUMPTION/IDLE slots to OFF_GRID during
            # negative export price periods.
            if self._should_apply_offgrid_overlay():
                schedule = self._apply_offgrid_overlay(
                    schedule, export_prices,
                )
            schedule = self._annotate_manual_control_schedule(
                schedule,
                manual_control_projection,
            )
            result = self._optimizer.reconcile_result_with_schedule(
                result,
                schedule,
                import_prices=import_prices,
                export_prices=export_prices,
                solar=solar_forecast,
                load=load_forecast,
                export_bonus_prices=self._last_zerohero_bonus_prices,
                export_bonus_cap_kwh=self._last_zerohero_bonus_cap_kwh,
                import_bonus_prices=self._last_zerocharge_bonus_prices,
                import_bonus_cap_kwh=self._last_zerocharge_bonus_cap_kwh,
                initial_soc=soc,
                cost_neutral_earnings_cap=cost_neutral_cap,
                cost_neutral_slots=cost_neutral_slots,
                cost_neutral_forecast_import_cost=(
                    cost_neutral_forecast_import_cost
                ),
                cost_neutral_fixed_cost_allowance=(
                    cost_neutral_fixed_cost_allowance
                ),
                cost_neutral_plan=cost_neutral_plan,
            )
            self._restore_bridged_export_gap_provenance(
                schedule,
                result.schedule,
            )
            self._annotate_manual_control_schedule(
                result.schedule,
                manual_control_projection,
            )
            result.lp_stats["manual_control_projection_slots"] = (
                manual_control_projection.slot_count
                if manual_control_projection is not None
                else 0
            )
            self._set_forecast_bridge_reserve_recommendation(
                result,
                reference_export_windows,
                solar_forecast,
                load_forecast,
            )
            reserve_semantic_keys = (
                "manual_optimizer_reserve_percent",
                "suggested_optimizer_reserve_percent",
                "needs_optimizer_reserve_raise",
                "forecast_bridge_kwh",
                "forecast_bridge_reserve_percent",
                "forecast_bridge_export_window_start",
                "forecast_bridge_export_window_end",
                "forecast_bridge_boundary_source",
                "protects_until",
                "next_charge_reason",
            )
            reference_reserve_semantics = {
                key: value
                for key, value in dict(
                    getattr(result, "reserve_recommendation", {}) or {}
                ).items()
                if key in reserve_semantic_keys
            }
            self._current_schedule = result.schedule
            self._last_optimizer_result = result
            self._adopt_solved_ev_series(result)
            self._commit_price_forecast_cache(import_prices, export_prices)

            reserve_changed = self._apply_auto_reserve_recommendation(result)
            if reserve_changed or used_reference_override:
                result = await _run_optimizer_once()
                applied_reserve_floor = (
                    self._reserve_ratio(self._config.backup_reserve, 0.0) or 0.0
                )
                schedule = result.schedule
                post_solve_export_floor = self._merge_export_protection_floors(
                    applied_reserve_floor,
                    result.future_export_protection_floor_slots,
                    len(schedule.actions or []),
                )
                if self._should_spread_import_schedule():
                    schedule = self._spread_import_schedule(
                        schedule,
                        import_prices,
                        spread_import_blocked,
                        soc,
                        solar_forecast=solar_forecast,
                        load_forecast=load_forecast,
                    )
                if self._should_spread_export_schedule():
                    schedule = self._spread_export_schedule(
                        schedule,
                        battery_export_allowed,
                        export_reserve_floor=post_solve_export_floor,
                        export_prices=spread_export_prices,
                    )
                schedule = self._bridge_short_export_gaps(
                    schedule,
                    cost_neutral_export_prices,
                    authoritative_reserve_floor=post_solve_export_floor,
                )
                if self._should_apply_offgrid_overlay():
                    schedule = self._apply_offgrid_overlay(
                        schedule, export_prices,
                    )
                schedule = self._annotate_manual_control_schedule(
                    schedule,
                    manual_control_projection,
                )
                result = self._optimizer.reconcile_result_with_schedule(
                    result,
                    schedule,
                    import_prices=import_prices,
                    export_prices=export_prices,
                    solar=solar_forecast,
                    load=load_forecast,
                    export_bonus_prices=self._last_zerohero_bonus_prices,
                    export_bonus_cap_kwh=self._last_zerohero_bonus_cap_kwh,
                    import_bonus_prices=self._last_zerocharge_bonus_prices,
                    import_bonus_cap_kwh=self._last_zerocharge_bonus_cap_kwh,
                    initial_soc=soc,
                    cost_neutral_earnings_cap=cost_neutral_cap,
                    cost_neutral_slots=cost_neutral_slots,
                    cost_neutral_forecast_import_cost=(
                        cost_neutral_forecast_import_cost
                    ),
                    cost_neutral_fixed_cost_allowance=(
                        cost_neutral_fixed_cost_allowance
                    ),
                    cost_neutral_plan=cost_neutral_plan,
                )
                self._restore_bridged_export_gap_provenance(
                    schedule,
                    result.schedule,
                )
                self._annotate_manual_control_schedule(
                    result.schedule,
                    manual_control_projection,
                )
                result.lp_stats["manual_control_projection_slots"] = (
                    manual_control_projection.slot_count
                    if manual_control_projection is not None
                    else 0
                )
                final_recommendation = dict(
                    getattr(result, "reserve_recommendation", {}) or {}
                )
                for key in reserve_semantic_keys:
                    final_recommendation.pop(key, None)
                final_recommendation.update(reference_reserve_semantics)
                final_recommendation["auto_apply_enabled"] = bool(
                    self.auto_apply_reserve_enabled
                )
                final_recommendation["applied_optimizer_reserve_percent"] = int(
                    round(applied_reserve_floor * 100)
                )
                result.reserve_recommendation = final_recommendation
                self._current_schedule = result.schedule
                self._last_optimizer_result = result
                self._adopt_solved_ev_series(result)
                self._last_update_time = dt_util.now()
            result = await self._apply_external_energy_plan(
                result,
                timestamps=schedule_timestamps,
                native_home_load_w=native_home_load_w,
                solar_forecast_kw=solar_forecast,
                avoided_import_prices=cost_neutral_import_prices,
                now=solve_timestamp,
            )
            self._current_schedule = result.schedule
            self._last_optimizer_result = result
            if _LOGGER.isEnabledFor(logging.DEBUG):
                solve_debug_record = self._optimizer_solve_debug_record(
                    solve_timestamp=solve_timestamp,
                    schedule_timestamps=schedule_timestamps,
                    acquisition_cost_kwh=acq_cost,
                    effective_acquisition_costs=(
                        self._optimizer._effective_export_acquisition_costs(
                            len(import_prices),
                            import_prices,
                            battery_charge_blocked,
                            self._config.allow_grid_charge,
                            acq_cost,
                            grid_charge_allowed,
                        )
                    ),
                    import_prices=import_prices,
                    export_prices=export_prices,
                    export_bonus_prices=self._last_zerohero_bonus_prices,
                    import_bonus_prices=self._last_zerocharge_bonus_prices,
                    battery_export_allowed=battery_export_allowed,
                    priority_export_slots=priority_export_slots,
                    hard_battery_charge_blocked=hard_battery_charge_blocked,
                    profit_max_solar_export_slots=profit_max_solar_export_slots,
                    battery_charge_blocked=battery_charge_blocked,
                    grid_charge_allowed=grid_charge_allowed,
                    result=result,
                )
                _LOGGER.debug(
                    "Optimizer solve diagnostic: %s",
                    json.dumps(
                        solve_debug_record,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            if cost_neutral_plan is not None:
                effective_caps = {
                    str(day): max(0.0, float(value))
                    for day, value in dict(
                        result.lp_stats.get(
                            "cost_neutral_earnings_caps_by_day",
                            cost_neutral_plan.earnings_caps_by_day,
                        )
                        or {}
                    ).items()
                }
                planned_by_day = {
                    str(day): max(0.0, float(value))
                    for day, value in dict(
                        result.lp_stats.get(
                            "cost_neutral_planned_earnings_by_day",
                            {},
                        )
                        or {}
                    ).items()
                }
                days_status = {
                    str(day): dict(value)
                    for day, value in dict(
                        cost_neutral_status.get("days", {})
                    ).items()
                }
                cost_neutral_export_bonus_prices = (
                    self._last_zerohero_bonus_prices or []
                )
                for day in cost_neutral_plan.earnings_caps_by_day:
                    effective_cap = effective_caps.get(day, 0.0)
                    planned = planned_by_day.get(day, 0.0)
                    uncovered = max(0.0, effective_cap - planned)
                    eligible_indices = [
                        idx
                        for idx, slot_day in enumerate(cost_neutral_plan.day_ids)
                        if slot_day == day
                        and idx < len(battery_export_allowed)
                        and bool(battery_export_allowed[idx])
                        and idx < len(cost_neutral_export_prices)
                        and (
                            float(cost_neutral_export_prices[idx] or 0.0)
                            + float(
                                cost_neutral_export_bonus_prices[idx]
                                if idx < len(cost_neutral_export_bonus_prices)
                                else 0.0
                            )
                        )
                        > 0.0
                    ]
                    blocking_reasons: list[str] = []
                    if effective_cap <= 1e-6:
                        reason = "already_covered_by_measured_or_natural_export"
                    elif not eligible_indices:
                        reason = (
                            "no_eligible_export_slots_before_midnight"
                            if day == current_cost_neutral_day
                            else "no_eligible_export_slots_for_local_day"
                        )
                    elif grid_export_limits_w is not None and all(
                        idx >= len(grid_export_limits_w)
                        or (
                            grid_export_limits_w[idx] is not None
                            and float(grid_export_limits_w[idx] or 0.0) <= 0.0
                        )
                        for idx in eligible_indices
                    ):
                        reason = "site_or_network_export_limit"
                        blocking_reasons.append("site_or_network_export_limit")
                    elif (
                        soc <= reference_reserve_floor + 1e-6
                        and planned <= 1e-6
                    ):
                        reason = "battery_or_reserve_limit"
                        blocking_reasons.append("reserve_floor")
                    elif uncovered <= 0.005:
                        reason = "covered"
                    else:
                        reason = "insufficient_eligible_capacity"
                        if self.charge_by_time_enabled:
                            blocking_reasons.append("charge_by_time_requirement")
                        if self._should_spread_export_schedule():
                            blocking_reasons.append("spread_export_limit")
                    days_status[day] = {
                        **days_status.get(day, {"local_date": day}),
                        "battery_export_earnings_cap": round(effective_cap, 4),
                        "planned_battery_export_earnings": round(planned, 4),
                        "uncovered_amount": round(uncovered, 4),
                        "projected_net_daily_cost": round(uncovered, 4),
                        "reason": reason,
                        "blocking_reasons": blocking_reasons,
                    }

                current_status = days_status.get(
                    current_cost_neutral_day or "",
                    {},
                )
                self._cost_neutral_status = {
                    **cost_neutral_status,
                    "days": days_status,
                    "battery_export_earnings_cap": current_status.get(
                        "battery_export_earnings_cap",
                        0.0,
                    ),
                    "planned_battery_export_earnings": current_status.get(
                        "planned_battery_export_earnings",
                        0.0,
                    ),
                    "uncovered_amount": current_status.get(
                        "uncovered_amount",
                        0.0,
                    ),
                    "projected_net_daily_cost": current_status.get(
                        "projected_net_daily_cost",
                        0.0,
                    ),
                    "reason": current_status.get(
                        "reason",
                        cost_neutral_status.get("reason", "no_forecast_days"),
                    ),
                    "blocking_reasons": current_status.get(
                        "blocking_reasons",
                        [],
                    ),
                }
            self._set_active_export_reserve_floor_slots(None, None)

            # Store forecast data for LP forecast sensors. A current provider
            # can legitimately forecast zero generation throughout the
            # requested horizon, so availability must follow the selected
            # source rather than positive wattage.
            self._record_solar_forecast_availability(solar_forecast)
            self._last_solar_forecast = solar_forecast
            self._last_load_forecast = load_forecast
            provenance = derive_solar_forecast_provenance(
                raw_solar_forecast,
                adjusted_solar_forecast,
                solver_solar_forecast,
                getattr(result, "solar_curtailment_w", None),
            )
            if provenance is None:
                self._last_raw_solar_forecast = None
                self._last_planned_solar_forecast = None
                self._last_solar_curtailment_forecast = None
            else:
                self._last_raw_solar_forecast = (
                    provenance.raw_forecast_values_kw
                )
                self._last_planned_solar_forecast = (
                    provenance.planned_forecast_values_kw
                )
                self._last_solar_curtailment_forecast = (
                    provenance.curtailment_values_kw
                )

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
            await self._execute_current_action_and_publish(
                current_action,
                execution_trigger=execution_trigger,
            )
            return True

        except Exception as e:
            _LOGGER.error("Optimization failed: %s", e, exc_info=True)
            return False
        finally:
            self._pending_price_timestamps = None
            self._optimization_lock.release()

    async def _execute_current_action_and_publish(
        self,
        current_action: Any | None,
        *,
        execution_trigger: str | None = None,
    ) -> None:
        """Execute a solved action and publish its effective hardware status."""
        try:
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
                    await self._execute_optimizer_action(
                        current_action,
                        execution_trigger=execution_trigger,
                    )
        finally:
            # Publish after the execution attempt so effective-action metadata
            # includes the hardware target just accepted. The finally preserves
            # the successfully computed plan if a hardware write raises.
            self.async_set_updated_data(self.get_api_data())

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

                # The boundary action may update optimizer-owned force power.
                # Publish that accepted target before a slow forecast/LP pass so
                # the status sensor stays aligned with the hardware command.
                self.async_set_updated_data(self.get_api_data())

                # Re-optimize on each interval (executes the resulting action internally)
                await self._run_optimization(execution_trigger="poll")

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
        planned_action = getattr(current_action, "action", None)
        if not current_action or not planned_action:
            return
        action_name = self._effective_runtime_action(
            planned_action,
            getattr(current_action, "timestamp", None),
        )
        if (
            action_name == getattr(self, "_last_executed_action", None)
            and (
                action_name not in FORCED_ACTIONS
                or self._boundary_execution_matches_action(current_action, action_name)
            )
        ):
            self._record_boundary_execution(current_action, action_name)
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
            if (
                action_name == getattr(self, "_last_executed_action", None)
                and (
                    action_name not in FORCED_ACTIONS
                    or self._boundary_execution_matches_action(current_action, action_name)
                )
            ):
                self._record_boundary_execution(current_action, action_name)
                return

            _LOGGER.info(
                "Optimizer: applying cached schedule action %s on coordinator refresh",
                action_name,
            )
            previous_action = getattr(self, "_last_executed_action", None)
            await self._execute_optimizer_action(current_action)
            applied_action = getattr(self, "_last_executed_action", None)
            if applied_action == action_name or applied_action != previous_action:
                self._record_boundary_execution(current_action, applied_action)

    def _boundary_execution_matches_action(
        self,
        action: Any,
        action_name: str,
    ) -> bool:
        """Return whether this cached slot/action was already accepted."""
        boundary_execution = getattr(self, "_boundary_execution", None)
        return bool(
            boundary_execution
            and boundary_execution.get("slot_start")
            == getattr(action, "timestamp", None)
            and boundary_execution.get("action") == action_name
        )

    def _record_boundary_execution(
        self,
        action: Any,
        applied_action: str | None,
    ) -> None:
        """Record the action accepted for the cached schedule's current slot."""
        slot_start = getattr(action, "timestamp", None)
        if not isinstance(slot_start, datetime) or not applied_action:
            return
        interval = max(1, int(getattr(self._config, "interval_minutes", 5) or 5))
        self._boundary_execution = {
            "slot_start": slot_start,
            "slot_end": slot_start + timedelta(minutes=interval),
            "action": applied_action,
            "was_forced": applied_action in FORCED_ACTIONS,
        }

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
                    interval = max(
                        1, int(getattr(self._config, "interval_minutes", 5) or 5)
                    )
                    schedule_end = action.timestamp + timedelta(minutes=interval)
                    if now < schedule_end:
                        return action
                    return None

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
                # The action may be executed after a slow solve has consumed
                # part of its window. Do not ask hardware to force for the
                # full original slots and cross the next action boundary.
                block_end = None
                if slots > 0:
                    final_ts = getattr(actions[start_idx + slots - 1], "timestamp", None)
                    if isinstance(final_ts, datetime):
                        block_end = final_ts + timedelta(minutes=interval)
                now = dt_util.now()
                if (
                    isinstance(action_ts, datetime)
                    and isinstance(block_end, datetime)
                    and action_ts <= now < block_end
                ):
                    remaining_seconds = max(0.0, (block_end - now).total_seconds())
                    requested = max(1, int(math.ceil(remaining_seconds / 60.0)))
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

    def _should_disable_idle_schedule(self) -> bool:
        """Return True when no-idle mode should replace optimizer IDLE."""
        return bool(self._config.disable_idle_enabled)

    def _adopt_solved_ev_series(self, result: Any) -> None:
        """Publish the solved EV draw everywhere the overlay used to go.

        When the LP co-optimizes the car, the load overlay is deliberately
        blanked -- but three surfaces (the schedule's ``planned_ev_load_w``
        key, the optimization-status sensor attributes, and the planned-EV
        summary) publish only the overlay, so co-optimizing sites showed no
        vehicles in the plan at all. Adopt the solved series for them, and
        keep the per-vehicle split for surfaces that can use identity.
        """
        self._last_ev_charge_by_vehicle_w = (
            dict(getattr(result, "ev_charge_by_vehicle_w", None) or {}) or None
        )
        schedule = getattr(result, "schedule", None)
        self._last_ev_charge_schedule_updated = (
            getattr(schedule, "last_updated", None)
            if self._last_ev_charge_by_vehicle_w is not None
            else None
        )
        self._last_ev_source_by_vehicle_w = (
            dict(getattr(result, "ev_source_by_vehicle_w", None) or {}) or None
        )
        actions = getattr(schedule, "actions", None) or []
        solved = [
            max(0.0, float(getattr(action, "ev_charge_w", 0.0) or 0.0))
            for action in actions
        ]
        if not any(value > 0 for value in solved):
            return
        if not self._last_planned_ev_load_forecast_w:
            self._last_planned_ev_load_forecast_w = list(solved)
        if not self._last_effective_ev_load_forecast_w:
            self._last_effective_ev_load_forecast_w = list(solved)

    def _build_ev_charge_plan(
        self,
        schedule_timestamps: list[datetime] | None,
    ) -> Any | None:
        """Return the site's EV demand for this solve, or None.

        Every managed vehicle with outstanding energy and a charging deadline
        contributes its physical charging envelope. The LP then places the car
        against prices and the site import limit instead of the battery plan
        assuming an import envelope the car will take anyway.
        """
        if not schedule_timestamps:
            return None
        try:
            from ..automations.ev_charging_planner import (
                get_auto_schedule_executor,
                is_smart_schedule_grid_price_allowed,
            )
            from ..const import DOMAIN
            from ..solar_surplus_config import get_stored_solar_surplus_config
            from .ev_load_plan import ev_plan_from_demand

            executor = get_auto_schedule_executor()
            if executor is None or getattr(
                executor, "config_entry", None
            ) is not self.config_entry:
                return None
            hass_data = getattr(getattr(self, "hass", None), "data", {}) or {}
            entry_data = hass_data.get(DOMAIN, {}).get(
                self.config_entry.entry_id, {}
            )
            solar_surplus_config = get_stored_solar_surplus_config(entry_data)

            now = dt_util.now()
            plans = []
            policy_diagnostics: dict[str, dict[str, Any]] = {}
            for vehicle_id, state in (getattr(executor, "_state", {}) or {}).items():
                charging_plan = getattr(state, "current_plan", None)
                if charging_plan is None:
                    continue
                settings = (getattr(executor, "_settings", {}) or {}).get(vehicle_id)
                if settings is None or not getattr(settings, "enabled", True):
                    continue

                deadline = None
                raw_target = getattr(charging_plan, "target_time", None)
                if raw_target:
                    try:
                        deadline = dt_util.parse_datetime(str(raw_target))
                    except (TypeError, ValueError):
                        deadline = None
                if deadline is not None and deadline.tzinfo is None:
                    # _regenerate_plan() stamps target_time from an HA-local
                    # *naive* clock, so ChargingPlan.target_time parses back
                    # naive. Comparing it against an aware now() raised
                    # TypeError straight into the except below, which returned
                    # None -- silently disabling EV co-optimization for every
                    # vehicle that has a departure time set.
                    deadline = deadline.replace(
                        tzinfo=dt_util.DEFAULT_TIME_ZONE
                    )
                if deadline is not None and deadline <= now:
                    continue

                charger_power_kw = (
                    float(getattr(settings, "max_charge_amps", 0) or 0)
                    * float(getattr(settings, "voltage", 240) or 240)
                    * int(getattr(settings, "phases", 1) or 1)
                    / 1000.0
                )

                # ChargingPlan.energy_needed_kwh is already AC-side energy:
                # plan_charging() grosses the pack delta up by its 0.90
                # charging efficiency. Passing the default 0.90 here applied
                # that loss a second time and over-planned every car by 11%.
                charge_efficiency = 1.0

                # A generated Smart Schedule plan is the executable policy
                # envelope. The old hand-off replaced it with the charger's
                # physical envelope, letting the battery LP move solar-only,
                # price-capped and demand-blocked charging into arbitrary
                # slots. Tests and lightweight callers that predate planned
                # windows keep the legacy physical-envelope fallback.
                raw_windows = getattr(charging_plan, "windows", None)
                power_by_slot = None
                allow_grid = ()
                allow_solar = ()
                allow_battery = ()
                min_start_soc = ()
                battery_floor_soc = ()
                stop_at_floor = ()
                preserve_home_battery = ()
                limit_grid_import = ()
                allow_min_start_solar_exception = ()
                solar_battery_reserve_kw = ()
                window_sources = ()
                if raw_windows is not None:
                    power_values: list[float] = []
                    grid_values: list[bool] = []
                    solar_values: list[bool] = []
                    battery_values: list[bool] = []
                    start_values: list[float] = []
                    floor_values: list[float] = []
                    stop_values: list[bool] = []
                    preserve_values: list[bool] = []
                    limit_values: list[bool] = []
                    solar_exception_values: list[bool] = []
                    solar_reserve_values: list[float] = []
                    source_values: list[str] = []

                    parsed_windows = []
                    for window in raw_windows:
                        try:
                            window_start = dt_util.parse_datetime(
                                str(getattr(window, "start_time", ""))
                            )
                            window_end = dt_util.parse_datetime(
                                str(getattr(window, "end_time", ""))
                            )
                        except (TypeError, ValueError):
                            continue
                        if window_start is None or window_end is None:
                            continue
                        if window_start.tzinfo is None:
                            window_start = window_start.replace(
                                tzinfo=dt_util.DEFAULT_TIME_ZONE
                            )
                        if window_end.tzinfo is None:
                            window_end = window_end.replace(
                                tzinfo=dt_util.DEFAULT_TIME_ZONE
                            )
                        parsed_windows.append((window_start, window_end, window))

                    for timestamp in schedule_timestamps:
                        comparison = timestamp
                        if comparison.tzinfo is None:
                            comparison = comparison.replace(
                                tzinfo=dt_util.DEFAULT_TIME_ZONE
                            )
                        active_window = next(
                            (
                                window
                                for window_start, window_end, window in parsed_windows
                                if window_start <= comparison < window_end
                            ),
                            None,
                        )
                        source = str(
                            getattr(active_window, "source", "") or ""
                        ).lower()
                        is_solar = source == "solar_surplus"
                        weekday = comparison.astimezone(
                            dt_util.DEFAULT_TIME_ZONE
                        ).weekday()

                        def _effective(method_name: str, attr_name: str, default):
                            method = getattr(settings, method_name, None)
                            if callable(method):
                                return method(weekday)
                            return getattr(settings, attr_name, default)

                        if active_window is not None and not (
                            is_smart_schedule_grid_price_allowed(
                                source=source,
                                price_cents=getattr(
                                    active_window, "price_cents_kwh", None
                                ),
                                max_grid_price_cents=_effective(
                                    "get_effective_max_grid_price",
                                    "max_grid_price_cents",
                                    getattr(
                                        charging_plan,
                                        "max_grid_price_cents",
                                        None,
                                    ),
                                ),
                                priority=_effective(
                                    "get_effective_priority",
                                    "priority",
                                    getattr(charging_plan, "priority", None),
                                ),
                            )
                        ):
                            # Plans can survive a settings update until their
                            # next regeneration. Fail closed against the
                            # current per-day price policy here.
                            active_window = None
                            source = "price_blocked"
                            is_solar = False
                        planner = getattr(executor, "planner", None)
                        demand_blocked = getattr(
                            planner, "_is_grid_charging_blocked_at", None
                        )
                        if (
                            active_window is not None
                            and source.startswith("grid")
                            and callable(demand_blocked)
                            and demand_blocked(comparison)
                        ):
                            active_window = None
                            source = "demand_blocked"
                            is_solar = False

                        consume_floor = max(
                            0.0,
                            min(
                                1.0,
                                float(
                                    _effective(
                                        "get_effective_consume_battery_level",
                                        "consume_battery_level",
                                        0,
                                    )
                                    or 0
                                )
                                / 100.0,
                            ),
                        )
                        preserve = bool(
                            _effective(
                                "get_effective_preserve_home_battery",
                                "preserve_home_battery",
                                False,
                            )
                        )
                        minimum_start = max(
                            0.0,
                            min(
                                1.0,
                                float(
                                    _effective(
                                        "get_effective_min_battery_to_start",
                                        "min_battery_to_start",
                                        0,
                                    )
                                    or 0
                                )
                                / 100.0,
                            ),
                        )
                        limit_grid = bool(
                            _effective(
                                "get_effective_limit_grid_import",
                                "limit_grid_import",
                                False,
                            )
                        )
                        stop_at_floor_value = bool(
                            _effective(
                                "get_effective_stop_at_battery_floor",
                                "stop_at_battery_floor",
                                True,
                            )
                        )
                        solar_exception = bool(
                            is_solar
                            and solar_surplus_config.get(
                                "allow_parallel_charging", False
                            )
                        )
                        solar_reserve_kw = (
                            max(
                                0.0,
                                float(
                                    solar_surplus_config.get(
                                        "max_battery_charge_rate_kw", 5.0
                                    )
                                    or 0.0
                                ),
                            )
                            if solar_exception
                            else 0.0
                        )

                        window_power_kw = 0.0
                        if active_window is not None:
                            planned_power_kw = max(
                                0.0,
                                float(
                                    getattr(
                                        active_window,
                                        "estimated_power_kw",
                                        charger_power_kw,
                                    )
                                    or charger_power_kw
                                ),
                            )
                            # Grid-window power was pre-reduced against the
                            # previous battery schedule. The joint LP owns that
                            # sharing now, so expose the physical charger cap.
                            # Solar windows remain capped by forecast surplus.
                            window_power_kw = min(
                                charger_power_kw,
                                planned_power_kw if is_solar else charger_power_kw,
                            )
                        power_values.append(window_power_kw)
                        grid_values.append(active_window is not None and not is_solar)
                        solar_values.append(active_window is not None)
                        battery_values.append(
                            active_window is not None
                            and not is_solar
                            and (consume_floor > 0.0 or limit_grid)
                            and not preserve
                        )
                        start_values.append(minimum_start)
                        floor_values.append(consume_floor)
                        stop_values.append(stop_at_floor_value)
                        preserve_values.append(preserve)
                        limit_values.append(limit_grid)
                        solar_exception_values.append(solar_exception)
                        solar_reserve_values.append(solar_reserve_kw)
                        source_values.append(source)

                    power_by_slot = power_values
                    allow_grid = grid_values
                    allow_solar = solar_values
                    allow_battery = battery_values
                    min_start_soc = start_values
                    battery_floor_soc = floor_values
                    stop_at_floor = stop_values
                    preserve_home_battery = preserve_values
                    limit_grid_import = limit_values
                    allow_min_start_solar_exception = solar_exception_values
                    solar_battery_reserve_kw = solar_reserve_values
                    window_sources = source_values
                    if (
                        power_values
                        and str(getattr(state, "last_decision", "")).lower()
                        in {"away", "unplugged"}
                    ):
                        # Keep future demand protected: an away/unplugged car
                        # can return before its deadline. Only the current slot
                        # is known to be non-executable; the next rolling solve
                        # will refresh this gate from live availability.
                        power_values[0] = 0.0
                        grid_values[0] = False
                        solar_values[0] = False
                        battery_values[0] = False
                        source_values[0] = "unavailable_now"
                if power_by_slot is not None:
                    def _policy_at(index: int) -> dict[str, Any]:
                        source = str(window_sources[index] or "")
                        power = round(float(power_by_slot[index] or 0.0), 3)
                        return {
                            "window_source": source,
                            "max_power_kw": power,
                            "allow_grid": bool(allow_grid[index]),
                            "allow_solar": bool(allow_solar[index]),
                            "allow_battery": bool(allow_battery[index]),
                            "min_start_soc_pct": round(
                                float(min_start_soc[index]) * 100, 1
                            ),
                            "battery_floor_soc_pct": round(
                                float(battery_floor_soc[index]) * 100, 1
                            ),
                            "stop_at_battery_floor": bool(
                                stop_at_floor[index]
                            ),
                            "preserve_home_battery": bool(
                                preserve_home_battery[index]
                            ),
                            "limit_grid_import": bool(
                                limit_grid_import[index]
                            ),
                            "constraint_reason": (
                                "eligible"
                                if power > 1e-9
                                else source or "outside_smart_schedule_window"
                            ),
                        }

                    segments: list[dict[str, Any]] = []
                    segment_start = 0
                    previous = _policy_at(0)
                    for index in range(1, len(schedule_timestamps) + 1):
                        current = (
                            _policy_at(index)
                            if index < len(schedule_timestamps)
                            else None
                        )
                        if current == previous:
                            continue
                        if (
                            previous["max_power_kw"] > 0
                            or previous["window_source"]
                        ):
                            end_time = (
                                schedule_timestamps[index]
                                if index < len(schedule_timestamps)
                                else schedule_timestamps[-1]
                                + (
                                    schedule_timestamps[-1]
                                    - schedule_timestamps[-2]
                                    if len(schedule_timestamps) > 1
                                    else timedelta(minutes=5)
                                )
                            )
                            segments.append(
                                {
                                    "start": schedule_timestamps[
                                        segment_start
                                    ].isoformat(),
                                    "end": end_time.isoformat(),
                                    **previous,
                                }
                            )
                        segment_start = index
                        previous = current
                    policy_diagnostics[str(vehicle_id)] = {
                        "current": _policy_at(0),
                        "segments": segments,
                    }
                plans.append(
                    ev_plan_from_demand(
                        vehicle_id=str(vehicle_id),
                        energy_needed_kwh=getattr(
                            charging_plan, "energy_needed_kwh", 0.0
                        ),
                        charger_power_kw=charger_power_kw,
                        schedule_timestamps=schedule_timestamps,
                        deadline=deadline,
                        charge_efficiency=charge_efficiency,
                        min_power_kw=(
                            float(getattr(settings, "min_charge_amps", 0) or 0)
                            * float(getattr(settings, "voltage", 240) or 240)
                            * int(getattr(settings, "phases", 1) or 1)
                            / 1000.0
                        ),
                        max_power_by_slot_kw=power_by_slot,
                        allow_grid=allow_grid,
                        allow_solar=allow_solar,
                        allow_battery=allow_battery,
                        min_start_soc=min_start_soc,
                        battery_floor_soc=battery_floor_soc,
                        stop_at_battery_floor=stop_at_floor,
                        preserve_home_battery=preserve_home_battery,
                        limit_grid_import=limit_grid_import,
                        allow_min_start_solar_exception=(
                            allow_min_start_solar_exception
                        ),
                        solar_battery_reserve_kw=solar_battery_reserve_kw,
                        window_source=window_sources,
                        initially_charging=bool(
                            getattr(state, "is_charging", False)
                        ),
                    )
                )
            # Hand the solver one plan per vehicle. Combining them into a
            # single block summed both chargers' capability into every
            # overlapping slot, so the plan could show 2x one car's rate in a
            # window where only one car would really charge, and had no
            # per-vehicle identity to publish.
            plans = [plan for plan in plans if plan is not None]
            self._last_ev_optimizer_policy = policy_diagnostics or None
            # ``[]`` is an intentional sentinel: a valid Smart Schedule was
            # evaluated but every managed EV slot is currently blocked. The
            # caller must suppress the legacy fixed-load overlay in that case
            # rather than resurrecting those rejected windows.
            return plans
        except Exception as err:
            # Keep the guard -- a broken EV plan must not stop the solve -- but
            # not at debug level: this swallowed a plain TypeError for every
            # deadline-bearing vehicle and nothing surfaced it.
            _LOGGER.warning(
                "Could not build EV charge plan for the optimizer: %s", err
            )
            return None

    def _effective_runtime_action(
        self,
        action_name: str | None,
        timestamp: datetime | None = None,
    ) -> str | None:
        """Return the action that runtime execution will apply."""
        if action_name != "idle":
            return action_name
        # No Idle takes precedence over every optimizer hold, including Charge
        # By Time reachability holds. Keep this runtime guard even though the
        # emitted trajectory is modeled the same way, so a stale schedule
        # cannot publish IDLE after the setting is enabled.
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
            # A converted slot keeps its planned EV draw, and in
            # self-consumption the inverter serves the car too.
            if index < len(actions):
                net_load_w += max(
                    0.0,
                    float(getattr(actions[index], "ev_charge_w", 0.0) or 0.0),
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
                            ev_charge_w=getattr(action, "ev_charge_w", 0.0),
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
                    ev_charge_w=getattr(action, "ev_charge_w", 0.0),
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

    def _restore_bridged_export_gap_provenance(
        self,
        source_schedule: OptimizationSchedule,
        reconciled_schedule: OptimizationSchedule,
    ) -> None:
        """Carry bridged-slot provenance across reconciliation restamping."""
        bridged_timestamps = {
            self._as_utc_datetime(getattr(action, "timestamp", None))
            for action in (getattr(source_schedule, "actions", None) or [])
            if getattr(action, "_optimizer_bridged_export_gap", False)
        }
        bridged_timestamps.discard(None)
        if not bridged_timestamps:
            return

        for action in getattr(reconciled_schedule, "actions", None) or []:
            if (
                self._as_utc_datetime(getattr(action, "timestamp", None))
                in bridged_timestamps
                and getattr(action, "action", None) in EXPORT_ACTIONS
            ):
                setattr(action, "_optimizer_bridged_export_gap", True)

    @staticmethod
    def _merge_export_protection_floors(
        base_floor: float | list[float] | None,
        protection_floors: list[float] | None,
        length: int,
    ) -> float | list[float] | None:
        """Merge solve-local export protection with the authoritative floor."""
        if not protection_floors:
            return base_floor
        if isinstance(base_floor, list):
            base = [
                max(0.0, min(1.0, float(value or 0.0)))
                for value in base_floor[:length]
            ]
            if len(base) < length:
                base.extend([0.0] * (length - len(base)))
        else:
            scalar = max(0.0, min(1.0, float(base_floor or 0.0)))
            base = [scalar] * length
        return [
            max(
                base[idx],
                max(0.0, min(1.0, float(protection_floors[idx] or 0.0)))
                if idx < len(protection_floors)
                else 0.0,
            )
            for idx in range(length)
        ]

    def _bridge_short_export_gaps(
        self,
        schedule: OptimizationSchedule,
        export_prices: list[float] | None = None,
        export_reserve_floor: float | list[float] | None = None,
        *,
        authoritative_reserve_floor: float | list[float] | None = None,
    ) -> OptimizationSchedule:
        """Keep export mode through one-slot self-use islands between exports."""
        if (
            export_reserve_floor is not None
            and authoritative_reserve_floor is not None
        ):
            raise ValueError(
                "export_reserve_floor and authoritative_reserve_floor "
                "are mutually exclusive"
            )
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
            min_export_price = normalize_min_export_price(
                getattr(self._config, "min_export_price", 0.0)
            )
            if min_export_price > 0 and (
                export_prices is None
                or any(
                not export_price_allows_battery_export(
                    export_prices[slot] if slot < len(export_prices) else None,
                    min_export_price,
                )
                for slot in range(gap_start, gap_end)
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
            gap_action = actions[gap_start]
            home_discharge_w = max(
                0.0,
                float(getattr(gap_action, "battery_discharge_w", 0.0) or 0.0),
            )
            max_discharge_w = max(
                0.0,
                float(getattr(self._config, "max_discharge_w", 0.0) or 0.0),
            )
            bridge_power_w = min(
                bridge_power_w,
                max(0.0, max_discharge_w - home_discharge_w),
            )
            if bridge_power_w <= 0:
                continue
            battery_discharge_w = home_discharge_w + bridge_power_w
            if authoritative_reserve_floor is not None:
                if isinstance(authoritative_reserve_floor, list):
                    scoped_floors = [
                        self._reserve_ratio(value, 0.0) or 0.0
                        for value in authoritative_reserve_floor[gap_start:gap_end]
                    ]
                    reserve_floor = max(scoped_floors, default=0.0)
                else:
                    reserve_floor = (
                        self._reserve_ratio(authoritative_reserve_floor, 0.0)
                        or 0.0
                    )
            else:
                reserve_floor = self._bridge_export_reserve_floor(
                    export_reserve_floor,
                    gap_start,
                    gap_end,
                )
            if not self._can_bridge_export_gap_above_reserve(
                previous_action,
                actions[gap_start:gap_end],
                battery_discharge_w,
                reserve_floor,
            ):
                continue

            for gap_action in actions[gap_start:gap_end]:
                gap_action.action = export_action
                gap_action.power_w = bridge_power_w
                gap_action.battery_charge_w = 0.0
                gap_action.battery_discharge_w = battery_discharge_w
                # Keep this one-slot provenance so execution can apply the
                # optimizer force commitment to the bridged action.  A
                # bridged LP target is not a new hardware power request.
                setattr(gap_action, "_optimizer_bridged_export_gap", True)
                bridged_soc = self._bridged_gap_soc(
                    previous_action,
                    battery_discharge_w,
                )
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
        battery_discharge_w: float,
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
        bridged_soc = self._bridged_gap_soc(previous_action, battery_discharge_w)
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
        battery_discharge_w: float,
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
        removed_wh = (
            max(0.0, float(battery_discharge_w or 0.0))
            * interval_hours
            / max(efficiency, 0.001)
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
        # for the full 20-minute commitment. Discharge has an additional
        # priority-window hold because its tariff opportunity is time-bounded.
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

        # A priority tariff window is deliberately stable even when one fresh
        # LP solve temporarily values future self-consumption above exporting.
        # Without this, a rolling forecast can restart an export and cancel it
        # again five minutes later, making the nominal 20-minute commitment
        # ineffective.  Only hold while the *fresh* solve still says battery
        # export is permitted and the current slot remains a priority window;
        # window endings and hard export gates therefore still release at once.
        calibration_data = self.hass.data.get("power_sync", {}).get(
            self.entry_id,
            {},
        )
        hard_runtime_block = bool(
            calibration_data.get("calibration_suspected")
            or self._scheduled_ev_preserve_active()
            or self._should_block_export_for_demand()
        )
        export_price = None
        real_export_prices = (
            getattr(self, "_last_settlement_export_prices", None)
            or getattr(self, "_last_export_prices", None)
        )
        if real_export_prices:
            export_price = self._current_effective_export_price_for_action(
                real_export_prices,
                action,
            )
        min_export_price = normalize_min_export_price(
            getattr(self._config, "min_export_price", 0.0)
        )
        price_is_usable = (
            export_price is None or export_price >= 0.01
            if min_export_price <= 0
            else export_price_allows_battery_export(
                export_price,
                min_export_price,
            )
        )
        if hard_runtime_block or not price_is_usable:
            return None

        export_allowed_slots = getattr(
            self,
            "_last_battery_export_allowed_slots",
            [],
        )
        export_allowed_now = self._action_slot_enabled(
            action,
            export_allowed_slots,
        )
        if export_allowed_slots and not export_allowed_now:
            return None

        if (
            export_allowed_now
            and self._action_slot_enabled(
                action,
                getattr(self, "_last_priority_export_slots", []),
            )
        ):
            return remaining

        if not self._schedule_has_future_action(action, EXPORT_ACTIONS, remaining):
            return None
        return remaining

    def _action_slot_enabled(
        self,
        action: Any,
        enabled_slots: list[bool],
    ) -> bool:
        """Return whether an action timestamp maps to an enabled solve slot."""
        if not enabled_slots:
            return False

        action_ts = self._as_utc_datetime(getattr(action, "timestamp", None))
        if action_ts is None:
            return False

        interval_minutes = max(
            1,
            int(getattr(self._config, "interval_minutes", 5) or 5),
        )
        interval = timedelta(minutes=interval_minutes)
        timestamps = self._price_timestamps(len(enabled_slots))
        for idx, enabled in enumerate(enabled_slots):
            if not enabled or idx >= len(timestamps):
                continue
            slot_start = self._as_utc_datetime(timestamps[idx])
            if (
                slot_start is not None
                and slot_start <= action_ts < slot_start + interval
            ):
                return True
        return False

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
        shared_force_state: dict[str, Any] = {}
        force_state_getter = getattr(self, "_force_state_getter", None)
        if force_state_getter:
            shared_force_state = force_state_getter() or {}
            if (
                shared_force_state.get("active")
                and shared_force_state.get("source") != "optimizer"
            ):
                external_state = dict(shared_force_state)
                external_state.setdefault("scope", "external")
                return external_state

        state = getattr(self, "_optimizer_force_state", None)
        optimizer_state_matches = (
            isinstance(state, dict)
            and state.get("active")
            and (
                not shared_force_state.get("active")
                or shared_force_state.get("type") == state.get("type")
            )
        )
        if not optimizer_state_matches:
            if shared_force_state.get("active"):
                external_state = dict(shared_force_state)
                external_state.setdefault("scope", "external")
                return external_state
            return {"active": False}

        expires_at = self._as_utc_datetime(state.get("expires_at"))
        now = dt_util.utcnow()
        if expires_at is not None and expires_at <= now:
            self._clear_optimizer_force_state()
            if shared_force_state.get("active"):
                external_state = dict(shared_force_state)
                external_state.setdefault("scope", "external")
                return external_state
            return {"active": False}

        active = dict(state)
        active.setdefault("source", "optimizer")
        active.setdefault("scope", "optimizer")
        return active

    @staticmethod
    def _annotate_manual_control_schedule(
        schedule: OptimizationSchedule,
        projection: ManualControlProjection | None,
    ) -> OptimizationSchedule:
        """Mark fixed manual slots without claiming hardware acknowledgement."""
        if projection is None:
            return schedule
        projected_action = {
            "charge": "charge",
            "discharge": "export",
            "export": "export",
            "hold_soc": "idle",
            "self_consumption": "self_consumption",
        }.get(projection.control_type)
        for idx, action in enumerate(schedule.actions or []):
            if idx >= len(projection.active_slots) or not projection.active_slots[idx]:
                continue
            if projected_action is not None:
                action.action = projected_action
                if projected_action == "charge":
                    action.power_w = action.battery_charge_w
                elif projected_action == "idle":
                    action.power_w = 0.0
            action.reason = "manual_control_projection"
            action.control_source = "manual"
            action.control_action = projection.control_type
        return schedule

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

    def _network_export_guard(self) -> Any | None:
        """Return the entry-scoped network guard when configured."""
        from ..const import DOMAIN

        entry_data = self.hass.data.get(DOMAIN, {}).get(self.entry_id, {})
        return entry_data.get("network_export_guard")

    def _sigenergy_zero_export_curtailment_active(self) -> bool:
        """Return True while PowerSync holds Sigenergy at zero grid export.

        Sigenergy DC curtailment and a force discharge own the same register
        (REG_GRID_EXPORT_LIMIT / 40038), so an export ceiling written while the
        cached curtailment state is ``curtailed`` silently lifts a curtailment
        the user asked for because export earnings are below the threshold.
        """
        from ..const import DOMAIN

        entry_data = self.hass.data.get(DOMAIN, {}).get(self.entry_id, {})
        return entry_data.get("sigenergy_curtailment_state") == "curtailed"

    async def _force_discharge_through_export_guard(
        self,
        battery: Any,
        requested_w: float,
        total_battery_discharge_w: float | None = None,
        **command_kwargs: Any,
    ) -> tuple[bool, float]:
        """Issue one export-increasing write through the centralized guard.

        The writer closure is invoked only after the guard has read an atomic
        envelope snapshot, accounted for unmanaged PCC export and rechecked the
        snapshot version immediately before the actuator write.
        """
        if self._sigenergy_zero_export_curtailment_active():
            # Curtailment owns the export-limit register while it is active.
            # Raising the ceiling here would export at a price the user's own
            # curtailment threshold rejected, and nothing clears the cached
            # curtailment state, so the site would keep reporting "curtailed".
            _LOGGER.info(
                "Optimizer: export command blocked — Sigenergy zero-export "
                "curtailment is active (export earnings below threshold)"
            )
            force_state = getattr(self, "_optimizer_force_state", None)
            if (
                isinstance(force_state, dict)
                and force_state.get("active")
                and force_state.get("type") == "discharge"
            ):
                # An earlier force window is still armed in hardware; hand the
                # inverter back to self-consumption. The optimizer-sourced
                # restore reasserts the zero-export limit rather than the
                # normal export cap (inverters/sigenergy.py restore_normal).
                if hasattr(battery, "restore_normal"):
                    await battery.restore_normal()
                elif hasattr(battery, "set_self_consumption_mode"):
                    await battery.set_self_consumption_mode()
            return False, 0.0

        guard = self._network_export_guard()
        applied_w = 0.0

        async def _writer(power_w: float) -> bool:
            nonlocal applied_w
            applied_w = max(0.0, float(power_w))
            writer_kwargs = dict(command_kwargs)
            if (
                self.battery_system == "solax"
                and total_battery_discharge_w is not None
            ):
                home_discharge_w = max(
                    0.0,
                    float(total_battery_discharge_w) - max(0.0, float(requested_w)),
                )
                writer_kwargs["battery_discharge_w"] = (
                    home_discharge_w + applied_w
                )
            result = await battery.force_discharge(
                power_w=applied_w,
                **writer_kwargs,
            )
            return result is not False

        if guard is None:
            return await _writer(requested_w), applied_w

        success = await guard.async_guard_write(requested_w, _writer)
        if success:
            return True, applied_w

        snapshot = guard.manager.snapshot
        if snapshot.mode != "off":
            _LOGGER.warning(
                "Optimizer: export command blocked by network envelope (%s)",
                snapshot.fault or snapshot.reason or snapshot.mode,
            )
            stopped = True
            if hasattr(battery, "restore_normal"):
                stopped = await battery.restore_normal()
            elif hasattr(battery, "set_self_consumption_mode"):
                stopped = await battery.set_self_consumption_mode()
            if stopped is False:
                await guard.manager.async_set_fault(
                    "network envelope blocked export; export stop command failed"
                )
        return False, 0.0

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

        if self.battery_system == "saj_h2":
            app_mode = data.get("app_mode")
            try:
                app_mode_int = int(float(app_mode))
            except (TypeError, ValueError):
                return False
            if app_mode_int == 1:
                return False
            _LOGGER.info(
                "Optimizer: SAJ force charge hardware appears inactive "
                "(AppMode=%s, expected TOU AppMode=1) — refreshing command",
                app_mode,
            )
            return True

        # The power-threshold heuristic is only meaningful when the selected
        # battery can honor the requested target. Fixed-rate controls may taper
        # below that target while their hardware command remains active.
        if not self._supports_target_charge_power():
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

    def _current_price_index_for_action(
        self,
        length: int,
        action: Any | None,
    ) -> int | None:
        """Return the price interval index matching an action timestamp."""
        if action is None:
            return None
        action_time = self._as_utc_datetime(getattr(action, "timestamp", None))
        if action_time is None:
            return None
        if length <= 0:
            return None
        timestamps = self._price_timestamps(length)
        if not timestamps:
            return None

        interval_minutes = max(
            1,
            int(getattr(self._config, "interval_minutes", 5) or 5),
        )
        slot_limit = timedelta(minutes=interval_minutes)
        n = min(length, len(timestamps))
        for idx in range(n):
            slot_start = self._as_utc_datetime(timestamps[idx])
            if slot_start is None:
                continue
            if slot_start <= action_time < slot_start + slot_limit:
                return idx
        return None

    def _current_import_price_for_action(
        self,
        prices: list[float],
        action: Any | None,
    ) -> float | None:
        """Return the tariff price for an action's scheduled interval."""
        idx = self._current_price_index_for_action(len(prices), action)
        if idx is None:
            return None
        try:
            return float(prices[idx])
        except (TypeError, ValueError):
            return None

    def _current_export_price_for_action(
        self,
        prices: list[float],
        action: Any | None,
    ) -> float | None:
        """Return the export tariff price for an action's scheduled interval."""
        return self._current_import_price_for_action(prices, action)

    def _current_effective_export_price_for_action(
        self,
        prices: list[float],
        action: Any | None,
    ) -> float | None:
        """Return an action slot's export value including an active quota bonus."""
        base_price = self._current_export_price_for_action(prices, action)
        if base_price is None:
            return None

        group_ids = getattr(self, "_last_export_bonus_group_ids", None)
        caps_by_group = getattr(self, "_last_export_bonus_caps_by_group", None)
        if group_ids is not None and caps_by_group is not None:
            action_idx = self._current_price_index_for_action(len(group_ids), action)
            action_group = group_ids[action_idx] if action_idx is not None else None
            if action_group is None:
                remaining_bonus_cap = 0.0
            else:
                try:
                    remaining_bonus_cap = float(
                        caps_by_group.get(str(action_group), 0.0) or 0.0
                    )
                except (TypeError, ValueError):
                    remaining_bonus_cap = 0.0
        else:
            try:
                remaining_bonus_cap = float(
                    getattr(self, "_last_zerohero_bonus_cap_kwh", 0.0) or 0.0
                )
            except (TypeError, ValueError):
                remaining_bonus_cap = 0.0
        if not math.isfinite(remaining_bonus_cap) or remaining_bonus_cap <= 1e-6:
            return base_price

        bonus_prices = getattr(self, "_last_zerohero_bonus_prices", None)
        if not bonus_prices:
            return base_price
        bonus_price = self._current_export_price_for_action(bonus_prices, action)
        if bonus_price is None:
            return base_price
        try:
            return base_price + max(0.0, float(bonus_price))
        except (TypeError, ValueError):
            return base_price

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
        load_w = self._kw_to_w(
            data.get("site_load_power", data.get("load_power"))
        )
        if solar_w is not None and load_w is not None:
            return max(0.0, min(max_charge_w, max_grid_import_w + solar_w - load_w))

        grid_w = self._kw_to_w(data.get("grid_power"))
        battery_w = self._kw_to_w(data.get("battery_power"))
        if grid_w is None or battery_w is None:
            return None

        current_charge_w = max(0.0, -battery_w)
        return max(0.0, min(max_charge_w, max_grid_import_w - grid_w + current_charge_w))

    def _charge_command_power_w(self, action: Any) -> float:
        """Return a safe charge command power for the scheduled action."""
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

        # Spread Import deliberately lowers the scheduled target to distribute
        # grid charging over its eligible window.  Live headroom remains a
        # safety ceiling, but must not widen that deliberate target again.
        if self._should_spread_import_schedule():
            command_w = min(scheduled_w, live_limit_w)
            if scheduled_w - command_w >= 250.0:
                _LOGGER.info(
                    "Optimizer: Limiting spread-import charge target from %.0fW to %.0fW "
                    "using live site-import headroom",
                    scheduled_w,
                    command_w,
                )
            return command_w

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

        load_w = self._kw_to_w(
            data.get("site_load_power", data.get("load_power"))
        )
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

    async def _grid_charge_soc_cap_reached(
        self,
    ) -> tuple[bool, float | None, float]:
        """Return whether live SOC has reached the configured grid-charge cap."""
        cap = self._soc_ratio(
            getattr(self._config, "grid_charge_soc_cap", 1.0),
            1.0,
        )
        if cap >= 0.999:
            return False, None, cap

        try:
            data = self._get_energy_data()
            raw_soc = data.get("battery_level") if data else None
            if raw_soc is None:
                return True, None, cap
            soc = float(raw_soc) / 100.0
            if not math.isfinite(soc):
                return True, None, cap
            soc = max(0.0, min(1.0, soc))
        except (TypeError, ValueError):
            return True, None, cap
        return soc >= cap - 0.0001, soc, cap

    def _tesla_stale_grid_charge_reading(
        self,
        reserve_pct: int | None,
    ) -> tuple[float, float, float] | None:
        """Return a material unintended Tesla grid-charge reading.

        Tesla can confirm ``self_consumption`` and the restored backup reserve
        while retaining the previous force-charge dispatch. Only treat the
        flow as stale when the battery is materially charging from the grid,
        SOC is already clear of the reserve, and native grid services are not
        active. Solar-funded charging and reserve recovery remain untouched.
        """
        if self.battery_system != "tesla" or reserve_pct is None:
            return None

        entry_data = self.hass.data.get("power_sync", {}).get(
            self.entry_id,
            {},
        )
        if entry_data.get("calibration_suspected"):
            return None

        data = self._get_energy_data()
        if not isinstance(data, dict) or data.get("grid_services_active"):
            return None

        try:
            battery_kw = float(data.get("battery_power"))
            grid_kw = float(data.get("grid_power"))
            soc_pct = float(data.get("battery_level"))
        except (TypeError, ValueError):
            return None
        if not all(
            math.isfinite(value) for value in (battery_kw, grid_kw, soc_pct)
        ):
            return None

        charge_kw = max(0.0, -battery_kw)
        grid_import_kw = max(0.0, grid_kw)
        if (
            charge_kw < 0.5
            or grid_import_kw < max(0.5, charge_kw * 0.5)
            or soc_pct <= float(reserve_pct) + 2.0
        ):
            return None
        return charge_kw, grid_import_kw, soc_pct

    async def _execute_optimizer_action(
        self,
        action: Any,
        *,
        execution_trigger: str | None = None,
    ) -> None:
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
        if getattr(self, "_optimizer_restore_in_progress", False):
            _LOGGER.debug(
                "Optimizer: skipping reentrant action while restoring battery mode"
            )
            return

        runtime_action = self._effective_runtime_action(
            getattr(action, "action", None),
        )
        battery_export_price_blocked = False
        min_export_price = normalize_min_export_price(
            getattr(self._config, "min_export_price", 0.0)
        )
        if runtime_action in EXPORT_ACTIONS and min_export_price > 0:
            real_export_prices = (
                getattr(self, "_last_settlement_export_prices", None)
                or getattr(self, "_last_export_prices", None)
            )
            real_export_price = (
                self._current_effective_export_price_for_action(
                    real_export_prices,
                    action,
                )
                if real_export_prices
                else None
            )
            if not export_price_allows_battery_export(
                real_export_price,
                min_export_price,
            ):
                _LOGGER.info(
                    "Optimizer: battery export blocked by minimum real export "
                    "price (price=%s, floor=%.1fc/kWh); restoring self_consumption",
                    (
                        f"{real_export_price * 100:.1f}c/kWh"
                        if real_export_price is not None
                        else "unavailable"
                    ),
                    min_export_price * 100,
                )
                runtime_action = "self_consumption"
                battery_export_price_blocked = True

        solar_export_hold = getattr(self, "_solar_export_hold", None)
        solar_export_cleanup_failed = False
        if (
            solar_export_hold
            and solar_export_hold.active
            and (
                runtime_action != "solar_export"
                or self._monitoring_mode_active()
            )
        ):
            if not await solar_export_hold.clear(
                f"transition_to_{runtime_action or 'unknown'}"
            ):
                _LOGGER.error(
                    "Profit Max: solar-export hold cleanup remains pending; "
                    "falling back to ordinary self-consumption"
                )
                runtime_action = "self_consumption"
                solar_export_cleanup_failed = True

        # Monitoring mode — log what would happen but don't execute
        if self._monitoring_mode_active():
            _LOGGER.info(
                "[MONITORING] Optimizer would execute: %s (power=%sW) — blocked by monitoring mode",
                runtime_action, getattr(action, 'power_w', 'N/A'),
            )
            return

        battery = self._executor.battery_controller
        if solar_export_cleanup_failed:
            try:
                if hasattr(battery, "set_self_consumption_mode"):
                    await battery.set_self_consumption_mode()
                elif hasattr(battery, "restore_normal"):
                    await battery.restore_normal()
            except Exception:
                _LOGGER.exception(
                    "Profit Max: ordinary self-consumption fallback command failed"
                )

        # An active Powerwall calibration pauses automatic battery commands.
        # Check this before the optimizer-owned force-extension path: that
        # path can otherwise refresh a Tesla TOU tariff (and reserve/mode
        # writes) before the ordinary action gate below converts charge/export
        # to self-consumption.
        from ..const import DOMAIN as _CAL_DOMAIN
        _calibration_data = self.hass.data.get(_CAL_DOMAIN, {}).get(
            self.entry_id, {}
        )
        if _calibration_data.get("calibration_suspected"):
            active_force = self._get_active_force_state()
            if (
                active_force
                and active_force.get("active")
                and active_force.get("source") == "optimizer"
            ):
                _LOGGER.info(
                    "Optimizer: calibration suspected - holding active optimizer force "
                    "without refreshing hardware"
                )
                return

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
                lp_matches_force = (
                    _action_matches_force(action)
                    and not battery_export_price_blocked
                )
                bridged_export_gap = bool(
                    force_type == "discharge"
                    and getattr(action, "_optimizer_bridged_export_gap", False)
                )
                if preserve_active_for_force and force_type == "discharge":
                    lp_matches_force = False
                elif bridged_export_gap:
                    # The bridge keeps the action in export mode for the LP,
                    # but it must still use the existing commitment safety
                    # path.  Otherwise the normal matching-action path would
                    # refresh hardware with the bridge's lower power.
                    lp_matches_force = False
                force_window_action = action
                force_discharge_soc_now: float | None = None
                force_discharge_reserve: float | None = None
                cap_cancelled_for_new_action = False

                if force_type == "charge":
                    if self._grid_charge_blackout_active_now():
                        _LOGGER.info(
                            "Optimizer: Canceling active force charge during configured "
                            "grid-charge blackout; restoring self_consumption"
                        )
                        if force_state.get("scope") == "optimizer":
                            self._clear_optimizer_force_state()
                        elif self._force_state_clearer:
                            self._force_state_clearer()
                        restore_success = True
                        if hasattr(battery, "restore_normal"):
                            restore_success = await battery.restore_normal()
                        elif hasattr(battery, "set_self_consumption_mode"):
                            restore_success = await battery.set_self_consumption_mode()
                        if restore_success is False:
                            _LOGGER.warning(
                                "Optimizer: blackout restore failed; retaining force "
                                "state for retry"
                            )
                            return
                        self._last_executed_planned_action = action.action
                        self._last_executed_action = "self_consumption"
                        return
                    try:
                        cap_reached, charge_soc_now, charge_soc_cap = (
                            await self._grid_charge_soc_cap_reached()
                        )
                    except Exception as cap_err:
                        cap_reached = True
                        charge_soc_now = None
                        charge_soc_cap = self._soc_ratio(
                            getattr(self._config, "grid_charge_soc_cap", 1.0),
                            1.0,
                        )
                        _LOGGER.warning(
                            "Optimizer: grid-charge SOC-cap check before extending "
                            "force charge failed; restoring conservatively: %s",
                            cap_err,
                        )
                    if cap_reached:
                        if charge_soc_now is None:
                            _LOGGER.warning(
                                "Optimizer: Canceling active force charge — live "
                                "SOC could not be verified against grid-charge cap "
                                "%.0f%%; restoring self_consumption instead of "
                                "extending",
                                charge_soc_cap * 100,
                            )
                        else:
                            _LOGGER.warning(
                                "Optimizer: Canceling active force charge — live SOC "
                                "%.1f%% reached grid-charge cap %.0f%%; restoring "
                                "self_consumption instead of extending",
                                charge_soc_now * 100,
                                charge_soc_cap * 100,
                            )
                        optimizer_force_snapshot = None
                        if force_state.get("scope") == "optimizer":
                            optimizer_force_snapshot = dict(self._optimizer_force_state)
                            self._clear_optimizer_force_state()
                        elif self._force_state_clearer:
                            self._force_state_clearer()
                        self._optimizer_restore_in_progress = True
                        try:
                            restore_success = True
                            if hasattr(battery, "restore_normal"):
                                restore_success = await battery.restore_normal()
                            elif hasattr(battery, "set_self_consumption_mode"):
                                restore_success = await battery.set_self_consumption_mode()
                        finally:
                            self._optimizer_restore_in_progress = False
                        if restore_success is False:
                            if optimizer_force_snapshot is not None:
                                self._optimizer_force_state = optimizer_force_snapshot
                            _LOGGER.warning(
                                "Optimizer: Grid-charge SOC-cap restore failed; "
                                "retaining force state for retry"
                            )
                            return
                        cap_cancelled_for_new_action = action.action in (
                            "discharge",
                            "export",
                        )
                        if not cap_cancelled_for_new_action:
                            self._last_executed_planned_action = action.action
                            self._last_executed_action = "self_consumption"
                            return

                if not cap_cancelled_for_new_action and lp_matches_force:
                    if force_type == "discharge":
                        try:
                            force_discharge_soc_now, _ = (
                                await self._get_battery_state()
                            )
                            force_discharge_reserve = (
                                self._force_discharge_reserve_floor(action)
                            )
                            reaches_reserve, projected_soc = (
                                self._force_discharge_reaches_reserve(
                                    action,
                                    force_discharge_soc_now,
                                    force_discharge_reserve,
                                )
                            )
                            if reaches_reserve:
                                soc_text = (
                                    f"{force_discharge_soc_now * 100:.1f}%"
                                    if force_discharge_soc_now is not None
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
                                    force_discharge_reserve * 100,
                                )
                                restore_success = True
                                if hasattr(battery, "restore_normal"):
                                    restore_success = await battery.restore_normal()
                                elif hasattr(battery, "set_self_consumption_mode"):
                                    restore_success = await battery.set_self_consumption_mode()
                                if restore_success is False:
                                    _LOGGER.warning(
                                        "Optimizer: Force-discharge reserve restore failed; "
                                        "retaining force state for retry"
                                    )
                                    return
                                if force_state.get("scope") == "optimizer":
                                    self._clear_optimizer_force_state()
                                elif self._force_state_clearer:
                                    self._force_state_clearer()
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
                    targetless_window_shortened = False
                    if (
                        force_type == "discharge"
                        and not self._supports_target_export_power()
                    ):
                        safe_mins, _ = self._targetless_export_safe_duration(
                            force_window_action,
                            force_discharge_soc_now,
                            (
                                force_discharge_reserve
                                if force_discharge_reserve is not None
                                else 1.0
                            ),
                            extend_mins,
                        )
                        if safe_mins <= 0:
                            _LOGGER.warning(
                                "Optimizer: Canceling active targetless force "
                                "discharge — reserve-safe duration is unavailable"
                            )
                            restore_success = True
                            if hasattr(battery, "restore_normal"):
                                restore_success = await battery.restore_normal()
                            elif hasattr(battery, "set_self_consumption_mode"):
                                restore_success = (
                                    await battery.set_self_consumption_mode()
                                )
                            if restore_success is False:
                                _LOGGER.warning(
                                    "Optimizer: Targetless force-discharge restore "
                                    "failed; retaining force state for retry"
                                )
                                return
                            if force_state.get("scope") == "optimizer":
                                self._clear_optimizer_force_state()
                            elif self._force_state_clearer:
                                self._force_state_clearer()
                            self._last_executed_planned_action = action.action
                            self._last_executed_action = "self_consumption"
                            return
                        if safe_mins < extend_mins:
                            _LOGGER.info(
                                "Optimizer: Shortening targetless force discharge "
                                "from %dmin to reserve-safe %dmin",
                                extend_mins,
                                safe_mins,
                            )
                            extend_mins = safe_mins
                            targetless_window_shortened = True
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
                    now = dt_util.utcnow()
                    refresh_window = timedelta(
                        minutes=max(
                            1,
                            int(getattr(self._config, "interval_minutes", 5) or 5),
                        )
                        + 1
                    )
                    if force_scope == "optimizer":
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
                        # original 30-minute-aligned window. Refresh when that
                        # window is near expiry or the desired expiry extends it
                        # by more than the sub-minute ceil/I/O skew between a
                        # cached boundary command and the immediately fresh LP.
                        should_refresh_hardware = (
                            hardware_expiry is None
                            or hardware_expiry <= now + refresh_window
                            or new_expiry > hardware_expiry + timedelta(minutes=1)
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
                    if (
                        targetless_window_shortened
                        and (
                            hardware_expiry is None
                            or hardware_expiry > new_expiry
                        )
                    ):
                        should_refresh_hardware = True

                    # Re-issue hardware writes when the hardware-side window is
                    # shorter than the extended optimizer-owned force state, or
                    # when the LP changes the target power inside the same mode.
                    if (
                        battery
                        and should_refresh_hardware
                        and (
                            (force_type == "charge" and hasattr(battery, "force_charge"))
                            or (
                                force_type == "discharge"
                                and hasattr(battery, "force_discharge")
                            )
                        )
                    ):
                        try:
                            # For Modbus-backed systems, _extend_hardware
                            # re-issues the inverter countdown. For Tesla, the
                            # service falls through to the full tariff uploader
                            # so the TOU force window is rolled forward too.
                            if force_type == "charge":
                                refresh_result = await battery.force_charge(
                                    duration_minutes=extend_mins,
                                    power_w=force_power_w,
                                    _extend_hardware=True,
                                )
                                if refresh_result is False:
                                    _LOGGER.warning(
                                        "Optimizer: force-charge hardware refresh "
                                        "was not confirmed; retaining prior force "
                                        "state for retry"
                                    )
                                    return
                            else:
                                allowed, applied_power_w = (
                                    await self._force_discharge_through_export_guard(
                                        battery,
                                        force_power_w,
                                        total_battery_discharge_w=(
                                            getattr(
                                                force_window_action,
                                                "battery_discharge_w",
                                                None,
                                            )
                                            if self.battery_system == "solax"
                                            else None
                                        ),
                                        duration_minutes=extend_mins,
                                        _extend_hardware=True,
                                        _tariff_duration=tariff_mins,
                                    )
                                )
                                if not allowed:
                                    if self.battery_system == "solaredge":
                                        _LOGGER.warning(
                                            "Optimizer: SolarEdge force-discharge "
                                            "refresh was not confirmed; retaining "
                                            "the prior force state"
                                        )
                                        return
                                    if force_scope == "optimizer":
                                        self._clear_optimizer_force_state()
                                    elif self._force_state_clearer:
                                        self._force_state_clearer()
                                    self._last_executed_planned_action = action.action
                                    self._last_executed_action = "self_consumption"
                                    return
                                force_power_w = applied_power_w
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
                            if self.battery_system == "solaredge":
                                return

                    solaredge_restore_generation = (
                        getattr(self.energy_coordinator, "generation", None)
                        if self.battery_system == "solaredge"
                        else None
                    )

                    if force_scope != "optimizer":
                        effective_expiry = self._as_utc_datetime(
                            _ext_state.get("expires_at")
                        ) or new_expiry

                        async def _auto_restore_extended(_now):
                            current_expiry = self._as_utc_datetime(
                                _ext_state.get("expires_at")
                            )
                            if current_expiry is not None and _now < current_expiry:
                                _LOGGER.debug(
                                    "Optimizer: force %s expiry was extended — "
                                    "skipping stale restore timer",
                                    force_type,
                                )
                                return
                            if _ext_state.get("active"):
                                _LOGGER.info("⏰ Force %s expired (extended timer), auto-restoring", force_type)
                                from ..const import DOMAIN as _SVC_DOMAIN
                                restore_data = {}
                                if self.battery_system == "solaredge":
                                    restore_data = {
                                        "source": "optimizer",
                                        "_allow_monitoring_restore": True,
                                        "_solaredge_generation": (
                                            solaredge_restore_generation
                                        ),
                                    }
                                await self.hass.services.async_call(
                                    _SVC_DOMAIN,
                                    "restore_normal",
                                    restore_data,
                                    blocking=True,
                                )

                        from homeassistant.helpers.event import async_track_point_in_utc_time
                        # A full hardware refresh can install a new service-owned
                        # timer while awaited above. Cancel that timer before the
                        # coordinator takes ownership of the extended expiry.
                        if _ext_state.get("cancel_expiry_timer"):
                            _ext_state["cancel_expiry_timer"]()
                        _ext_state["cancel_expiry_timer"] = async_track_point_in_utc_time(
                            self.hass, _auto_restore_extended, effective_expiry,
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

                if not cap_cancelled_for_new_action:
                    # LP changed its mind — cancel the optimizer's force mode.
                    if (
                        action.action in SELF_USE_ACTIONS
                        or action.action == "idle"
                        or bridged_export_gap
                    ):
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
                                try:
                                    commitment_soc, _ = (
                                        await self._get_battery_state()
                                    )
                                    commitment_reserve = (
                                        self._force_discharge_reserve_floor(action)
                                    )
                                    capacity_wh = float(
                                        getattr(
                                            self._config,
                                            "battery_capacity_wh",
                                            0.0,
                                        )
                                        or 0.0
                                    )
                                    command_w = max(
                                        0.0,
                                        float(force_state.get("power_w", 0.0) or 0.0),
                                    )
                                    efficiency = float(
                                        getattr(
                                            getattr(self, "_optimizer", None),
                                            "efficiency",
                                            0.92,
                                        )
                                        or 0.92
                                    )
                                    if (
                                        commitment_soc is None
                                        or capacity_wh <= 0
                                        or command_w <= 0
                                        or not 0 < efficiency <= 1
                                    ):
                                        commitment_remaining = None
                                    else:
                                        projected_soc = commitment_soc - (
                                            command_w
                                            * commitment_remaining.total_seconds()
                                            / 3600.0
                                            / efficiency
                                            / capacity_wh
                                        )
                                        if projected_soc < commitment_reserve - 0.0001:
                                            _LOGGER.info(
                                                "Optimizer: Releasing force discharge "
                                                "commitment — projected SOC %.1f%% would "
                                                "cross reserve %.0f%%",
                                                projected_soc * 100,
                                                commitment_reserve * 100,
                                            )
                                            commitment_remaining = None
                                except Exception as err:
                                    _LOGGER.warning(
                                        "Optimizer: Releasing force discharge "
                                        "commitment because reserve safety could not "
                                        "be verified: %s",
                                        err,
                                    )
                                    commitment_remaining = None
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
                    optimizer_force_snapshot = None
                    if force_state.get("scope") == "optimizer":
                        optimizer_force_snapshot = dict(self._optimizer_force_state)
                        self._clear_optimizer_force_state()
                    elif self._force_state_clearer:
                        self._force_state_clearer()
                    battery = self._executor.battery_controller
                    restore_success = True
                    if hasattr(battery, "restore_normal"):
                        restore_success = await battery.restore_normal()
                    if restore_success is False:
                        if optimizer_force_snapshot is not None:
                            self._optimizer_force_state = optimizer_force_snapshot
                        _LOGGER.warning(
                            "Optimizer: Restore after canceling force %s failed; "
                            "retaining optimizer force state for retry",
                            force_type,
                        )
                        return
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
            effective_action = runtime_action

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
            # An RTE-justified hold is not the "not enough charge to bother
            # holding" case this band was written for: the optimizer already
            # proved that discharging now and rebuying the same energy later
            # costs more. Remapping it here would silently undo that plan.
            rte_economic_hold = (
                getattr(action, "reason", None) == RTE_ECONOMIC_HOLD_REASON
            )
            if effective_action == "idle" and not rte_economic_hold:
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
                    _current_export = self._current_effective_export_price_for_action(
                        _ep,
                        action,
                    )
                    if _current_export is None:
                        _current_export = _ep[0] if _ep else 0
                    from ..curtailment_config import get_curtailment_price_thresholds

                    curtail_threshold_cents, _ = get_curtailment_price_thresholds(
                        self._entry
                    )
                    if _current_export < curtail_threshold_cents / 100.0:
                        _LOGGER.info(
                            "Optimizer: Overriding %s → self_consumption — "
                            "export price %.1fc/kWh < %.1fc threshold",
                            effective_action,
                            _current_export * 100,
                            curtail_threshold_cents,
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
                if getattr(self, "_idle_no_discharge_active", False):
                    if not await self._restore_idle_no_discharge_mode(
                        "optimizer action transition"
                    ):
                        _LOGGER.warning(
                            "Optimizer: Failed to release IDLE no-discharge mode "
                            "(will retry next cycle)"
                        )
                        return
                elif (
                    self.energy_coordinator
                    and hasattr(self.energy_coordinator, "restore_work_mode_from_idle")
                ):
                    work_mode_restored = (
                        await self._call_optimizer_energy_control(
                            "restore_work_mode_from_idle"
                        )
                    )
                    if work_mode_restored is False:
                        _LOGGER.warning(
                            "Optimizer: Failed to restore work mode while exiting "
                            "IDLE (will retry next cycle)"
                        )
                        return
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
            export_soc_now: float | None = None
            export_reserve: float | None = None
            if effective_action in ("discharge", "export"):
                try:
                    export_soc_now, _ = await self._get_battery_state()
                    export_reserve = self._force_discharge_reserve_floor(action)
                    reaches_reserve, projected_soc = (
                        self._force_discharge_reaches_reserve(
                            action,
                            export_soc_now,
                            export_reserve,
                        )
                    )
                    if reaches_reserve:
                        soc_text = (
                            f"{export_soc_now * 100:.1f}%"
                            if export_soc_now is not None
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
                            export_reserve * 100,
                        )
                        effective_action = "self_consumption"
                except Exception as reserve_err:
                    if not self._supports_target_export_power():
                        _LOGGER.warning(
                            "Optimizer: Blocking targetless %s because its "
                            "reserve-safe duration could not be verified: %s",
                            effective_action,
                            reserve_err,
                        )
                        effective_action = "self_consumption"

            # A cached boundary action owns this slot once the fresh periodic
            # solve is genuinely late. Normal boundary solves finish a few
            # seconds after the cached action is applied; suppressing those for
            # the whole interval can miss a newly-started tariff window. Keep a
            # short boundary grace, then preserve the cached action for the
            # rest of the slot. Safety gates above may still turn a forced plan
            # into self-consumption; explicit price/settings/startup/manual
            # runs do not pass the polling trigger and retain immediate
            # execution authority.
            boundary_execution = getattr(self, "_boundary_execution", None)
            if (
                execution_trigger == "poll"
                and effective_action in FORCED_ACTIONS
                and boundary_execution
                and not boundary_execution.get("was_forced", False)
            ):
                now = dt_util.now()
                slot_start = boundary_execution.get("slot_start")
                slot_end = boundary_execution.get("slot_end")
                if (
                    isinstance(slot_start, datetime)
                    and isinstance(slot_end, datetime)
                    and slot_start <= now < slot_end
                    and now - slot_start >= BOUNDARY_FRESH_SOLVE_GRACE
                ):
                    _LOGGER.info(
                        "Optimizer: deferring periodic mid-slot %s after cached %s "
                        "until boundary %s",
                        effective_action,
                        boundary_execution.get("action"),
                        slot_end.isoformat(),
                    )
                    return

            if effective_action == "charge":
                if self._grid_charge_blackout_active_now():
                    _LOGGER.info(
                        "Optimizer: Blocking force charge during configured "
                        "grid-charge blackout; restoring self_consumption"
                    )
                    restore_success = True
                    if hasattr(battery, "restore_normal"):
                        restore_success = await battery.restore_normal()
                    elif hasattr(battery, "set_self_consumption_mode"):
                        restore_success = await battery.set_self_consumption_mode()
                    if restore_success is False:
                        _LOGGER.warning(
                            "Optimizer: blackout restore failed; next cycle will retry"
                        )
                        return
                    self._last_executed_planned_action = action.action
                    self._last_executed_action = "self_consumption"
                    return
                try:
                    cap_reached, charge_soc_now, charge_soc_cap = (
                        await self._grid_charge_soc_cap_reached()
                    )
                except Exception as cap_err:
                    cap_reached = True
                    charge_soc_now = None
                    charge_soc_cap = self._soc_ratio(
                        getattr(self._config, "grid_charge_soc_cap", 1.0),
                        1.0,
                    )
                    _LOGGER.warning(
                        "Optimizer: grid-charge SOC-cap check before force charge "
                        "failed; blocking conservatively: %s",
                        cap_err,
                    )
                if cap_reached:
                    if (
                        getattr(self, "_last_executed_planned_action", None)
                        == action.action
                        and self._last_executed_action == "self_consumption"
                    ):
                        return
                    if charge_soc_now is None:
                        _LOGGER.warning(
                            "Optimizer: Blocking charge — live SOC could not be "
                            "verified against grid-charge cap %.0f%%; restoring "
                            "self_consumption",
                            charge_soc_cap * 100,
                        )
                    else:
                        _LOGGER.warning(
                            "Optimizer: Blocking charge — live SOC %.1f%% reached "
                            "grid-charge cap %.0f%%; restoring self_consumption",
                            charge_soc_now * 100,
                            charge_soc_cap * 100,
                        )
                    restore_success = True
                    if hasattr(battery, "restore_normal"):
                        restore_success = await battery.restore_normal()
                    elif hasattr(battery, "set_self_consumption_mode"):
                        restore_success = await battery.set_self_consumption_mode()
                    if restore_success is False:
                        _LOGGER.warning(
                            "Optimizer: Grid-charge SOC-cap restore failed; "
                            "keeping previous action marker so the next cycle retries"
                        )
                        return
                    self._last_executed_planned_action = action.action
                    self._last_executed_action = "self_consumption"
                    return
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
                    if not self._supports_target_charge_power():
                        safe_duration, projected_soc = (
                            self._targetless_charge_safe_duration(
                                action,
                                charge_soc_now,
                                charge_soc_cap,
                                charge_duration,
                            )
                        )
                        if safe_duration <= 0:
                            if (
                                getattr(
                                    self,
                                    "_last_executed_planned_action",
                                    None,
                                )
                                == action.action
                                and self._last_executed_action
                                == "self_consumption"
                            ):
                                return
                            projected_text = (
                                f" (full-rate projection "
                                f"{projected_soc * 100:.1f}%)"
                                if projected_soc is not None
                                else ""
                            )
                            _LOGGER.warning(
                                "Optimizer: Blocking targetless charge at %.0fW%s "
                                "because it cannot fit one whole fixed-power minute "
                                "below the %.0f%% grid-charge cap; restoring "
                                "self_consumption",
                                charge_power_w,
                                projected_text,
                                charge_soc_cap * 100,
                            )
                            restore_success = True
                            if hasattr(battery, "restore_normal"):
                                restore_success = await battery.restore_normal()
                            elif hasattr(battery, "set_self_consumption_mode"):
                                restore_success = (
                                    await battery.set_self_consumption_mode()
                                )
                            if restore_success is False:
                                _LOGGER.warning(
                                    "Optimizer: Targetless charge block restore "
                                    "failed; keeping previous action marker so the "
                                    "next cycle retries"
                                )
                                return
                            self._last_executed_planned_action = action.action
                            self._last_executed_action = "self_consumption"
                            return
                        if safe_duration < charge_duration:
                            _LOGGER.info(
                                "Optimizer: Shortening targetless charge from "
                                "%dmin to %dmin so the fixed-power command stays "
                                "below the %.0f%% grid-charge cap",
                                charge_duration,
                                safe_duration,
                                charge_soc_cap * 100,
                            )
                            charge_duration = safe_duration
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
                        if force_result is False:
                            _LOGGER.warning(
                                "Optimizer: force-charge command failed — keeping "
                                "previous action marker so the next cycle retries"
                            )
                            return
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
                        if force_result is False:
                            _LOGGER.warning(
                                "Optimizer: force-charge command failed — keeping "
                                "previous action marker so the next cycle retries"
                            )
                            return
                        if force_result is not False and self.battery_system != "tesla":
                            self._set_optimizer_force_state(
                                "charge",
                                charge_duration,
                                charge_power_w,
                            )
                        _LOGGER.info("Optimizer: Charging at %.0fW", charge_power_w)
            elif effective_action == "solar_export":
                if solar_export_hold is None:
                    _LOGGER.error(
                        "Profit Max: solar-export hold controller unavailable; "
                        "using self-consumption"
                    )
                    effective_action = "self_consumption"
                else:
                    timestamp = getattr(action, "timestamp", None)
                    generation = (
                        timestamp.isoformat()
                        if isinstance(timestamp, datetime)
                        else str(timestamp or "current")
                    )
                    applied = await solar_export_hold.apply(
                        self.entry_id,
                        generation,
                    )
                    if not applied:
                        _LOGGER.error(
                            "Profit Max: solar-export hold failed verification; "
                            "restoring ordinary self-consumption"
                        )
                        effective_action = "self_consumption"
                        if hasattr(battery, "set_self_consumption_mode"):
                            await battery.set_self_consumption_mode()
                        elif hasattr(battery, "restore_normal"):
                            await battery.restore_normal()
                    else:
                        _LOGGER.info(
                            "Profit Max: holding battery charge at zero for "
                            "direct solar export (%.0fW planned)",
                            float(getattr(action, "power_w", 0.0) or 0.0),
                        )
            elif effective_action in ("discharge", "export"):
                if hasattr(battery, "force_discharge"):
                    discharge_power = self._export_command_power_w(action)
                    discharge_duration = self._force_duration_for_action_window(
                        action,
                        {"discharge", "export"},
                        allow_boundary_overrun=False,
                        minimum_minutes=self._config.interval_minutes + 5,
                    )
                    if not self._supports_target_export_power():
                        safe_mins, _ = self._targetless_export_safe_duration(
                            action,
                            export_soc_now,
                            export_reserve if export_reserve is not None else 1.0,
                            discharge_duration,
                        )
                        if safe_mins <= 0:
                            _LOGGER.warning(
                                "Optimizer: Blocking targetless %s because no "
                                "reserve-safe force duration remains",
                                effective_action,
                            )
                            mode_result = True
                            if hasattr(battery, "set_self_consumption_mode"):
                                mode_result = (
                                    await battery.set_self_consumption_mode()
                                )
                            elif hasattr(battery, "restore_normal"):
                                mode_result = await battery.restore_normal()
                            if mode_result is False:
                                _LOGGER.warning(
                                    "Optimizer: Failed to restore self-consumption "
                                    "after blocking targetless export"
                                )
                                return
                            self._last_executed_planned_action = planned_action
                            self._last_executed_action = "self_consumption"
                            return
                        if safe_mins < discharge_duration:
                            _LOGGER.info(
                                "Optimizer: Shortening targetless %s from %dmin "
                                "to reserve-safe %dmin",
                                effective_action,
                                discharge_duration,
                                safe_mins,
                            )
                            discharge_duration = safe_mins
                    tariff_duration = self._tesla_tariff_duration_for_force_window(
                        discharge_duration
                    )
                    force_result, discharge_power = (
                        await self._force_discharge_through_export_guard(
                            battery,
                            discharge_power,
                            total_battery_discharge_w=(
                                getattr(action, "battery_discharge_w", None)
                                if self.battery_system == "solax"
                                else None
                            ),
                            duration_minutes=discharge_duration,
                            _tariff_duration=tariff_duration,
                        )
                    )
                    if force_result and self.battery_system != "tesla":
                        self._set_optimizer_force_state(
                            "discharge",
                            discharge_duration,
                            discharge_power,
                        )
                    if not force_result:
                        if self.battery_system == "solaredge":
                            _LOGGER.warning(
                                "Optimizer: SolarEdge force-discharge command was "
                                "not confirmed; keeping the previous action marker"
                            )
                            return
                        effective_action = "self_consumption"
                    else:
                        _LOGGER.info(
                            "Optimizer: Discharging/exporting at %.0fW for %dmin",
                            discharge_power,
                            discharge_duration,
                        )
            elif effective_action == "no_discharge":
                await self._set_scheduled_ev_no_discharge_mode(
                    battery,
                    getattr(action, "action", "scheduled_ev_preserve"),
                )
            elif effective_action == "idle":
                if await self._set_idle_hold_mode(
                    battery,
                    preserve_charge=self._should_disable_idle_schedule(),
                ) is False:
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
                    desired_reserve_pct: int | None = None
                    current_reserve: int | None = None
                    current_reserve_trust = None
                    tesla_stale_grid_charge = False
                    last_optimizer_reserve_target = getattr(
                        self,
                        "_last_optimizer_self_consumption_reserve_target",
                        None,
                    )
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
                            desired_reserve_pct = (
                                self._startup_backup_reserve
                                if self._startup_backup_reserve is not None
                                else configured_reserve_pct
                            )
                            reserve_pct = max(0, min(100, desired_reserve_pct))
                            if 81 <= reserve_pct <= 99:
                                reserve_pct = 80
                            if hasattr(battery, "read_backup_reserve"):
                                current_reserve_reading = await battery.read_backup_reserve()
                                current_reserve = current_reserve_reading.percent
                                current_reserve_trust = current_reserve_reading.trust
                            else:
                                current_reserve = await battery.get_backup_reserve()
                            # Only protect a lower reserve that matches the target
                            # PowerSync successfully wrote earlier in this runtime.
                            # The authoritative startup/manual hardware reserve is
                            # never lowered merely because current SOC is below it.
                            if (
                                current_reserve is not None
                                and last_optimizer_reserve_target is not None
                                and current_reserve == last_optimizer_reserve_target
                                and desired_reserve_pct is not None
                                and last_optimizer_reserve_target < desired_reserve_pct
                                and current_reserve != 100
                                and not 81 <= current_reserve <= 99
                            ):
                                reserve_pct = max(reserve_pct, current_reserve)
                            if (
                                not apply_self_consumption
                                and current_reserve is not None
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
                            stale_charge = self._tesla_stale_grid_charge_reading(
                                reserve_pct,
                            )
                            if stale_charge is not None:
                                last_restore = getattr(
                                    self,
                                    "_last_tesla_stale_grid_charge_restore_at",
                                    None,
                                )
                                now = dt_util.utcnow()
                                if (
                                    last_restore is None
                                    or now - last_restore
                                    >= TESLA_STALE_GRID_CHARGE_RESTORE_COOLDOWN
                                ):
                                    charge_kw, grid_import_kw, stale_soc_pct = (
                                        stale_charge
                                    )
                                    _LOGGER.warning(
                                        "Optimizer: Tesla is still grid-charging "
                                        "%.2fkW (grid import %.2fkW, SOC %.1f%%, "
                                        "reserve %d%%) while LP action and hardware "
                                        "mode are self_consumption — restoring the "
                                        "normal tariff and controls",
                                        charge_kw,
                                        grid_import_kw,
                                        stale_soc_pct,
                                        reserve_pct,
                                    )
                                    apply_self_consumption = True
                                    tesla_stale_grid_charge = True
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
                            if getattr(
                                self.energy_coordinator,
                                "pending_optimizer_export_restore",
                                False,
                            ):
                                _LOGGER.info(
                                    "Optimizer: Sungrow has a pending optimizer-owned "
                                    "export-limit restore — reapplying restore_normal"
                                )
                                apply_self_consumption = True

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
                    # A mode transition skips the existing-mode verification
                    # above, so resolve its Tesla target separately. Apply the
                    # provenance-aware ratchet guard without lowering the
                    # authoritative hardware reserve to current SOC.
                    if (
                        apply_self_consumption
                        and reserve_pct is None
                        and self.battery_system == "tesla"
                        and hasattr(battery, "get_backup_reserve")
                    ):
                        soc_now, _ = await self._get_battery_state()
                        soc_pct = max(0, min(100, int(soc_now * 100)))
                        desired_reserve_pct = (
                            self._startup_backup_reserve
                            if self._startup_backup_reserve is not None
                            else configured_reserve_pct
                        )
                        reserve_pct = max(0, min(100, desired_reserve_pct))
                        if 81 <= reserve_pct <= 99:
                            reserve_pct = 80
                        if hasattr(battery, "read_backup_reserve"):
                            current_reserve = (
                                await battery.read_backup_reserve()
                            ).percent
                        else:
                            current_reserve = await battery.get_backup_reserve()
                        if (
                            current_reserve is not None
                            and last_optimizer_reserve_target is not None
                            and current_reserve == last_optimizer_reserve_target
                            and desired_reserve_pct is not None
                            and last_optimizer_reserve_target < desired_reserve_pct
                            and current_reserve != 100
                            and not 81 <= current_reserve <= 99
                        ):
                            reserve_pct = max(reserve_pct, current_reserve)
                    mode_apply_failed = False
                    if apply_self_consumption or reapply_backup_reserve:
                        if (
                            tesla_stale_grid_charge
                            and hasattr(battery, "restore_normal")
                        ):
                            if await battery.restore_normal(force_restore=True) is False:
                                mode_apply_failed = True
                            else:
                                self._last_tesla_stale_grid_charge_restore_at = (
                                    dt_util.utcnow()
                                )
                        elif hasattr(battery, "set_self_consumption_mode"):
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
                        # Tesla only: restore the authoritative hardware backup
                        # reserve after an action transition. Modbus batteries
                        # such as GoodWe expose this as a real hardware/DOD
                        # setting, so ordinary self-consumption must not rewrite
                        # it to the LP floor.
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
                            reserve_result = await battery.set_backup_reserve(reserve_pct)
                            if reserve_result is not False:
                                self._last_optimizer_self_consumption_reserve_target = (
                                    reserve_pct
                                )
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
                        self._last_optimizer_self_consumption_reserve_target = None
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
        if zerohero_config is not None:
            slot_sources.append(self._zerohero_bonus_window_slots(n))
        else:
            slot_sources.insert(0, self._positive_price_export_slots(n, export_prices))

        for slots in slot_sources:
            for idx, value in enumerate(slots[:n]):
                allowed[idx] = allowed[idx] or value

        min_export_price = normalize_min_export_price(
            getattr(self._config, "min_export_price", 0.0)
        )
        if min_export_price > 0:
            real_price_allowed = battery_export_allowed_slots(
                (export_prices or [])[:n],
                min_export_price,
            )
            allowed = [
                bool(value) and idx < len(real_price_allowed) and real_price_allowed[idx]
                for idx, value in enumerate(allowed)
            ]

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
            self._agl_battery_reward_export_slots(n, export_prices),
            self._flow_power_export_window_slots(n),
            self._export_boost_mask_for_run(n, export_prices),
            self._saving_session_export_slots(n),
        ]
        zerohero_config = self._zerohero_config()
        if zerohero_config is not None:
            slot_sources.append(self._zerohero_bonus_window_slots(n))
        if self._provider_key() == "covau":
            slot_sources.append(self._covau_export_window_slots(n))

        for slots in slot_sources:
            for idx, value in enumerate(slots[:n]):
                allowed[idx] = allowed[idx] or value

        min_export_price = normalize_min_export_price(
            getattr(self._config, "min_export_price", 0.0)
        )
        if min_export_price > 0:
            real_price_allowed = battery_export_allowed_slots(
                (export_prices or [])[:n],
                min_export_price,
            )
            allowed = [
                bool(value) and idx < len(real_price_allowed) and real_price_allowed[idx]
                for idx, value in enumerate(allowed)
            ]

        allowed_count = sum(allowed)
        if allowed_count:
            _LOGGER.debug(
                "Priority export enabled in %d/%d optimizer intervals",
                allowed_count,
                n,
            )
        return allowed

    def _spread_export_prices_for_run(
        self,
        export_prices: list[float],
    ) -> list[float]:
        """Return slot prices including any bounded provider export bonus."""
        bonus_prices = list(self._last_zerohero_bonus_prices or [])
        return [
            float(base_price or 0.0)
            + (
                float(bonus_prices[idx] or 0.0)
                if idx < len(bonus_prices)
                else 0.0
            )
            for idx, base_price in enumerate(export_prices)
        ]

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

            # Self-consumption slots can still carry forecast solar charging
            # already counted by LP SOC floors.  Keep those slots in place so
            # spreading deliberate charge energy cannot erase deadline energy.
            preserved_natural_positions = {
                pos
                for pos in range(start, end)
                if getattr(actions[pos], "action", None) != "charge"
                and max(
                    0.0,
                    float(
                        getattr(actions[pos], "battery_charge_w", 0.0)
                        or 0.0
                    ),
                )
                > 0.0
            }
            spread_positions = [
                pos
                for pos in range(start, end)
                if pos not in preserved_natural_positions
            ]
            charge_wh = sum(
                max(0.0, float(getattr(action, "battery_charge_w", 0.0) or 0.0))
                * interval_hours
                for action in actions[start:end]
                if getattr(action, "action", None) == "charge"
            )
            if charge_wh <= 0 or max_charge_w <= 0:
                for pos in range(start, end):
                    soc_cursor = _advance_soc(soc_cursor, new_actions[pos])
                continue

            if price <= 0.001 and capacity_wh > 0:
                natural_charge_wh = sum(
                    max(
                        0.0,
                        float(
                            getattr(actions[pos], "battery_charge_w", 0.0)
                            or 0.0
                        ),
                    )
                    * interval_hours
                    for pos in preserved_natural_positions
                )
                available_wh = max(
                    0.0,
                    (1.0 - soc_cursor)
                    * capacity_wh
                    / max(efficiency, 0.001)
                    - natural_charge_wh,
                )
                charge_wh = min(charge_wh, available_wh)
                if charge_wh <= 0:
                    for pos in range(start, end):
                        soc_cursor = _advance_soc(soc_cursor, new_actions[pos])
                    continue

            if cap_by_slot:
                target_by_pos = _spread_power_by_cap(
                    charge_wh,
                    [_slot_charge_cap_w(pos) for pos in spread_positions],
                )
            else:
                target_w = min(
                    max_charge_w,
                    charge_wh / (len(spread_positions) * interval_hours),
                )
                target_w = round(max(0.0, target_w), 1)
                target_by_pos = [target_w] * len(spread_positions)

            if not any(target_w > 0 for target_w in target_by_pos):
                for pos in range(start, end):
                    soc_cursor = _advance_soc(soc_cursor, new_actions[pos])
                continue

            spread_targets = dict(zip(spread_positions, target_by_pos))
            for pos in range(start, end):
                original = actions[pos]
                if pos in preserved_natural_positions:
                    soc_cursor = _advance_soc(soc_cursor, original)
                    new_actions[pos] = ScheduleAction(
                        timestamp=original.timestamp,
                        action=original.action,
                        power_w=original.power_w,
                        soc=round(soc_cursor, 4),
                        battery_charge_w=original.battery_charge_w,
                        battery_discharge_w=original.battery_discharge_w,
                        ev_charge_w=getattr(original, "ev_charge_w", 0.0),
                    )
                    continue
                target_w = spread_targets[pos]
                if target_w > 0:
                    new_actions[pos] = ScheduleAction(
                        timestamp=original.timestamp,
                        action="charge",
                        power_w=target_w,
                        soc=original.soc,
                        battery_charge_w=target_w,
                        battery_discharge_w=0.0,
                        ev_charge_w=getattr(original, "ev_charge_w", 0.0),
                    )
                else:
                    new_actions[pos] = ScheduleAction(
                        timestamp=original.timestamp,
                        action="self_consumption",
                        power_w=0.0,
                        soc=original.soc,
                        battery_charge_w=0.0,
                        battery_discharge_w=0.0,
                        ev_charge_w=getattr(original, "ev_charge_w", 0.0),
                    )
                soc_cursor = _advance_soc(soc_cursor, new_actions[pos])
                new_actions[pos].soc = round(soc_cursor, 4)

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
        *,
        export_prices: list[float] | None = None,
    ) -> OptimizationSchedule:
        """Spread planned export energy across each same-price allowed window."""
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

        prices: list[float] | None = None
        if export_prices is not None:
            try:
                candidate_prices = [float(price) for price in export_prices[:n]]
            except (TypeError, ValueError):
                candidate_prices = []
            if (
                len(candidate_prices) == n
                and all(math.isfinite(price) for price in candidate_prices)
            ):
                prices = candidate_prices

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

        def _battery_home_discharge_w(action: ScheduleAction) -> float:
            discharge_w = max(
                0.0,
                float(getattr(action, "battery_discharge_w", 0.0) or 0.0),
            )
            if getattr(action, "action", None) in SELF_USE_ACTIONS:
                return discharge_w
            if getattr(action, "action", None) in EXPORT_ACTIONS:
                export_w = max(
                    0.0,
                    min(
                        float(getattr(action, "power_w", 0.0) or 0.0),
                        discharge_w,
                    ),
                )
                return max(0.0, discharge_w - export_w)
            return 0.0

        def _advance_discharge_soc(soc: float, battery_discharge_w: float) -> float:
            if capacity_wh <= 0:
                return soc
            removed_wh = (
                max(0.0, battery_discharge_w)
                * interval_hours
                / max(efficiency, 0.001)
            )
            return max(0.0, min(1.0, soc - removed_wh / capacity_wh))

        def _advance_action_soc(soc: float, action: ScheduleAction) -> float:
            if capacity_wh <= 0:
                return soc
            charge_w = max(
                0.0,
                float(getattr(action, "battery_charge_w", 0.0) or 0.0),
            )
            discharge_w = max(
                0.0,
                float(getattr(action, "battery_discharge_w", 0.0) or 0.0),
            )
            stored_wh = charge_w * interval_hours * max(efficiency, 0.001)
            removed_wh = (
                discharge_w
                * interval_hours
                / max(efficiency, 0.001)
            )
            return max(
                0.0,
                min(1.0, soc + (stored_wh - removed_wh) / capacity_wh),
            )

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
            window_price = prices[idx] if prices is not None else 0.0
            while (
                idx < n
                and allowed[idx]
                and (
                    prices is None
                    or abs(prices[idx] - window_price) <= 1e-6
                )
            ):
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
            first_export_pos = next(
                pos
                for pos in spread_positions
                if getattr(actions[pos], "action", None) in ("export", "discharge")
            )
            if floor is not None:
                # SOC labels after the first raw export describe the concentrated
                # LP plan that this pass is about to replace. Using those depleted
                # labels to select later slots makes the spread denominator collapse
                # at the reserve floor and leaves export pinned at the original cap.
                # Leading slots still use their pre-export SOC labels so a window
                # that begins below the floor cannot manufacture an export action.
                spread_positions = [
                    pos
                    for pos in spread_positions
                    if pos >= first_export_pos
                    or (
                        self._reserve_ratio(
                            getattr(actions[pos], "soc", None),
                            None,
                        )
                        or 0.0
                    )
                    > floor + 0.0001
                ]

            export_cap_w = (
                self._config.max_grid_export_w
                if self._config.max_grid_export_w is not None
                else self._config.max_discharge_w
            )
            export_cap_w = float(max(0, export_cap_w))
            spread_position_set = set(spread_positions)
            headroom_by_position = {
                pos: min(
                    export_cap_w,
                    max(
                        0.0,
                        float(self._config.max_discharge_w or 0)
                        - _battery_home_discharge_w(actions[pos]),
                    ),
                )
                for pos in spread_positions
            }
            window_start_soc = _action_soc(start - 1)
            if window_start_soc is None:
                window_start_soc = _action_soc(start)

            def _rounded_export_target(level_w: float, pos: int) -> float:
                return round(
                    max(0.0, min(level_w, headroom_by_position[pos])),
                    1,
                )

            def _common_export_level_is_feasible(level_w: float) -> bool:
                target_export_wh = sum(
                    _rounded_export_target(level_w, pos) * interval_hours
                    for pos in spread_positions
                )
                if target_export_wh > export_wh + 1e-6:
                    return False

                candidate_soc = window_start_soc
                for pos in range(start, end):
                    original = actions[pos]
                    if pos not in spread_position_set:
                        if candidate_soc is not None:
                            candidate_soc = _advance_action_soc(candidate_soc, original)
                        continue

                    forced_export_w = _rounded_export_target(level_w, pos)
                    if candidate_soc is None:
                        continue
                    candidate_soc = _advance_discharge_soc(
                        candidate_soc,
                        _battery_home_discharge_w(original) + forced_export_w,
                    )
                    if (
                        floor is not None
                        and forced_export_w > 0
                        and candidate_soc < floor - 1e-9
                    ):
                        return False
                return True

            # Reserve and forecast-home-load modes may reduce the common export
            # level, but must not shorten the window. Search one capped water
            # level against the real sequential SOC path so every eligible slot
            # either exports for the whole window or none are force-exported.
            target_by_position = {pos: 0.0 for pos in spread_positions}
            if spread_positions and all(
                round(headroom_by_position[pos], 1) > 0
                for pos in spread_positions
            ):
                low_w = 0.0
                high_w = max(headroom_by_position.values())
                if _common_export_level_is_feasible(high_w):
                    low_w = high_w
                else:
                    for _ in range(24):
                        mid_w = (low_w + high_w) / 2.0
                        if _common_export_level_is_feasible(mid_w):
                            low_w = mid_w
                        else:
                            high_w = mid_w
                target_by_position = {
                    pos: _rounded_export_target(low_w, pos)
                    for pos in spread_positions
                }

            soc_cursor = _action_soc(start - 1)
            if soc_cursor is None:
                soc_cursor = _action_soc(start)
            for pos in range(start, end):
                original = actions[pos]
                if pos not in spread_position_set:
                    if soc_cursor is not None:
                        soc_cursor = _advance_action_soc(soc_cursor, original)
                    continue
                home_discharge_w = _battery_home_discharge_w(original)
                slot_target_w = target_by_position[pos]
                slot_target_w = min(
                    slot_target_w,
                    max(
                        0.0,
                        float(self._config.max_discharge_w or 0) - home_discharge_w,
                    ),
                )
                if floor is not None and soc_cursor is not None:
                    slot_target_w = min(
                        slot_target_w,
                        max(
                            0.0,
                            _available_export_w(soc_cursor, floor) - home_discharge_w,
                        ),
                    )
                    slot_target_w = round(max(0.0, slot_target_w), 1)
                if slot_target_w > 0:
                    battery_discharge_w = home_discharge_w + slot_target_w
                    soc_after = (
                        _advance_discharge_soc(soc_cursor, battery_discharge_w)
                        if soc_cursor is not None
                        else original.soc
                    )
                    new_actions[pos] = ScheduleAction(
                        timestamp=original.timestamp,
                        action="export",
                        power_w=slot_target_w,
                        soc=round(soc_after, 4) if soc_cursor is not None else original.soc,
                        battery_charge_w=0.0,
                        battery_discharge_w=battery_discharge_w,
                        ev_charge_w=getattr(original, "ev_charge_w", 0.0),
                    )
                    if soc_cursor is not None:
                        soc_cursor = soc_after
                else:
                    battery_discharge_w = home_discharge_w
                    soc_after = (
                        _advance_discharge_soc(soc_cursor, battery_discharge_w)
                        if soc_cursor is not None
                        else original.soc
                    )
                    new_actions[pos] = ScheduleAction(
                        timestamp=original.timestamp,
                        action="self_consumption",
                        power_w=battery_discharge_w,
                        soc=(
                            round(soc_after, 4)
                            if soc_cursor is not None
                            else original.soc
                        ),
                        battery_charge_w=0.0,
                        battery_discharge_w=battery_discharge_w,
                        ev_charge_w=getattr(original, "ev_charge_w", 0.0),
                    )
                    if soc_cursor is not None:
                        soc_cursor = soc_after

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

    def _solar_export_capability(self) -> dict[str, Any]:
        """Resolve the fail-closed hardware contract for solar-only export."""
        hold = getattr(self, "_solar_export_hold", None)
        if self._monitoring_mode_active():
            status = {"supported": False, "reason": "monitoring_mode"}
        elif hold is None:
            status = {"supported": False, "reason": "adapter_unavailable"}
        else:
            try:
                status = dict(hold.capability() or {})
            except Exception as err:
                status = {
                    "supported": False,
                    "reason": "capability_check_failed",
                    "last_error": str(err),
                }
        if status.get("supported") and not status.get("export_limit_kw"):
            limit_w = getattr(getattr(self, "_config", None), "max_grid_export_w", None)
            try:
                limit_kw = None if limit_w is None else float(limit_w) / 1000.0
            except (TypeError, ValueError):
                limit_kw = None
            if limit_kw is not None and not math.isfinite(limit_kw):
                limit_kw = None
            if limit_kw is not None and limit_kw > 0:
                status["export_limit_kw"] = limit_kw
                status["export_limit_source"] = "site_setting"
            else:
                # A blank field and a deliberate 0 are different sites: one
                # needs the setting filled in because nothing upstream reports
                # a cap, the other can never export at all. Reporting both as
                # one reason told zero-export users to set a limit they had
                # already set, and told everyone else nothing actionable.
                status = {
                    **status,
                    "supported": False,
                    "reason": (
                        "zero_export_site"
                        if limit_kw is not None
                        else "export_limit_not_configured"
                    ),
                }
        elif status.get("export_limit_kw"):
            status.setdefault("export_limit_source", "inverter_reported")
        self._sync_solar_export_capability_notice(status)
        self._solar_export_capability_status = status
        return status

    def _sync_solar_export_capability_notice(
        self, status: dict[str, Any]
    ) -> None:
        """Surface capability refusals the user can act on, exactly once.

        Without this the only trace of a blocked solar export was a nested
        field on the optimization status sensor, so a site whose export cap is
        simply unset looked identical to one where the economics never paid.
        """
        reason = str(status.get("reason") or "")

        outage = None
        if reason == "upstream_integration_not_loaded":
            outage = (
                reason,
                str(status.get("upstream_domain") or "upstream integration"),
                str(status.get("upstream_state") or "unknown"),
            )
            if outage != getattr(
                self, "_last_solar_export_upstream_outage", None
            ):
                _LOGGER.warning(
                    "Profit Max solar export unavailable: %s (%s state=%s)",
                    *outage,
                )
        self._last_solar_export_upstream_outage = outage

        notice = reason if reason in _SOLAR_EXPORT_LIMIT_REASONS else None
        previous = getattr(
            self, "_last_solar_export_limit_notice", _SOLAR_EXPORT_NOTICE_UNSYNCED
        )
        if notice == previous:
            return
        if notice == "export_limit_not_configured":
            _LOGGER.warning(
                "Profit Max solar export is disabled: no site export limit "
                "is available. This battery connection does not report one, "
                "so set Smart Optimization -> Grid & site constraints -> "
                "Maximum grid export to your site/DNSP export cap in kW"
            )
        elif notice == "zero_export_site":
            _LOGGER.info(
                "Profit Max solar export is disabled: Maximum grid export "
                "is set to 0 kW, so this site can never export solar"
            )
        self._last_solar_export_limit_notice = notice
        self._sync_solar_export_limit_issue(
            notice == "export_limit_not_configured"
        )

    def _sync_solar_export_limit_issue(self, active: bool) -> None:
        """Raise or clear the repair that names the setting to fill in."""
        try:
            from homeassistant.helpers import issue_registry as ir

            from ..const import DOMAIN

            issue_id = f"solar_export_limit_not_configured_{self.entry_id}"
            if active:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="solar_export_limit_not_configured",
                )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        except Exception as err:  # pragma: no cover - diagnostics only
            _LOGGER.debug("Solar export limit repair sync skipped: %s", err)

    def _profit_max_solar_export_slots(
        self,
        import_prices: list[float],
        export_prices: list[float],
        solar: list[float],
        load: list[float],
        current_soc: float,
        hard_charge_blocks: list[bool],
        grid_charge_allowed: list[bool],
        grid_export_limits_w: list[float | None] | None = None,
    ) -> list[bool]:
        """Select profitable solar deferrals with explicitly funded recharge."""
        n = min(len(import_prices), len(export_prices), len(solar), len(load))
        selected = [False] * n
        if n <= 1 or not self.profit_max_mode:
            reason = (
                "profit_max_disabled"
                if not self.profit_max_mode
                else "insufficient_horizon"
            )
            self._solar_export_capability_status = {
                "supported": False,
                "reason": reason,
                "selected_slots": 0,
                "current_slot": (
                    {"selected": False, "reason": reason} if n else None
                ),
                "rejection_counts": (
                    {reason: max(1, n - 1)} if n else {}
                ),
            }
            # Nothing here is about the export limit, so drop any standing
            # notice rather than leaving a repair up after Profit Max is off.
            self._sync_solar_export_capability_notice({"reason": reason})
            return selected
        capability = self._solar_export_capability()
        if not capability.get("supported"):
            reason = str(capability.get("reason") or "capability_unsupported")
            self._solar_export_capability_status = {
                **capability,
                "selected_slots": 0,
                "current_slot": {"selected": False, "reason": reason},
                "rejection_counts": {reason: n - 1},
            }
            return selected

        interval_hours = max(1, int(self._config.interval_minutes or 5)) / 60.0
        max_charge_kw = max(0.0, float(self._config.max_charge_w or 0.0) / 1000.0)
        capacity_kwh = max(
            0.0, float(self._config.battery_capacity_wh or 0.0) / 1000.0
        )
        efficiency = float(
            getattr(getattr(self, "_optimizer", None), "efficiency", 0.92) or 0.92
        )
        efficiency = max(0.01, min(1.0, efficiency))
        if max_charge_kw <= 0 or capacity_kwh <= 0:
            self._solar_export_capability_status = {
                **capability,
                "supported": False,
                "reason": "battery_limits_unknown",
                "selected_slots": 0,
                "current_slot": {
                    "selected": False,
                    "reason": "battery_limits_unknown",
                },
                "rejection_counts": {"battery_limits_unknown": n - 1},
            }
            return selected

        deadline_slot = None
        optimizer = getattr(self, "_optimizer", None)
        if self.charge_by_time_enabled and optimizer is not None:
            raw_deadline = getattr(optimizer, "pre_window_slot", None)
            if isinstance(raw_deadline, int) and raw_deadline > 0:
                deadline_slot = min(n, raw_deadline)

        # Each future slot contributes a finite amount of stored-energy
        # repayment. Consuming this budget prevents multiple high-FIT slots
        # from claiming the same later recharge opportunity.
        future_capacity_kwh = [0.0] * n
        future_cost_per_input_kwh = [math.inf] * n
        for idx in range(n):
            if idx < len(hard_charge_blocks) and hard_charge_blocks[idx]:
                continue
            solar_surplus_kw = max(0.0, solar[idx] - load[idx])
            solar_charge_kw = min(max_charge_kw, solar_surplus_kw)
            best_charge_kw = solar_charge_kw
            if (
                self._config.allow_grid_charge
                and idx < len(grid_charge_allowed)
                and grid_charge_allowed[idx]
                and (
                    import_prices[idx] <= 0.001
                    or (deadline_slot is not None and idx < deadline_slot)
                )
            ):
                grid_headroom_kw = max_charge_kw
                max_grid_import_w = self._config.max_grid_import_w
                if max_grid_import_w is not None:
                    net_load_kw = max(0.0, load[idx] - solar[idx])
                    grid_headroom_kw = min(
                        max_charge_kw,
                        max(0.0, float(max_grid_import_w) / 1000.0 - net_load_kw),
                    )
                if grid_headroom_kw > best_charge_kw:
                    best_charge_kw = grid_headroom_kw
                    future_cost_per_input_kwh[idx] = import_prices[idx] / efficiency
            if solar_charge_kw > 0:
                future_cost_per_input_kwh[idx] = min(
                    future_cost_per_input_kwh[idx], export_prices[idx]
                )
            future_capacity_kwh[idx] = best_charge_kw * interval_hours * efficiency

        remaining_headroom_kwh = capacity_kwh * max(
            0.0, 1.0 - max(0.0, min(1.0, current_soc))
        )
        margin = 0.001  # Internal 0.1c/kWh anti-churn margin; not user config.
        current_slot_status: dict[str, Any] | None = None
        rejection_counts: dict[str, int] = {}

        def _record_slot(
            idx: int,
            reason: str,
            **details: Any,
        ) -> None:
            nonlocal current_slot_status
            selected_slot = reason == "selected"
            if not selected_slot:
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
            if idx == 0:
                current_slot_status = {
                    "selected": selected_slot,
                    "reason": reason,
                    **details,
                }

        for idx in range(n - 1):
            if remaining_headroom_kwh <= 1e-9:
                for blocked_idx in range(idx, n - 1):
                    _record_slot(
                        blocked_idx,
                        "battery_headroom_exhausted",
                        remaining_headroom_kwh=0.0,
                    )
                break
            if idx < len(hard_charge_blocks) and hard_charge_blocks[idx]:
                _record_slot(idx, "hard_charge_block")
                continue
            if (
                grid_export_limits_w is not None
                and idx < len(grid_export_limits_w)
                and grid_export_limits_w[idx] is not None
                and float(grid_export_limits_w[idx]) <= 100.0
            ):
                _record_slot(
                    idx,
                    "grid_export_limit_blocked",
                    grid_export_limit_w=round(
                        float(grid_export_limits_w[idx]), 3
                    ),
                )
                continue
            surplus_kw = min(max_charge_kw, max(0.0, solar[idx] - load[idx]))
            slot_details = {
                "solar_surplus_kw": round(surplus_kw, 6),
                "export_price_per_kwh": round(float(export_prices[idx]), 6),
                "remaining_headroom_kwh": round(remaining_headroom_kwh, 6),
            }
            if surplus_kw <= 0.1:
                _record_slot(idx, "insufficient_solar_surplus", **slot_details)
                continue
            if export_prices[idx] <= margin:
                _record_slot(idx, "non_positive_export_price", **slot_details)
                continue
            deferred_kwh = min(
                remaining_headroom_kwh,
                surplus_kw * interval_hours * efficiency,
            )
            repayment_end = (
                deadline_slot
                if deadline_slot is not None and idx < deadline_slot
                else n
            )
            candidates = [
                future_idx
                for future_idx in range(idx + 1, repayment_end)
                if future_capacity_kwh[future_idx] > 1e-9
                and future_cost_per_input_kwh[future_idx] + margin
                < export_prices[idx]
            ]
            candidates.sort(key=lambda future_idx: future_cost_per_input_kwh[future_idx])
            available = sum(future_capacity_kwh[future_idx] for future_idx in candidates)
            cheapest_cost = (
                future_cost_per_input_kwh[candidates[0]]
                if candidates
                else None
            )
            funding_details = {
                **slot_details,
                "deferred_kwh": round(deferred_kwh, 6),
                "eligible_replenishment_kwh": round(available, 6),
                "cheapest_replenishment_cost_per_kwh": (
                    round(float(cheapest_cost), 6)
                    if cheapest_cost is not None
                    else None
                ),
            }
            if available + 1e-9 < deferred_kwh:
                _record_slot(
                    idx,
                    "insufficient_cheaper_replenishment",
                    **funding_details,
                )
                continue
            remaining = deferred_kwh
            for future_idx in candidates:
                used = min(remaining, future_capacity_kwh[future_idx])
                future_capacity_kwh[future_idx] -= used
                remaining -= used
                if remaining <= 1e-9:
                    break
            selected[idx] = True
            _record_slot(idx, "selected", **funding_details)
            remaining_headroom_kwh -= deferred_kwh

        self._solar_export_capability_status = {
            **capability,
            "selected_slots": sum(selected),
            "current_slot": current_slot_status,
            "rejection_counts": rejection_counts,
        }
        return selected

    def _revise_solar_export_holds(
        self,
        result: OptimizerResult,
        import_prices: list[float],
        export_prices: list[float],
        solar_export_slots: list[bool],
    ) -> list[bool]:
        """Drop solar-export holds the solved plan repays with costlier grid.

        ``_profit_max_solar_export_slots`` funds each deferral from the
        cheapest future charge capacity it can find, but that capacity is raw
        per-slot headroom: the selector cannot know how much of it the rest of
        the plan already needs. When the plan is charge-capacity constrained
        the same solar is counted twice — once as the charge it was always
        going to do, once as the deferral's replenishment — and the solve makes
        up the difference with priced grid import instead. The hold is a hard
        pre-LP charge block, so the LP cannot reject it. Re-check each hold
        against the grid charging the solve actually planned after it and drop
        the ones that lose the round trip.
        """
        actions = getattr(getattr(result, "schedule", None), "actions", None) or []
        n = min(
            len(solar_export_slots),
            len(import_prices),
            len(export_prices),
            len(actions),
        )
        if n <= 0:
            return list(solar_export_slots)
        efficiency = float(
            getattr(getattr(self, "_optimizer", None), "efficiency", 0.92) or 0.92
        )
        efficiency = max(0.01, min(1.0, efficiency))
        margin = 0.001  # Same anti-churn margin the selector applies.
        # Worst grid-charge cost at or after each slot. Deferred solar can only
        # displace grid charging planned later in the same horizon, and a
        # rational plan would shave its most expensive grid charge first.
        worst_later_charge_cost: list[float | None] = [None] * (n + 1)
        for idx in range(n - 1, -1, -1):
            worst = worst_later_charge_cost[idx + 1]
            action = actions[idx]
            if (
                getattr(action, "action", None) == "charge"
                and float(getattr(action, "battery_charge_w", 0.0) or 0.0) > 0.0
            ):
                cost = float(import_prices[idx]) / efficiency
                worst = cost if worst is None else max(worst, cost)
            worst_later_charge_cost[idx] = worst
        revised = list(solar_export_slots)
        for idx in range(n):
            if not revised[idx]:
                continue
            replacement_cost = worst_later_charge_cost[idx + 1]
            if (
                replacement_cost is not None
                and float(export_prices[idx]) <= replacement_cost + margin
            ):
                revised[idx] = False
        return revised

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
        interval = max(1, int(self._config.interval_minutes or 5))
        raw_now = dt_util.now()
        now = raw_now.replace(
            minute=(raw_now.minute // interval) * interval,
            second=0, microsecond=0,
        )
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

    def _agl_battery_reward_export_slots(
        self,
        n: int,
        export_prices: list[float] | None = None,
    ) -> list[bool]:
        """Return AGL's contractual evening reward slots with a usable rate."""
        if self._provider_key() != "agl" or n <= 0 or not export_prices:
            return [False] * max(0, n)

        from ..const import (
            AGL_BATTERY_REWARDS_END_HOUR,
            AGL_BATTERY_REWARDS_START_HOUR,
        )

        allowed = [False] * n
        for idx, timestamp in enumerate(self._price_timestamps(n)):
            if idx >= len(export_prices):
                break
            try:
                export_price = float(export_prices[idx] or 0.0)
            except (TypeError, ValueError):
                continue
            allowed[idx] = (
                export_price > 0.001
                and AGL_BATTERY_REWARDS_START_HOUR
                <= timestamp.hour
                < AGL_BATTERY_REWARDS_END_HOUR
            )
        return allowed

    def _flow_power_profit_export_slots(self, n: int) -> list[bool]:
        """Allow Flow Power profit exports only during Happy Hour."""
        if not self._config.profit_max_enabled or self._provider_key() != "flow_power":
            return [False] * n
        return self._flow_power_export_window_slots(n)

    def _flow_power_export_window_slots(self, n: int) -> list[bool]:
        """Return Flow Power's configured daily export window slots."""
        if self._provider_key() != "flow_power":
            return [False] * n
        if not self._entry:
            return [False] * n

        runtime = self._ensure_flow_power_ledger(now=dt_util.now())
        if runtime is not None and runtime[0].plan_id in {
            "happy_hour_2026",
            "four_free_2026",
            "flow_home_2026",
        }:
            snapshot, ledger = runtime
            if snapshot.plan_id == "flow_home_2026":
                return [False] * n
            from ..flow_power import flow_power_price_series

            series = flow_power_price_series(
                snapshot,
                self._price_timestamps(n),
                [0.0] * n,
                ledger=ledger,
            )
            return [
                (
                    active_plan == "legacy_unclassified"
                    and base_export > 0
                )
                or (group is not None and bonus > 0)
                for active_plan, base_export, group, bonus in zip(
                    series.active_plan_ids,
                    series.settlement_export,
                    series.export_group_ids,
                    series.export_bonus,
                    strict=False,
                )
            ]

        from ..const import (
            CONF_FLOW_POWER_EXPORT_RATE,
            CONF_FLOW_POWER_HAPPY_HOUR_END,
            CONF_FLOW_POWER_STATE,
            FLOW_POWER_EXPORT_RATES,
            resolve_flow_power_happy_hour_end,
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

        happy_hour_end = resolve_flow_power_happy_hour_end(
            self._entry.options.get(
                CONF_FLOW_POWER_HAPPY_HOUR_END,
                self._entry.data.get(CONF_FLOW_POWER_HAPPY_HOUR_END),
            )
        )
        return self._time_window_slots(n, "17:30", happy_hour_end)

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
        # Cap = max(real_export, cheapest_import / economic round-trip efficiency)
        # This allows discharge of existing/solar charge at boosted prices
        # but prevents grid-charge-then-export from appearing profitable.
        economic_round_trip_efficiency = max(
            1e-9,
            getattr(
                getattr(
                    self,
                    "_last_resolved_optimizer_parameters",
                    ResolvedOptimizerParameters.legacy(),
                ),
                "economic_round_trip_efficiency",
                ResolvedOptimizerParameters.legacy().economic_round_trip_efficiency,
            ),
        )
        arbitrage_cap = None
        if import_prices:
            min_import = min(p for p in import_prices if p > 0.001) if any(p > 0.001 for p in import_prices) else 0
            if min_import > 0:
                arbitrage_cap = min_import / economic_round_trip_efficiency

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
        """Replace export prices with the Flow Power billable base schedule."""
        if not self._entry:
            return export_prices

        from ..const import (
            CONF_ELECTRICITY_PROVIDER,
            CONF_FLOW_POWER_EXPORT_RATE,
            CONF_FLOW_POWER_HAPPY_HOUR_END,
            CONF_FLOW_POWER_STATE,
            FLOW_POWER_EXPORT_RATES,
            resolve_flow_power_happy_hour_end,
        )

        provider = self._entry.options.get(
            CONF_ELECTRICITY_PROVIDER,
            self._entry.data.get(CONF_ELECTRICITY_PROVIDER, ""),
        )
        if provider != "flow_power":
            return export_prices

        runtime = self._ensure_flow_power_ledger(now=dt_util.now())
        if runtime is not None:
            snapshot, ledger = runtime
            from ..flow_power import flow_power_price_series

            series = flow_power_price_series(
                snapshot,
                self._price_timestamps(len(export_prices)),
                [0.0] * len(export_prices),
                ledger=ledger,
            )
            self._last_flow_power_price_series = series
            return list(series.settlement_export)

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
        happy_end_time = resolve_flow_power_happy_hour_end(
            self._entry.options.get(
                CONF_FLOW_POWER_HAPPY_HOUR_END,
                self._entry.data.get(CONF_FLOW_POWER_HAPPY_HOUR_END),
            )
        )
        happy_end_hour, happy_end_minute = map(int, happy_end_time.split(":"))
        happy_end = happy_end_hour * 60 + happy_end_minute
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

    @staticmethod
    def _solar_error_margin_after_nowcast(
        *,
        learned_margin_kwh: float | None,
        raw_solar_forecast: list[float],
        adjusted_solar_forecast: list[float],
        deadline_slot: int | None,
        interval_minutes: int,
    ) -> tuple[float | None, float]:
        """Return residual learned margin and overlapping nowcast allowance."""
        if learned_margin_kwh is None or deadline_slot is None:
            return learned_margin_kwh, 0.0
        deadline_slots = max(
            0,
            min(
                len(raw_solar_forecast),
                len(adjusted_solar_forecast),
                deadline_slot,
            ),
        )
        interval_hours = interval_minutes / 60.0
        nowcast_allowance_kwh = sum(
            max(0.0, raw_solar_forecast[idx] - adjusted_solar_forecast[idx])
            * interval_hours
            for idx in range(deadline_slots)
        )
        # Combined risk is max(nowcast, learned), not their sum. The LP already
        # sees the nowcast-adjusted forecast, so pass only the residual learned
        # allowance into its solar-headroom calculation.
        return (
            max(0.0, learned_margin_kwh - nowcast_allowance_kwh),
            nowcast_allowance_kwh,
        )

    def _observe_solar_forecast_accuracy(
        self,
        solar_forecast: list[float],
        soc: float,
    ) -> None:
        """Feed one raw forecast/live-production pair to the learner.

        The forecast is sampled before nowcast or curtailment adjustments so
        the model never trains on its own output. Invalid observations break
        integration continuity instead of being recorded as zero production.
        """
        learner = getattr(self, "_solar_forecast_learner", None)
        forecaster = getattr(self, "_solar_forecaster", None)
        source = getattr(forecaster, "last_forecast_source", None)
        if learner is None or not source or not solar_forecast:
            return

        data = self._get_energy_data() or {}
        reason: str | None = None
        if data.get("telemetry_ready") is False:
            reason = "stale_telemetry"
        elif data.get("solar_power_valid") is False:
            reason = "invalid_telemetry"
        elif soc >= 0.98:
            reason = "near_full_soc"
        elif getattr(self, "_last_executed_action", None) == "off_grid":
            reason = "off_grid"

        entry_data = getattr(self, "hass", None)
        entry_data = getattr(entry_data, "data", {}) if entry_data else {}
        if isinstance(entry_data, dict):
            runtime = entry_data.get("power_sync", {}).get(
                getattr(self, "entry_id", ""), {}
            )
            if isinstance(runtime, dict) and any(
                key.endswith("_curtailment_state") and value == "curtailed"
                for key, value in runtime.items()
            ):
                reason = "curtailment"

        try:
            actual_kw = float(data.get("solar_power"))
        except (TypeError, ValueError):
            actual_kw = None
            reason = reason or "invalid_telemetry"
        if actual_kw is not None and (not math.isfinite(actual_kw) or actual_kw < 0):
            actual_kw = None
            reason = reason or "invalid_telemetry"

        try:
            forecast_now_kw = max(0.0, float(solar_forecast[0]))
        except (TypeError, ValueError, IndexError):
            return
        if not math.isfinite(forecast_now_kw):
            return
        observation_time = dt_util.now()
        interval_seconds = max(60, int(self._config.interval_minutes * 60))
        aligned_epoch = (
            int(observation_time.timestamp()) // interval_seconds
        ) * interval_seconds
        observation_time = datetime.fromtimestamp(
            aligned_epoch,
            tz=observation_time.tzinfo,
        )
        changed = learner.observe(
            timestamp=observation_time,
            source=source,
            forecast_kw=forecast_now_kw,
            actual_kw=actual_kw,
            valid=reason is None,
            skip_reason=reason,
        )
        if changed:
            self._schedule_solar_forecast_learning_save()

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
        if data.get("solar_power_valid") is False:
            _LOGGER.debug(
                "Solar forecast nowcast: ignoring unavailable live solar telemetry"
            )
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
            # Dawn/dusk and very low production are too noisy to learn from,
            # but a derate learned before sunset must still recover overnight
            # instead of freezing and suppressing next-morning forecasts.
            self._solar_nowcast_derate = min(1.0, self._solar_nowcast_derate + 0.08)
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
        else:
            # A prior transient can leave the derate below the level supported
            # by current production. Recover through the 75-90% deadband
            # instead of freezing the stale factor indefinitely. Do not learn
            # a new derate here: only raise an already-lower factor toward the
            # live ratio plus the same 10% forecast buffer used above.
            recovery_target = min(1.0, ratio + 0.10)
            if self._solar_nowcast_derate < recovery_target:
                self._solar_nowcast_derate = min(
                    recovery_target,
                    self._solar_nowcast_derate + 0.08,
                )

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

        # Coordinators that receive native market products can preserve the
        # provider's exact boundary instead of reconstructing it from a
        # rounded duration.
        explicit_start = e.get("startTime") or e.get("startsAt")
        if explicit_start:
            return explicit_start

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
        explicit_end = e.get("endTime") or e.get("endsAt")
        if explicit_end:
            return explicit_end
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
            # Amber can omit advancedPrice after the active interval is
            # finalised.  Its perKwh is still the customer's settled retail
            # price; dropping it would let the gap filler assign a later
            # forecast to the current slot.
            if interval_type == "CurrentInterval":
                price_cents = entry.get("perKwh")
                if price_cents is None:
                    return None
            else:
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
            # See _dynamic_import_price_dollar: CurrentInterval.perKwh is the
            # valid settled feed-in retail price when advancedPrice is absent.
            if interval_type == "CurrentInterval":
                price_cents = entry.get("perKwh")
                if price_cents is None:
                    return None
            else:
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
        if self._electricity_provider() == "covau":
            return self._covau_price_forecast()

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

                        self._last_settlement_import_prices = list(import_prices)
                        self._last_settlement_export_prices = list(export_prices)

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

                        # Keep the successfully built price values coupled to
                        # their original interval grid. Cached actions can execute
                        # after the wall clock crosses a slot boundary; synthesizing
                        # a fresh grid then would shift every cached price one
                        # position. Stage a fresh grid every run so a successful
                        # provider switch cannot retain static-TOU metadata.
                        self._pending_price_timestamps = self._interval_timestamps(
                            current_window,
                            n_steps,
                            interval,
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

        timestamps = self._interval_timestamps(now, n_steps, interval)
        for t, ts in enumerate(timestamps):
            slot_tou_periods, slot_buy_rates, slot_sell_rates, _ = (
                tariff_components_for_datetime(tariff, ts)
            )
            matched_period = find_matching_tou_period(
                slot_tou_periods,
                ts,
                default="OFF_PEAK",
                buy_rates=slot_buy_rates,
                sell_rates=slot_sell_rates,
            )

            # buy_rates values are in $/kWh (e.g. 0.48 for 48c)
            # When the matched period isn't in buy_rates (e.g. GloBird gaps at 14-17, 21-24),
            # try common fallback period names, then use the median of available rates.
            buy = slot_buy_rates.get(matched_period)
            if buy is None:
                for fallback in ("OFF_PEAK", "PARTIAL_PEAK", "SHOULDER"):
                    if fallback in slot_buy_rates:
                        buy = slot_buy_rates[fallback]
                        break
                if buy is None:
                    # Use median of defined rates (better than arbitrary hardcoded default)
                    defined = sorted(v for v in slot_buy_rates.values() if isinstance(v, (int, float)))
                    buy = defined[len(defined) // 2] if defined else 0.30

            sell = slot_sell_rates.get(matched_period)
            if sell is None:
                # Global FiT (ALL key) is the correct fallback for unmatched periods
                sell = slot_sell_rates.get("ALL")
            if sell is None:
                for fallback in ("OFF_PEAK", "PARTIAL_PEAK", "SHOULDER"):
                    if fallback in slot_sell_rates:
                        sell = slot_sell_rates[fallback]
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
                ts = timestamps[t_idx]
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
        self._last_settlement_import_prices = list(display_import)
        self._last_settlement_export_prices = list(display_export)
        self._last_grid_charge_cap_import_prices = list(import_prices)
        self._pending_price_timestamps = timestamps

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
                "title": "Solar Forecast Unavailable",
                "message": "No usable current solar forecast data is available. The optimizer is making decisions based on price only, without knowing when solar will be available. Check that the selected Solcast, Open-Meteo, or Volcast provider is loaded and exposing current forecast periods.",
            })
        return warnings

    def _record_solar_forecast_availability(
        self, solar_forecast: list[float] | None
    ) -> None:
        """Record whether the latest forecast came from a supported provider."""
        source = getattr(
            getattr(self, "_solar_forecaster", None),
            "last_forecast_source",
            None,
        )
        self._has_solar_forecast = solar_forecast is not None and source is not None

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

        PowerSync Home Load declares ``home_load_basis=excludes_ev`` and must
        never be adjusted again. An external recorder-backed gross load sensor
        can still opt into per-entity history subtraction. Returning an empty
        list leaves an already-normalized or unconfigured history unchanged.
        """
        if not getattr(self, "_ev_integration_enabled", False):
            return []
        load_entity_id = getattr(self._load_estimator, "load_entity_id", None)
        load_state = self.hass.states.get(load_entity_id) if load_entity_id else None
        if (
            load_state
            and (load_state.attributes or {}).get("home_load_basis") == "excludes_ev"
        ):
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

    def _get_ev_planned_load_components(
        self,
        n_intervals: int,
    ) -> dict[str, list[float]]:
        """Get timestamp-aligned Smart Schedule load by physical loadpoint.

        Reads the selected charging windows from each vehicle's current plan
        and returns a per-interval power array in Watts matching the load
        forecast resolution.

        Args:
            n_intervals: Number of intervals in the load forecast.

        Returns:
            Mapping of loadpoint id to Watts per interval.
        """
        from ..automations.ev_charging_planner import (
            get_auto_schedule_executor,
            is_smart_schedule_grid_price_allowed,
        )

        executor = get_auto_schedule_executor()
        if not executor:
            return {}

        # Access vehicle states directly for typed AutoScheduleState objects
        states = getattr(executor, "_state", {})
        if not states:
            return {}

        now = dt_util.now()
        interval_minutes = self._config.interval_minutes
        timestamps = self._price_timestamps(n_intervals)
        ev_load_by_vehicle: dict[str, list[float]] = {}

        for vehicle_id, state in states.items():
            configured_power_w = None
            try:
                settings = getattr(executor, "_settings", {}).get(vehicle_id)
                if settings is None or not bool(getattr(settings, "enabled", False)):
                    continue
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
                # Fail closed: a plan with missing or invalid settings must not
                # become household demand in the battery optimiser.
                continue

            plan = state.current_plan
            if not plan or not plan.windows:
                continue

            vehicle_load = ev_load_by_vehicle.setdefault(
                str(vehicle_id), [0.0] * n_intervals
            )

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

                # Map against the optimizer's absolute slot boundaries. This
                # preserves repeated/missing local DST hours and provider grids.
                for i, interval_start in enumerate(timestamps):
                    interval_end = (
                        timestamps[i + 1]
                        if i + 1 < len(timestamps)
                        else interval_start
                        + timedelta(minutes=interval_minutes)
                    )
                    local_weekday = interval_start.astimezone(
                        dt_util.DEFAULT_TIME_ZONE
                    ).weekday()
                    priority_getter = getattr(
                        settings,
                        "get_effective_priority",
                        None,
                    )
                    slot_priority = (
                        priority_getter(local_weekday)
                        if callable(priority_getter)
                        else getattr(
                            settings,
                            "priority",
                            getattr(plan, "priority", None),
                        )
                    )
                    max_price_getter = getattr(
                        settings,
                        "get_effective_max_grid_price",
                        None,
                    )
                    slot_max_grid_price_cents = (
                        max_price_getter(local_weekday)
                        if callable(max_price_getter)
                        else getattr(
                            settings,
                            "max_grid_price_cents",
                            getattr(plan, "max_grid_price_cents", None),
                        )
                    )
                    if not is_smart_schedule_grid_price_allowed(
                        source=getattr(window, "source", ""),
                        price_cents=getattr(window, "price_cents_kwh", None),
                        max_grid_price_cents=slot_max_grid_price_cents,
                        priority=slot_priority,
                    ):
                        continue
                    planner = getattr(executor, "planner", None)
                    demand_blocked = getattr(
                        planner, "_is_grid_charging_blocked_at", None
                    )
                    if (
                        str(getattr(window, "source", "")).startswith("grid")
                        and callable(demand_blocked)
                        and demand_blocked(interval_start)
                    ):
                        continue
                    overlap_start = max(w_start, interval_start)
                    overlap_end = min(w_end, interval_end)
                    overlap_seconds = (
                        overlap_end - overlap_start
                    ).total_seconds()
                    if overlap_seconds <= 0:
                        continue
                    slot_seconds = (interval_end - interval_start).total_seconds()
                    if slot_seconds <= 0:
                        continue
                    overlap_fraction = overlap_seconds / slot_seconds
                    vehicle_load[i] += power_w * overlap_fraction

        ev_load_by_vehicle = {
            vehicle_id: values
            for vehicle_id, values in ev_load_by_vehicle.items()
            if any(value > 0 for value in values)
        }
        if not ev_load_by_vehicle:
            return {}

        # Log summary
        ev_load = [
            sum(values[index] for values in ev_load_by_vehicle.values())
            for index in range(n_intervals)
        ]
        peak_kw = max(ev_load) / 1000
        dt_h = interval_minutes / 60
        total_kwh = sum(ev_load) / 1000 * dt_h
        active_intervals = sum(1 for v in ev_load if v > 0)
        _LOGGER.debug(
            "EV load overlay: %d intervals, peak %.1f kW, total %.1f kWh",
            active_intervals, peak_kw, total_kwh,
        )

        return ev_load_by_vehicle

    def _get_ev_planned_load(self, n_intervals: int) -> list[float] | None:
        """Return aggregate Smart Schedule load for compatibility callers."""
        components = self._get_ev_planned_load_components(n_intervals)
        if not components:
            return None
        return [
            sum(values[index] for values in components.values())
            for index in range(n_intervals)
        ]

    def _canonical_ev_loadpoint_ids(
        self,
        identifiers: list[str],
    ) -> dict[str, str]:
        """Resolve Fleet/BLE aliases onto one electrical loadpoint key."""
        from ..tesla_ble_mapping import canonical_tesla_vehicle_id

        opts = {
            **getattr(self._entry, "data", {}),
            **getattr(self._entry, "options", {}),
        }
        fleet_vins = [
            identifier
            for identifier in identifiers
            if len(identifier) == 17
            and identifier.isalnum()
            and not identifier.isdigit()
        ]
        return {
            identifier: canonical_tesla_vehicle_id(
                opts,
                identifier,
                fleet_vins,
            )
            or identifier
            for identifier in identifiers
        }

    def _merge_internal_ev_load_components(
        self,
        *,
        n_intervals: int,
        smart_components: dict[str, list[float]],
        price_components: dict[str, tuple[float, ...]],
    ) -> tuple[list[float], list[float], list[float]]:
        """Union same-loadpoint plans and sum genuinely separate chargers."""
        identifiers = [*smart_components, *price_components]
        canonical = self._canonical_ev_loadpoint_ids(identifiers)
        smart_by_loadpoint: dict[str, list[float]] = {}
        price_by_loadpoint: dict[str, list[float]] = {}

        for source, destination in (
            (smart_components, smart_by_loadpoint),
            (price_components, price_by_loadpoint),
        ):
            for identifier, values in source.items():
                loadpoint = canonical.get(identifier, identifier)
                target = destination.setdefault(loadpoint, [0.0] * n_intervals)
                for index in range(min(n_intervals, len(values))):
                    try:
                        target[index] = max(target[index], float(values[index] or 0.0))
                    except (TypeError, ValueError):
                        continue

        loadpoints = set(smart_by_loadpoint) | set(price_by_loadpoint)
        effective = [0.0] * n_intervals
        smart_effective = [0.0] * n_intervals
        price_marginal = [0.0] * n_intervals
        for loadpoint in loadpoints:
            smart = smart_by_loadpoint.get(loadpoint, [0.0] * n_intervals)
            price = price_by_loadpoint.get(loadpoint, [0.0] * n_intervals)
            for index in range(n_intervals):
                smart_value = smart[index]
                price_value = price[index]
                selected = max(smart_value, price_value)
                effective[index] += selected
                smart_effective[index] += smart_value
                price_marginal[index] += max(0.0, selected - smart_value)
        return effective, smart_effective, price_marginal

    async def _build_price_level_projection(
        self,
        n_intervals: int,
    ) -> PriceLevelProjection:
        """Build a conservative projection from live facts and raw price slots."""
        from ..automations.ev_charging_planner import get_price_level_executor

        executor = get_price_level_executor()
        if executor is None or n_intervals <= 0:
            return PriceLevelProjection.empty(max(0, n_intervals))
        settings = executor._get_settings()
        display_prices = list(
            getattr(self, "_last_display_import_prices", None) or []
        )
        timestamps = self._price_timestamps(n_intervals)
        prices_cents: list[float | None] = []
        valid_slots: list[bool] = []
        for index in range(n_intervals):
            if index >= len(display_prices):
                prices_cents.append(None)
                valid_slots.append(False)
                continue
            try:
                value = float(display_prices[index]) * 100.0
            except (TypeError, ValueError):
                value = math.nan
            prices_cents.append(value if math.isfinite(value) else None)
            valid_slots.append(math.isfinite(value))

        try:
            snapshots = await executor.collect_projection_snapshots()
            demand_blocked = [
                executor.is_grid_charging_blocked_at(timestamp)
                for timestamp in timestamps
            ]
            return build_price_level_projection(
                timestamps=timestamps,
                prices_cents=prices_cents,
                vehicles=snapshots,
                enabled=bool(settings.get("enabled", False)),
                recovery_soc=float(settings.get("recovery_soc", 40)),
                recovery_price_cents=float(
                    settings.get("recovery_price_cents", 30)
                ),
                opportunity_price_cents=float(
                    settings.get("opportunity_price_cents", 10)
                ),
                home_battery_minimum=float(
                    settings.get("home_battery_minimum", 20)
                ),
                preserve_home_battery=bool(
                    settings.get("preserve_home_battery", False)
                ),
                no_grid_import=bool(settings.get("no_grid_import", False)),
                demand_blocked=demand_blocked,
                valid_price_slots=valid_slots,
                interval_minutes=self._config.interval_minutes,
            )
        except Exception as err:
            _LOGGER.warning(
                "Price-Level projection unavailable; keeping the existing EV plan: %s",
                err,
            )
            return PriceLevelProjection.empty(
                n_intervals,
                "Projection unavailable; live Price-Level charging is unchanged",
            )

    def _price_level_projection_payload(
        self,
        *,
        projection: PriceLevelProjection,
        effective_source: str,
        external_w: list[float],
        smart_w: list[float],
        price_expected_w: list[float],
    ) -> dict[str, Any]:
        timezone_name = getattr(getattr(self, "hass", None), "config", None)
        timezone_name = getattr(timezone_name, "time_zone", None) or str(
            dt_util.now().tzinfo
        )
        return {
            "schema_version": 1,
            "generated_at": dt_util.now().isoformat(),
            "timezone": timezone_name,
            "interval_minutes": self._config.interval_minutes,
            "effective_source": effective_source,
            "components": {
                "external_w": list(external_w),
                "smart_schedule_w": list(smart_w),
                "price_level_expected_w": list(price_expected_w),
                "price_level_conditional_cap_w": list(
                    projection.conditional_cap_w
                ),
            },
            "windows": [window.to_dict() for window in projection.windows],
            "warnings": list(projection.warnings),
        }

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
            if type(self.energy_coordinator).__name__ in {
                "FoxESSEnergyCoordinator",
                "FoxESSEntityEnergyCoordinator",
            }:
                foxess_limits = _foxess_auto_power_limits(data)
                if foxess_limits is not None:
                    charge_w, discharge_w = foxess_limits
                    self._config.max_charge_w = charge_w
                    self._config.max_discharge_w = discharge_w
                    self._battery_specs_source = "auto"

                    _LOGGER.info(
                        "Auto-detected battery power from live FoxESS telemetry: "
                        "charge %.1f kW, discharge %.1f kW",
                        charge_w / 1000,
                        discharge_w / 1000,
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

    async def _restore_solar_forecast_learning(self) -> None:
        """Restore provider-specific solar forecast calibration state."""
        try:
            data = await self._solar_forecast_learning_store.async_load()
        except Exception as exc:
            _LOGGER.warning("Failed to load solar forecast learning data: %s", exc)
            return
        self._solar_forecast_learner = SolarForecastLearner.from_dict(data)
        if data:
            diagnostics = self._solar_forecast_learner.diagnostics(
                getattr(self._solar_forecaster, "last_forecast_source", None)
            )
            _LOGGER.info(
                "Restored solar forecast learning: %d provider(s), %d valid day(s)",
                len(self._solar_forecast_learner.histories),
                sum(
                    len(history)
                    for history in self._solar_forecast_learner.histories.values()
                ),
            )
            _LOGGER.debug("Solar forecast learning state: %s", diagnostics)

    async def _restore_battery_efficiency_learning(self) -> None:
        """Restore provider-neutral closed-cycle battery efficiency evidence."""
        try:
            data = await self._battery_efficiency_learning_store.async_load()
        except Exception as exc:
            _LOGGER.warning("Failed to load battery efficiency learning data: %s", exc)
            return
        self._battery_efficiency_learner = BatteryEfficiencyLearner.from_dict(data)
        self._last_resolved_optimizer_parameters = (
            self._battery_efficiency_learner.resolved_parameters(
                application_enabled=self.battery_efficiency_learning_enabled,
                now=dt_util.now(),
            )
        )
        if data:
            diagnostics = self._battery_efficiency_learner.diagnostics(
                application_enabled=self.battery_efficiency_learning_enabled,
                now=dt_util.now(),
            )
            _LOGGER.info(
                "Restored battery efficiency learning: %d valid cycle(s), "
                "%d day(s), %.2f equivalent cycle(s)",
                diagnostics["valid_cycles"],
                diagnostics["distinct_days"],
                diagnostics["equivalent_full_cycles"],
            )
            _LOGGER.debug("Battery efficiency learning state: %s", diagnostics)

    def _schedule_battery_efficiency_learning_save(self) -> None:
        """Schedule a coalesced write of accepted/rejected learner state."""
        store = getattr(self, "_battery_efficiency_learning_store", None)
        learner = getattr(self, "_battery_efficiency_learner", None)
        if store is None or learner is None:
            return
        store.async_delay_save(
            learner.to_dict,
            BATTERY_EFFICIENCY_LEARNING_STORE_SAVE_DELAY,
        )

    def _battery_efficiency_topology_fingerprint(self, capacity_wh: float) -> str:
        """Return a stable aggregate AC measurement-boundary identifier."""
        try:
            normalized_capacity = int(round(float(capacity_wh)))
        except (TypeError, ValueError):
            normalized_capacity = 0
        return (
            f"{self.battery_system}|integrated_ac_power|ac_system|"
            f"{normalized_capacity}"
        )

    def _observe_battery_efficiency(
        self,
        *,
        timestamp: datetime,
        soc: float,
        capacity_wh: float,
    ) -> None:
        """Feed one normalized telemetry sample to the closed-cycle learner."""
        data = self._get_energy_data()
        if not isinstance(data, dict):
            return
        result = self._battery_efficiency_learner.observe(
            timestamp=timestamp,
            soc=soc,
            battery_power_kw=data.get("battery_power"),
            capacity_kwh=float(capacity_wh) / 1000.0,
            topology_fingerprint=(
                self._battery_efficiency_topology_fingerprint(capacity_wh)
            ),
            valid=data.get("telemetry_ready") is not False,
            skip_reason="stale_telemetry",
        )
        if result.changed:
            self._schedule_battery_efficiency_learning_save()
        if result.accepted_cycle:
            diagnostics = self._battery_efficiency_learner.diagnostics(
                application_enabled=self.battery_efficiency_learning_enabled,
                now=timestamp,
            )
            _LOGGER.info(
                "Battery efficiency learner accepted cycle: candidate=%.1f%%, "
                "confidence=%.0f%%, applied one-way=%.1f%%",
                (diagnostics.get("candidate_round_trip_efficiency") or 0.0) * 100,
                diagnostics["confidence"] * 100,
                diagnostics["applied_one_way_efficiency"] * 100,
            )

    def _schedule_solar_forecast_learning_save(self) -> None:
        """Schedule a coalesced write of forecast calibration state."""
        store = getattr(self, "_solar_forecast_learning_store", None)
        learner = getattr(self, "_solar_forecast_learner", None)
        if store is None or learner is None:
            return
        store.async_delay_save(
            learner.to_dict,
            SOLAR_FORECAST_LEARNING_STORE_SAVE_DELAY,
        )

    async def _restore_cost_data(self) -> None:
        """Restore daily cost accumulators from persistent storage."""
        # Initialize the provider ledger even on a first run. Its tariff-day
        # rollover is independent of HA's local daily-cost date.
        if self._provider_key() == "covau":
            self._ensure_covau_ledger(now=dt_util.now())
        elif self._provider_key() == "flow_power":
            self._ensure_flow_power_ledger(now=dt_util.now())
        else:
            self._ensure_custom_tariff_quota_ledger(now=dt_util.now())
        try:
            data = await self._cost_store.async_load()
        except Exception as e:
            _LOGGER.warning("Failed to load persisted cost data: %s", e)
            return

        if not data:
            _LOGGER.debug("No persisted cost data found (first run)")
            return

        quota_state = data.get("quota_state_v2")
        flow_runtime = self._ensure_flow_power_ledger(now=dt_util.now())
        if (
            flow_runtime is not None
            and flow_runtime[1] is not None
            and isinstance(quota_state, dict)
            and quota_state.get("provider") == "flow_power"
            and quota_state.get("plan_content_hash") == flow_runtime[0].plan_hash
        ):
            self._ensure_flow_power_ledger(
                QuotaLedgerState.from_dict(quota_state),
                now=dt_util.now(),
            )
        snapshot = self._covau_snapshot()
        if (
            snapshot is not None
            and isinstance(quota_state, dict)
            and quota_state.get("provider", "covau") == "covau"
            and quota_state.get("plan_content_hash", snapshot.content_hash)
            == snapshot.content_hash
        ):
            self._ensure_covau_ledger(
                QuotaLedgerState.from_dict(quota_state),
                now=dt_util.now(),
            )
            _LOGGER.info(
                "Restored CovaU quota ledger: tariff_day=%s confidence=%s import=%.3fkWh export=%.3fkWh",
                self._covau_ledger.state.tariff_day,
                self._covau_ledger.state.confidence,
                self._covau_ledger.state.settled_kwh.get(COVAU_IMPORT_RULE_ID, 0.0),
                self._covau_ledger.state.settled_kwh.get(COVAU_EXPORT_RULE_ID, 0.0),
            )
        custom_runtime = self._ensure_custom_tariff_quota_ledger(
            now=dt_util.now()
        )
        if (
            custom_runtime is not None
            and isinstance(quota_state, dict)
            and quota_state.get("provider") == "custom_tariff"
            and quota_state.get("plan_content_hash") == custom_runtime[3]
        ):
            custom_runtime = self._ensure_custom_tariff_quota_ledger(
                QuotaLedgerState.from_dict(quota_state),
                now=dt_util.now(),
            )
            if custom_runtime is not None:
                _tariff, _rule, ledger, _content_hash = custom_runtime
                _LOGGER.info(
                    "Restored custom tariff import quota: tariff_day=%s "
                    "confidence=%s import=%.3fkWh",
                    ledger.state.tariff_day,
                    ledger.state.confidence,
                    ledger.state.settled_kwh.get(
                        CUSTOM_TARIFF_IMPORT_RULE_ID,
                        0.0,
                    ),
                )

        restored_globird_quota_state = None
        if (
            self._provider_key() == "globird"
            and isinstance(quota_state, dict)
            and quota_state.get("provider") == "globird"
        ):
            restored_globird_quota_state = QuotaLedgerState.from_dict(quota_state)

        stored_date = data.get("date")
        now = dt_util.now()
        today = now.strftime("%Y-%m-%d")
        current_zerocharge_period = self._zerocharge_period_key(now)
        zerohero = data.get("zerohero", {}) or {}
        if not isinstance(zerohero, dict):
            zerohero = {}
        stored_zerocharge_period = zerohero.get("zerocharge_period_key")
        stored_baseline_zerocharge_period = zerohero.get(
            "baseline_zerocharge_period_key"
        )

        def _restored_zerocharge_value(*keys: str) -> float:
            """Return the first valid non-negative persisted month value."""
            for key in keys:
                if key not in zerohero:
                    continue
                try:
                    value = float(zerohero[key])
                except (TypeError, ValueError):
                    continue
                if math.isfinite(value) and value >= 0.0:
                    return value
            return 0.0

        # ZeroCharge month-to-date state is independent from daily cost state.
        # Restore an explicit period even when the daily cost date is yesterday
        # (for example, after a restart just after local midnight).  Legacy
        # daily-only payloads are adopted conservatively only for today's date.
        if stored_zerocharge_period == current_zerocharge_period:
            self._set_zerocharge_period_state(
                period_key=current_zerocharge_period,
                import_kwh=_restored_zerocharge_value(
                    "zerocharge_import_kwh_month",
                    "zerocharge_import_kwh",
                ),
                credit_value=_restored_zerocharge_value(
                    "zerocharge_credit_value_month",
                    "zerocharge_credit_value",
                ),
            )
        elif stored_date == today and not stored_zerocharge_period:
            self._set_zerocharge_period_state(
                period_key=current_zerocharge_period,
                import_kwh=_restored_zerocharge_value("zerocharge_import_kwh"),
                credit_value=_restored_zerocharge_value("zerocharge_credit_value"),
            )
        else:
            self._set_zerocharge_period_state(
                period_key=current_zerocharge_period,
                import_kwh=0.0,
                credit_value=0.0,
            )

        if stored_baseline_zerocharge_period == current_zerocharge_period:
            self._set_zerocharge_period_state(
                period_key=current_zerocharge_period,
                import_kwh=_restored_zerocharge_value(
                    "baseline_zerocharge_import_kwh_month",
                    "baseline_zerocharge_import_kwh",
                ),
                credit_value=_restored_zerocharge_value(
                    "baseline_zerocharge_credit_value_month",
                    "baseline_zerocharge_credit_value",
                ),
                baseline=True,
            )
        elif stored_date == today and not stored_baseline_zerocharge_period:
            self._set_zerocharge_period_state(
                period_key=current_zerocharge_period,
                import_kwh=_restored_zerocharge_value(
                    "baseline_zerocharge_import_kwh"
                ),
                credit_value=_restored_zerocharge_value(
                    "baseline_zerocharge_credit_value"
                ),
                baseline=True,
            )
        else:
            self._set_zerocharge_period_state(
                period_key=current_zerocharge_period,
                import_kwh=0.0,
                credit_value=0.0,
                baseline=True,
            )

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
            has_grid_charge_provenance = (
                "grid_charge_kwh" in data and "grid_charge_cost" in data
            )
            self._grid_charge_tracking_known = bool(
                data.get(
                    "grid_charge_tracking_known",
                    has_grid_charge_provenance,
                )
            )
            self._actual_zerohero_import_kwh_today = float(zerohero.get("import_window_kwh", 0.0))
            self._actual_zerohero_export_kwh_today = float(zerohero.get("export_window_kwh", 0.0))
            self._actual_zerohero_bonus_export_kwh_today = float(zerohero.get("bonus_export_kwh", 0.0))
            self._actual_zerohero_base_export_earnings_today = float(zerohero.get("base_export_earnings", 0.0))
            self._actual_zerohero_bonus_export_earnings_today = float(zerohero.get("bonus_export_earnings", 0.0))
            self._actual_zerohero_credit_value_today = float(zerohero.get("credit_value", 0.0))
            self._baseline_zerohero_import_kwh_today = float(zerohero.get("baseline_import_window_kwh", 0.0))
            self._baseline_zerohero_bonus_export_kwh_today = float(zerohero.get("baseline_bonus_export_kwh", 0.0))
            self._baseline_zerohero_credit_value_today = float(zerohero.get("baseline_credit_value", 0.0))
            if self._provider_key() == "globird":
                self._globird_quota_state = (
                    restored_globird_quota_state
                    or QuotaLedgerState(
                        tariff_day=stored_date,
                        timezone_token="HA_LOCAL",
                        confidence="estimated",
                    )
                )
                import_legacy_settled_state(
                    self._globird_quota_state,
                    zerohero,
                    {
                        GLOBIRD_QUOTA_EXPORT_RULE_ID: "bonus_export_kwh",
                        GLOBIRD_QUOTA_IMPORT_RULE_ID: "zerocharge_import_kwh",
                    },
                )
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

    def get_daily_cost_coverage_reference(self) -> dict[str, Any]:
        """Return independently persisted daily totals for coverage recovery."""
        return {
            "date": self._last_cost_date,
            "import_kwh": self._actual_import_kwh_today,
            "export_kwh": self._actual_export_kwh_today,
            "import_cost": self._actual_import_cost_today,
            "export_earnings": self._actual_export_earnings_today,
        }

    def _cost_data_to_save(self) -> dict:
        """Return cost data dict for Store serialization."""
        data = {
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
            "grid_charge_tracking_known": bool(
                self._grid_charge_tracking_known
            ),
            "zerohero": {
                "import_window_kwh": round(self._actual_zerohero_import_kwh_today, 4),
                "export_window_kwh": round(self._actual_zerohero_export_kwh_today, 4),
                "bonus_export_kwh": round(self._actual_zerohero_bonus_export_kwh_today, 4),
                "base_export_earnings": round(self._actual_zerohero_base_export_earnings_today, 4),
                "bonus_export_earnings": round(self._actual_zerohero_bonus_export_earnings_today, 4),
                "credit_value": round(self._actual_zerohero_credit_value_today, 4),
                "zerocharge_import_kwh": round(self._actual_zerocharge_import_kwh_today, 4),
                "zerocharge_credit_value": round(self._actual_zerocharge_credit_value_today, 4),
                "zerocharge_period_key": getattr(
                    self, "_actual_zerocharge_period_key", None
                ),
                "zerocharge_import_kwh_month": round(
                    getattr(
                        self,
                        "_actual_zerocharge_import_kwh_month",
                        self._actual_zerocharge_import_kwh_today,
                    ),
                    4,
                ),
                "zerocharge_credit_value_month": round(
                    getattr(
                        self,
                        "_actual_zerocharge_credit_value_month",
                        self._actual_zerocharge_credit_value_today,
                    ),
                    4,
                ),
                "baseline_import_window_kwh": round(self._baseline_zerohero_import_kwh_today, 4),
                "baseline_bonus_export_kwh": round(self._baseline_zerohero_bonus_export_kwh_today, 4),
                "baseline_credit_value": round(self._baseline_zerohero_credit_value_today, 4),
                "baseline_zerocharge_import_kwh": round(self._baseline_zerocharge_import_kwh_today, 4),
                "baseline_zerocharge_credit_value": round(self._baseline_zerocharge_credit_value_today, 4),
                "baseline_zerocharge_period_key": getattr(
                    self, "_baseline_zerocharge_period_key", None
                ),
                "baseline_zerocharge_import_kwh_month": round(
                    getattr(
                        self,
                        "_baseline_zerocharge_import_kwh_month",
                        self._baseline_zerocharge_import_kwh_today,
                    ),
                    4,
                ),
                "baseline_zerocharge_credit_value_month": round(
                    getattr(
                        self,
                        "_baseline_zerocharge_credit_value_month",
                        self._baseline_zerocharge_credit_value_today,
                    ),
                    4,
                ),
            },
        }
        quota_state = self._quota_state_v2_to_save()
        if quota_state is not None:
            data["quota_state_v2"] = quota_state
        return data

    def _quota_state_v2_to_save(self) -> dict[str, Any] | None:
        """Dual-write provider-neutral quota state beside legacy counters."""
        if self._provider_key() == "flow_power":
            runtime = self._ensure_flow_power_ledger(now=dt_util.now())
            if runtime is None or runtime[1] is None:
                return None
            snapshot, ledger = runtime
            payload = ledger.state.to_dict()
            payload.update(
                {
                    "provider": "flow_power",
                    "plan_id": snapshot.plan_id,
                    "plan_content_hash": snapshot.plan_hash,
                }
            )
            return payload
        if self._provider_key() == "covau":
            runtime = self._ensure_covau_ledger(now=dt_util.now())
            if runtime is None:
                return None
            snapshot, ledger = runtime
            payload = ledger.state.to_dict()
            payload.update(
                {
                    "provider": "covau",
                    "plan_id": snapshot.plan_id,
                    "plan_content_hash": snapshot.content_hash,
                }
            )
            return payload

        custom_runtime = self._ensure_custom_tariff_quota_ledger(
            now=dt_util.now()
        )
        if custom_runtime is not None:
            tariff, _rule, ledger, content_hash = custom_runtime
            payload = ledger.state.to_dict()
            payload.update(
                {
                    "provider": "custom_tariff",
                    "plan_id": tariff.get("template_id") or tariff.get("name"),
                    "plan_content_hash": content_hash,
                }
            )
            return payload

        # GloBird keeps its established public status/cost fields and restore
        # path. This parallel v2 payload lets future migrations consume the
        # provider-neutral counters without breaking existing installations.
        if self._zerohero_config() is not None:
            state = getattr(self, "_globird_quota_state", None) or QuotaLedgerState()
            state.tariff_day = getattr(self, "_last_cost_date", None)
            state.timezone_token = "HA_LOCAL"
            state.confidence = "estimated"
            state.reason = "legacy_zerohero_dual_write"
            state.settled_kwh[GLOBIRD_QUOTA_EXPORT_RULE_ID] = max(
                0.0,
                float(
                    getattr(
                        self,
                        "_actual_zerohero_bonus_export_kwh_today",
                        0.0,
                    )
                ),
            )
            state.settled_kwh[GLOBIRD_QUOTA_IMPORT_RULE_ID] = max(
                0.0,
                float(
                    getattr(
                        self,
                        "_actual_zerocharge_import_kwh_today",
                        0.0,
                    )
                ),
            )
            self._globird_quota_state = state
            payload = state.to_dict()
            payload["provider"] = "globird"
            return payload
        return None

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
                    ev_charge_w=getattr(slot, "ev_charge_w", 0.0),
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
            self._grid_charge_tracking_known = True
            self._actual_zerohero_import_kwh_today = 0.0
            self._actual_zerohero_export_kwh_today = 0.0
            self._actual_zerohero_bonus_export_kwh_today = 0.0
            self._actual_zerohero_base_export_earnings_today = 0.0
            self._actual_zerohero_bonus_export_earnings_today = 0.0
            self._actual_zerohero_credit_value_today = 0.0
            self._baseline_zerohero_import_kwh_today = 0.0
            self._baseline_zerohero_bonus_export_kwh_today = 0.0
            self._baseline_zerohero_credit_value_today = 0.0
            self._last_cost_tracking_time = None
            self._last_cost_date = today

        # ZeroCharge crosses daily cost boundaries unchanged.  Only a local
        # calendar-month transition resets its month-to-date pool.
        self._ensure_zerocharge_period_state(now)
        self._ensure_zerocharge_period_state(now, baseline=True)

        # Use actual elapsed time to prevent multi-counting
        if self._last_cost_tracking_time is None:
            self._last_cost_tracking_time = now
            return  # First call — no interval to accumulate yet

        # Subtract UTC instants rather than local wall times so a daylight-saving
        # transition cannot double-count or skip an hour of measured settlement.
        elapsed_seconds = elapsed_settlement_seconds(
            self._last_cost_tracking_time,
            now,
        )

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

        if self._provider_key() == "flow_power":
            quota_delta = dict(
                getattr(
                    self,
                    "_pending_flow_power_settlement",
                    {"import": 0.0, "export": 0.0},
                )
            )
            latest_delta = self._settle_flow_power_measurements(
                now,
                grid_import_kw,
                grid_export_kw,
            )
            quota_delta["import"] = quota_delta.get("import", 0.0) + latest_delta["import"]
            quota_delta["export"] = quota_delta.get("export", 0.0) + latest_delta["export"]
            self._pending_flow_power_settlement = {"import": 0.0, "export": 0.0}
            runtime = self._ensure_flow_power_ledger(now=now)
            if runtime is not None:
                snapshot, ledger = runtime
                base_import_prices = self._last_settlement_import_prices or self._last_import_prices
                base_import_price = max(0.0, float(base_import_prices[0] or 0.0))
                from ..flow_power import flow_power_price_series

                series = flow_power_price_series(
                    snapshot,
                    [now],
                    [base_import_price],
                    ledger=ledger,
                )
                actual_import_cost = (
                    grid_import_kwh * series.settlement_import[0]
                    - max(0.0, quota_delta["import"]) * series.import_bonus[0]
                )
                actual_export_earnings = (
                    grid_export_kwh * series.settlement_export[0]
                    + max(0.0, quota_delta["export"]) * series.export_bonus[0]
                )
                if grid_import_kwh > 1e-9:
                    import_price = actual_import_cost / grid_import_kwh
                if grid_export_kwh > 1e-9:
                    export_price = actual_export_earnings / grid_export_kwh
        elif self._provider_key() == "covau":
            quota_delta = dict(
                getattr(
                    self,
                    "_pending_covau_settlement",
                    {"import": 0.0, "export": 0.0},
                )
            )
            latest_delta = self._settle_covau_measurements(
                now,
                grid_import_kw,
                grid_export_kw,
            )
            quota_delta["import"] = quota_delta.get("import", 0.0) + latest_delta["import"]
            quota_delta["export"] = quota_delta.get("export", 0.0) + latest_delta["export"]
            self._pending_covau_settlement = {"import": 0.0, "export": 0.0}
            runtime = self._ensure_covau_ledger(now=now)
            if runtime is not None:
                snapshot, _ledger = runtime
                import_rule, export_rule = covau_quota_rules(snapshot)
                base_import_price = import_price_c_per_kwh(snapshot, now) / 100.0
                base_export_price = snapshot.export_base_c_per_kwh / 100.0
                # The ledger already split the measured interval at tariff
                # windows and the remaining cap. Apply its entire newly settled
                # delta as a correction even when a cumulative meter reports
                # late; tying it to this polling interval would drop delayed
                # 49.9→50.0 kWh boundary credits.
                eligible_import = max(0.0, quota_delta["import"])
                eligible_export = max(0.0, quota_delta["export"])
                actual_import_cost = (
                    (grid_import_kwh * base_import_price)
                    - eligible_import * (import_rule.bonus_price_c_per_kwh / 100.0)
                )
                actual_export_earnings = (
                    grid_export_kwh * base_export_price
                    + eligible_export * (export_rule.bonus_price_c_per_kwh / 100.0)
                )
                # Reuse the measured interval-average rates for baseline and
                # acquisition-cost reporting below. This keeps those secondary
                # estimates consistent at a partial quota boundary.
                if grid_import_kwh > 1e-9:
                    import_price = actual_import_cost / grid_import_kwh
                if grid_export_kwh > 1e-9:
                    export_price = actual_export_earnings / grid_export_kwh
        else:
            custom_runtime = self._ensure_custom_tariff_quota_ledger(now=now)
            if custom_runtime is not None:
                _tariff, rule, _ledger, _content_hash = custom_runtime
                eligible_import = getattr(
                    self,
                    "_pending_custom_tariff_quota_settlement",
                    0.0,
                )
                eligible_import += self._settle_custom_tariff_quota_measurements(
                    now,
                    grid_import_kw,
                )
                self._pending_custom_tariff_quota_settlement = 0.0
                base_import_price = rule.base_price_c_per_kwh / 100.0
                actual_import_cost = (
                    grid_import_kwh * base_import_price
                    - max(0.0, eligible_import)
                    * (rule.bonus_price_c_per_kwh / 100.0)
                )
                if grid_import_kwh > 1e-9:
                    import_price = actual_import_cost / grid_import_kwh

        zerohero_config = self._zerohero_config()
        if zerohero_config is not None:
            actual_zerocharge_period, actual_zerocharge_used, actual_zerocharge_credit = (
                self._ensure_zerocharge_period_state(now)
            )
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
                initial_import_kwh=actual_zerocharge_used,
                initial_period_key=actual_zerocharge_period,
            )
            # A live call contains one timestamp, so the aggregate result is
            # the current month's month-to-date state.
            self._set_zerocharge_period_state(
                period_key=actual_zerocharge_period,
                import_kwh=zerocharge_import,
                credit_value=actual_zerocharge_credit + zerocharge_credit,
            )
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
            baseline_zerocharge_period, baseline_zerocharge_used, baseline_zerocharge_credit_used = (
                self._ensure_zerocharge_period_state(now, baseline=True)
            )
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
                    initial_import_kwh=baseline_zerocharge_used,
                    initial_period_key=baseline_zerocharge_period,
                )
            )
            self._set_zerocharge_period_state(
                period_key=baseline_zerocharge_period,
                import_kwh=baseline_zerocharge_import,
                credit_value=baseline_zerocharge_credit_used + baseline_zerocharge_credit,
                baseline=True,
            )
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

            actual_zerocharge_period, actual_zerocharge_used, _actual_zerocharge_credit = (
                self._ensure_zerocharge_period_state(now)
            )
            baseline_zerocharge_period, baseline_zerocharge_used, _baseline_zerocharge_credit = (
                self._ensure_zerocharge_period_state(now, baseline=True)
            )
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
                    initial_import_kwh=actual_zerocharge_used,
                    initial_period_key=actual_zerocharge_period,
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
                    initial_import_kwh=baseline_zerocharge_used,
                    initial_period_key=baseline_zerocharge_period,
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

    def _daily_supply_charge_for_cost_neutral(
        self, now: datetime
    ) -> tuple[float, str]:
        """Return today's configured fixed supply charge and its source."""
        if not self._entry:
            return 0.0, "missing"
        from ..const import CONF_DAILY_SUPPLY_CHARGE, CONF_MONTHLY_SUPPLY_CHARGE

        options = self._entry.options
        data = self._entry.data
        if CONF_DAILY_SUPPLY_CHARGE in options or CONF_DAILY_SUPPLY_CHARGE in data:
            raw = options.get(
                CONF_DAILY_SUPPLY_CHARGE,
                data.get(CONF_DAILY_SUPPLY_CHARGE, 0.0),
            )
            try:
                value = max(0.0, float(raw or 0.0))
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                return value, "configured_daily"
        if CONF_MONTHLY_SUPPLY_CHARGE in options or CONF_MONTHLY_SUPPLY_CHARGE in data:
            raw = options.get(
                CONF_MONTHLY_SUPPLY_CHARGE,
                data.get(CONF_MONTHLY_SUPPLY_CHARGE, 0.0),
            )
            try:
                monthly = max(0.0, float(raw or 0.0))
            except (TypeError, ValueError):
                monthly = 0.0
            if monthly <= 0:
                return 0.0, "configured_zero"
            days = calendar.monthrange(now.year, now.month)[1]
            return monthly / days, "configured_monthly"
        if CONF_DAILY_SUPPLY_CHARGE in options or CONF_DAILY_SUPPLY_CHARGE in data:
            return 0.0, "configured_zero"
        return 0.0, "missing"

    def _cost_neutral_solve_inputs(
        self,
        *,
        now: datetime,
        timestamps: list[datetime],
        import_prices: list[float],
        export_prices: list[float],
        export_bonus_prices: list[float] | None,
        export_bonus_cap_kwh: float | None,
        import_bonus_prices: list[float] | None,
        import_bonus_cap_kwh: float | None,
        solar: list[float],
        load: list[float],
        current_soc: float,
    ) -> tuple[CostNeutralPlan | None, dict[str, Any]]:
        """Build independent non-self-referential HA-local daily budgets."""
        local_midnight = now.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)
        if not self.cost_neutral_enabled:
            return (
                None,
                {
                    "enabled": False,
                    "effective_mode": (
                        "profit_max" if self.profit_max_mode else "standard"
                    ),
                    "reason": "disabled",
                },
            )

        n = min(
            len(timestamps),
            len(import_prices),
            len(export_prices),
            len(solar),
            len(load),
        )
        local_tz = now.tzinfo
        current_day = now.date().isoformat()
        timezone_name = getattr(local_tz, "key", None) or str(local_tz or "local")
        day_ids: list[str | None] = []
        for timestamp in timestamps[:n]:
            local_timestamp = (
                timestamp.astimezone(local_tz)
                if timestamp.tzinfo is not None and local_tz is not None
                else timestamp
            )
            day = local_timestamp.date().isoformat()
            day_ids.append(day if day >= current_day else None)

        measured_day_matches = getattr(
            self,
            "_last_cost_date",
            None,
        ) in (None, current_day)
        measurement_as_of = (
            self._last_cost_tracking_time
            if measured_day_matches and self._last_cost_tracking_time is not None
            else now
        )
        dt_hours = self._config.interval_minutes / 60.0
        capacity_kwh = max(0.001, self._optimizer.capacity_kwh)
        efficiency = max(0.01, min(1.0, self._optimizer.efficiency))
        energy_kwh = max(0.0, min(1.0, current_soc)) * capacity_kwh
        floor_kwh = (
            self._optimizer._natural_self_consumption_floor(current_soc)
            * capacity_kwh
        )
        forecast_import_kw = [0.0] * n
        forecast_natural_export_kw = [0.0] * n
        for idx in range(n):
            if day_ids[idx] is None:
                continue
            if (
                day_ids[idx] == current_day
                and timestamps[idx] <= measurement_as_of
            ):
                continue
            net_load_kw = float(load[idx] or 0.0) - float(solar[idx] or 0.0)
            if net_load_kw > 0:
                discharge_kw = min(
                    self._optimizer.max_discharge_kw,
                    net_load_kw,
                    max(0.0, energy_kwh - floor_kwh)
                    * efficiency
                    / max(dt_hours, 1e-9),
                )
                forecast_import_kw[idx] = max(0.0, net_load_kw - discharge_kw)
                energy_kwh = max(
                    floor_kwh,
                    energy_kwh - discharge_kw * dt_hours / efficiency,
                )
            elif net_load_kw < 0:
                surplus_kw = -net_load_kw
                charge_kw = min(
                    self._optimizer.max_charge_kw,
                    surplus_kw,
                    max(0.0, capacity_kwh - energy_kwh)
                    / max(efficiency * dt_hours, 1e-9),
                )
                energy_kwh = min(
                    capacity_kwh,
                    energy_kwh + charge_kw * efficiency * dt_hours,
                )
                export_kw = max(0.0, surplus_kw - charge_kw)
                limit_kw = self._optimizer._grid_export_limit_kw_for_range(
                    idx, idx + 1
                )
                if limit_kw is not None:
                    export_kw = min(export_kw, limit_kw)
                forecast_natural_export_kw[idx] = export_kw

        export_bonus = self._optimizer._allocate_capped_bonus(
            forecast_natural_export_kw,
            export_bonus_prices or [0.0] * n,
            export_bonus_cap_kwh,
            self._last_export_bonus_group_ids,
            self._last_export_bonus_caps_by_group,
        )
        import_bonus = self._optimizer._allocate_capped_bonus(
            forecast_import_kw,
            import_bonus_prices or [0.0] * n,
            import_bonus_cap_kwh,
            self._last_import_bonus_group_ids,
            self._last_import_bonus_caps_by_group,
        )
        days = sorted({day for day in day_ids if day is not None})
        forecast_import_costs = {day: 0.0 for day in days}
        forecast_natural_export_earnings = {day: 0.0 for day in days}
        for idx, day in enumerate(day_ids):
            if day is None:
                continue
            forecast_import_costs[day] += (
                float(import_prices[idx]) * forecast_import_kw[idx]
                - float(
                    import_bonus_prices[idx]
                    if import_bonus_prices and idx < len(import_bonus_prices)
                    else 0.0
                ) * import_bonus[idx]
            ) * dt_hours
            forecast_natural_export_earnings[day] += (
                float(export_prices[idx]) * forecast_natural_export_kw[idx]
                + float(
                    export_bonus_prices[idx]
                    if export_bonus_prices and idx < len(export_bonus_prices)
                    else 0.0
                ) * export_bonus[idx]
            ) * dt_hours

        measured_import_cost = (
            self._actual_import_cost_today if measured_day_matches else 0.0
        )
        measured_export_earnings = (
            self._actual_export_earnings_today if measured_day_matches else 0.0
        )
        caps_by_day: dict[str, float] = {}
        fixed_allowances_by_day: dict[str, float] = {}
        day_status: dict[str, dict[str, Any]] = {}
        for day in days:
            day_date = datetime.fromisoformat(day)
            day_reference = now.replace(
                year=day_date.year,
                month=day_date.month,
                day=day_date.day,
                hour=12,
                minute=0,
                second=0,
                microsecond=0,
            )
            supply_charge, supply_source = (
                self._daily_supply_charge_for_cost_neutral(day_reference)
            )
            budget = CostNeutralBudget(
                supply_charge=supply_charge,
                measured_import_cost=(
                    measured_import_cost if day == current_day else 0.0
                ),
                measured_export_earnings=(
                    measured_export_earnings if day == current_day else 0.0
                ),
                forecast_import_cost=forecast_import_costs[day],
                forecast_natural_export_earnings=(
                    forecast_natural_export_earnings[day]
                ),
            )
            cap = budget.battery_export_earnings_cap
            caps_by_day[day] = cap
            fixed_allowances_by_day[day] = budget.fixed_cost_allowance
            day_status[day] = {
                "local_date": day,
                "base_projected_cost": round(budget.base_projected_cost, 4),
                "battery_export_earnings_cap": round(cap, 4),
                "planned_battery_export_earnings": 0.0,
                "uncovered_amount": round(cap, 4),
                "projected_net_daily_cost": round(cap, 4),
                "supply_charge": {
                    "value": round(supply_charge, 4),
                    "source": supply_source,
                },
                "measured_import_cost": round(
                    measured_import_cost if day == current_day else 0.0,
                    4,
                ),
                "measured_export_earnings": round(
                    measured_export_earnings if day == current_day else 0.0,
                    4,
                ),
                "forecast_import_cost": round(forecast_import_costs[day], 4),
                "forecast_natural_export_earnings": round(
                    forecast_natural_export_earnings[day],
                    4,
                ),
                "reason": (
                    "already_covered_by_measured_or_natural_export"
                    if cap <= 1e-6
                    else "insufficient_eligible_capacity"
                ),
                "blocking_reasons": [],
            }

        plan = CostNeutralPlan(
            day_ids=day_ids,
            earnings_caps_by_day=caps_by_day,
            forecast_import_costs_by_day=forecast_import_costs,
            fixed_cost_allowances_by_day=fixed_allowances_by_day,
            current_day=current_day,
            timezone=timezone_name,
            settlement_import_prices=list(import_prices[:n]),
            settlement_export_prices=list(export_prices[:n]),
        )
        current_status = day_status.get(current_day, {})
        status = {
            "enabled": True,
            "effective_mode": "cost_neutral",
            "timezone": timezone_name,
            "current_day": current_day,
            "days": day_status,
            "local_day_end": local_midnight.isoformat(),
            "measurement_as_of": measurement_as_of.isoformat(),
            "base_projected_cost": current_status.get("base_projected_cost", 0.0),
            "battery_export_earnings_cap": current_status.get(
                "battery_export_earnings_cap",
                0.0,
            ),
            "planned_battery_export_earnings": 0.0,
            "uncovered_amount": current_status.get("uncovered_amount", 0.0),
            "projected_net_daily_cost": current_status.get(
                "projected_net_daily_cost",
                0.0,
            ),
            "supply_charge": current_status.get(
                "supply_charge",
                {"value": 0.0, "source": "missing"},
            ),
            "measured_import_cost": current_status.get(
                "measured_import_cost",
                0.0,
            ),
            "measured_export_earnings": current_status.get(
                "measured_export_earnings",
                0.0,
            ),
            "forecast_import_cost": current_status.get(
                "forecast_import_cost",
                0.0,
            ),
            "forecast_natural_export_earnings": current_status.get(
                "forecast_natural_export_earnings",
                0.0,
            ),
            "reason": current_status.get("reason", "no_forecast_days"),
            "blocking_reasons": [],
        }
        return plan, status

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
        ev_charging_w = api_response.get("ev_charging_w", [])
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
                # The load forecast is deliberately house-only.  The
                # schedule's EV series is the other half of the canonical
                # load contract and must be present before rebuilding display
                # grid flows, not added later as a chart-only overlay.
                ev_load_w = (
                    max(0.0, float(ev_charging_w[idx] or 0.0))
                    if idx < len(ev_charging_w)
                    else 0.0
                )
                load_w += ev_load_w
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
            if key == "grid_charge_blackout_windows":
                value = normalize_grid_charge_blackout_windows(value)
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
            current_period, current_import_used, _current_credit = (
                self._ensure_zerocharge_period_state(dt_util.now())
                if import_prices
                else (self._zerocharge_period_key(dt_util.now()), 0.0, 0.0)
            )
            remaining_zerocharge_kwh = max(
                0.0,
                zerocharge_monthly_cap_kwh(zerohero_config, current_period)
                - current_import_used,
            )
            timestamps = self._price_timestamps(len(import_prices))
            zerocharge_slots = []
            for timestamp in timestamps:
                tariff_day = self._zerocharge_period_key(timestamp)
                remaining_zerocharge_kwh = (
                    remaining_zerocharge_kwh
                    if tariff_day == current_period
                    else zerocharge_monthly_cap_kwh(zerohero_config, timestamp)
                )
                zerocharge_slots.append(
                    remaining_zerocharge_kwh > 1e-6
                    and zerocharge_is_in_window(timestamp, zerohero_config)
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

    def _apply_grid_charge_blackout_limit(self, allowed: list[bool]) -> list[bool]:
        """Intersect final non-blackout eligibility with the blackout policy."""
        pre_blackout_allowed = list(allowed)
        blackout_mask = self._grid_charge_blackout_slots(len(allowed))
        result = [
            bool(slot_allowed) and not bool(blackout)
            for slot_allowed, blackout in zip(
                allowed, blackout_mask, strict=False
            )
        ]
        self._last_grid_charge_pre_blackout_allowed = pre_blackout_allowed
        self._last_grid_charge_blackout_slots = blackout_mask
        self._grid_charge_blackout_status = {
            "windows": self._grid_charge_blackout_windows(),
            "blocked_slots": sum(bool(slot) for slot in blackout_mask),
            "eligible_slots_before_blackout": sum(
                bool(slot) for slot in pre_blackout_allowed
            ),
            "eligible_slots": sum(bool(slot) for slot in result),
        }
        return result

    def _grid_charge_blackout_windows(self) -> list[dict[str, str]]:
        """Return the canonical configured local-time force-charge exclusions."""
        try:
            return normalize_grid_charge_blackout_windows(
                getattr(self._config, "grid_charge_blackout_windows", [])
            )
        except ValueError:
            # Old or externally edited entries must fail closed.  The config
            # flow/API rejects invalid values; this is only upgrade defence.
            _LOGGER.warning("Ignoring invalid grid-charge blackout configuration")
            return []

    @staticmethod
    def _time_is_in_grid_charge_blackout(
        timestamp: datetime,
        windows: list[dict[str, str]],
    ) -> bool:
        """Evaluate a local timestamp against start-inclusive/end-exclusive ranges."""
        minute = timestamp.hour * 60 + timestamp.minute
        for window in windows:
            start_text, end_text = window["start"], window["end"]
            start = int(start_text[:2]) * 60 + int(start_text[3:])
            end = int(end_text[:2]) * 60 + int(end_text[3:])
            if (start < end and start <= minute < end) or (
                end < start and (minute >= start or minute < end)
            ):
                return True
        return False

    def _grid_charge_blackout_slots(self, n: int) -> list[bool]:
        """Build a blackout mask on instant-contiguous solve timestamps.

        Evaluating each actual timestamp, rather than adding local wall-clock
        offsets, means both repeated fall-back occurrences obey the policy and
        no imaginary spring-forward slot is created.
        """
        windows = self._grid_charge_blackout_windows()
        if not windows:
            return [False] * n
        localize = getattr(dt_util, "as_local", None)
        return [
            self._time_is_in_grid_charge_blackout(
                localize(timestamp) if callable(localize) else timestamp,
                windows,
            )
            for timestamp in self._price_timestamps(n)
        ]

    def _grid_charge_blackout_active_now(self) -> bool:
        """Return whether optimizer-owned grid charging must be stopped now."""
        windows = self._grid_charge_blackout_windows()
        if not windows:
            return False
        now = dt_util.now()
        localize = getattr(dt_util, "as_local", None)
        return self._time_is_in_grid_charge_blackout(
            localize(now) if callable(localize) else now,
            windows,
        )

    def _apply_custom_tariff_quota_grid_charge_limit(
        self,
        allowed: list[bool],
        solar_forecast: list[float],
        load_forecast: list[float],
    ) -> list[bool]:
        """Stop discretionary battery charging before an import cap is exceeded."""
        runtime = self._ensure_custom_tariff_quota_ledger(now=dt_util.now())
        if runtime is None:
            return self._apply_grid_charge_blackout_limit(allowed)
        tariff, rule, ledger, _content_hash = runtime
        raw_quota = tariff.get("import_quota")
        if (
            not isinstance(raw_quota, dict)
            or raw_quota.get("stop_grid_charging_at_quota", False) is not True
        ):
            return self._apply_grid_charge_blackout_limit(allowed)

        result = list(allowed)
        timestamps = self._price_timestamps(len(result))
        current_day = ledger.state.tariff_day
        budgets: dict[str, float] = {}
        dt_hours = max(1, int(self._config.interval_minutes or 5)) / 60.0
        max_battery_import_kwh = (
            max(0.0, float(self._config.max_charge_w)) / 1000.0 * dt_hours
        )
        for idx, timestamp in enumerate(timestamps):
            if not rule.contains(timestamp):
                continue
            day = tariff_datetime(timestamp, rule.timezone_token).date().isoformat()
            if day not in budgets:
                budgets[day] = (
                    ledger.remaining_kwh(CUSTOM_TARIFF_IMPORT_RULE_ID)
                    if day == current_day
                    and ledger.state.confidence != "unknown"
                    else rule.daily_cap_kwh
                    if day != current_day
                    else 0.0
                )
            solar_kw = (
                max(0.0, float(solar_forecast[idx]))
                if idx < len(solar_forecast)
                else 0.0
            )
            load_kw = (
                max(0.0, float(load_forecast[idx]))
                if idx < len(load_forecast)
                else 0.0
            )
            expected_site_import_kwh = max(0.0, load_kw - solar_kw) * dt_hours
            budgets[day] = max(0.0, budgets[day] - expected_site_import_kwh)
            if not result[idx]:
                continue
            if budgets[day] + 1e-9 < max_battery_import_kwh:
                result[idx] = False
                continue
            budgets[day] -= max_battery_import_kwh
        return self._apply_grid_charge_blackout_limit(result)

    async def force_reoptimize(self) -> Any:
        """Force immediate re-optimization."""
        await self._run_optimization(force=True)
        return self._current_schedule

    @staticmethod
    def _settings_groups() -> dict[str, Any]:
        """Return non-breaking mobile metadata for grouped optimizer settings."""
        return optimizer_settings_groups()

    def get_forecast_data(self) -> dict[str, Any]:
        """Get forecast data for LP forecast sensors.

        Returns summary values (for sensor state) and full arrays (for attributes).
        """
        data: dict[str, Any] = {
            "available": self._last_solar_forecast is not None,
            "solar_nowcast_derate": round(self._solar_nowcast_derate, 3),
        }
        learner = getattr(self, "_solar_forecast_learner", None)
        forecaster = getattr(self, "_solar_forecaster", None)
        if learner is not None:
            learning_diagnostics = learner.diagnostics(
                getattr(forecaster, "last_forecast_source", None)
            )
            learning_diagnostics["nowcast_allowance_kwh"] = round(
                getattr(self, "_last_solar_nowcast_allowance_kwh", 0.0), 3
            )
            learning_diagnostics["effective_margin_kwh"] = getattr(
                self, "_last_solar_effective_error_margin_kwh", None
            )
            data["solar_forecast_learning"] = learning_diagnostics
        battery_learner = getattr(self, "_battery_efficiency_learner", None)
        if battery_learner is not None:
            data["battery_efficiency_learning"] = battery_learner.diagnostics(
                application_enabled=self.battery_efficiency_learning_enabled,
                now=dt_util.now(),
            )
        if self._last_solar_nowcast_ratio is not None:
            data["solar_nowcast_ratio"] = round(self._last_solar_nowcast_ratio, 3)
        dt_h = self._config.interval_minutes / 60

        if self._last_solar_forecast:
            data["solar_forecast_kwh"] = sum(self._last_solar_forecast) * dt_h
            data["solar_peak_kw"] = max(self._last_solar_forecast)
            data["solar_forecast"] = self._last_solar_forecast
            provenance = (
                getattr(self, "_last_raw_solar_forecast", None),
                getattr(self, "_last_planned_solar_forecast", None),
                getattr(self, "_last_solar_curtailment_forecast", None),
            )
            if all(value is not None for value in provenance):
                raw, planned, curtailed = provenance
                if len(raw) == len(planned) == len(curtailed):
                    data["raw_solar_forecast"] = raw
                    data["planned_solar_forecast"] = planned
                    data["solar_curtailment_forecast"] = curtailed

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
                data["load_history_diagnostics"] = load_summary.get(
                    "history_diagnostics", {}
                )
                data["load_recent_diagnostics"] = load_summary.get(
                    "recent_load_diagnostics", {}
                )
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
        ev_optimizer_policy = getattr(self, "_last_ev_optimizer_policy", None)
        if ev_optimizer_policy:
            data["ev_optimizer_policy_by_vehicle"] = ev_optimizer_policy

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
            schedule = self._current_schedule
            charge_kw = [
                -round((action.battery_charge_w or 0.0) / 1000.0, 3)
                for action in schedule.actions
            ]
            discharge_kw = [
                round((action.battery_discharge_w or 0.0) / 1000.0, 3)
                for action in schedule.actions
            ]
            home_consumption_kw = [
                round(float(value or 0.0) / 1000.0, 3)
                for value in getattr(schedule, "battery_consume_w", [])
            ]
            export_kw = [
                round(float(value or 0.0) / 1000.0, 3)
                for value in getattr(schedule, "battery_export_w", [])
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
            data["battery_home_consumption_forecast"] = home_consumption_kw
            data["battery_export_forecast"] = export_kw
            data["battery_power_forecast"] = net_kw

        return data

    def get_api_data(self) -> dict[str, Any]:
        """Get data for HTTP API and mobile app."""
        optimizer_available = self._optimizer is not None

        schedule_age_s = (
            (dt_util.now() - self._last_update_time).total_seconds()
            if self._last_update_time
            else None
        )
        stale_after_s = 3 * max(1, int(getattr(self._config, "interval_minutes", 5) or 5)) * 60
        is_stale = (
            optimizer_available
            and schedule_age_s is not None
            and schedule_age_s > stale_after_s
        )

        # Determine status message
        if optimizer_available:
            if self._current_schedule and self._current_schedule.actions:
                status_message = "Optimization active"
            else:
                status_message = "Optimizer ready — waiting for data"
        else:
            status_message = "Optimizer not initialized"

        # Get current action info
        default_action = (
            "self_consumption"
            if self._should_disable_idle_schedule()
            else "idle"
        )
        current_action = default_action
        actual_battery_power_w = self._get_actual_battery_power_w()
        current_power_w = actual_battery_power_w
        planned_current_action = current_action
        planned_current_power_w = current_power_w
        effective_current_action = current_action
        current_action_end_time = None  # When the current scheduled action segment ends
        next_action = default_action
        next_action_time = None
        next_action_power_w = 0

        if self._current_schedule and self._current_schedule.actions:
            ca = self._get_current_action()
            if ca:
                current_action = ca.action
                current_power_w = ca.power_w
                planned_current_action = current_action
                planned_current_power_w = current_power_w
                force_state = self._get_active_force_state()
                force_type = force_state.get("type") if force_state.get("active") else None
                last_executed_action = getattr(self, "_last_executed_action", None)
                last_executed_planned_action = getattr(
                    self,
                    "_last_executed_planned_action",
                    None,
                )
                if force_type == "self_consumption":
                    # A user-owned self-consumption override controls the
                    # hardware even when the retained LP plan still wants a
                    # charge/export action. Keep the planned action visible,
                    # but publish the mode that is actually in force.
                    effective_current_action = "self_consumption"
                    current_action = effective_current_action
                    current_power_w = actual_battery_power_w
                elif force_type in ("charge", "discharge"):
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
                    # A schedule is only intent. If execution was skipped,
                    # blocked, or failed before recording an acknowledgement,
                    # keep the plan in planned_current_action and report the
                    # last accepted hardware action (or the safe default).
                    safe_default_action = (
                        self._effective_runtime_action(default_action, ca.timestamp)
                        or default_action
                    )
                    effective_current_action = (
                        last_executed_action or safe_default_action
                    )
                    current_action = effective_current_action
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

        def _legacy_action(action_name: str | None) -> str | None:
            return (
                "self_consumption"
                if action_name == "solar_export"
                else action_name
            )

        planned_current_action_detail = (
            planned_current_action
            if planned_current_action == "solar_export"
            else None
        )
        effective_current_action_detail = (
            effective_current_action
            if effective_current_action == "solar_export"
            else None
        )
        current_action_detail = (
            current_action if current_action == "solar_export" else None
        )
        next_action_detail = next_action if next_action == "solar_export" else None
        current_action = _legacy_action(current_action)
        planned_current_action = _legacy_action(planned_current_action)
        effective_current_action = _legacy_action(effective_current_action)
        next_action = _legacy_action(next_action)

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
            **currency_metadata(currency_for_entry(self._entry, self.hass)),
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
            "battery_efficiency_learning_enabled": (
                self.battery_efficiency_learning_enabled
            ),
            "profit_max_enabled": self.profit_max_mode,
            "profit_max_mode": self.profit_max_mode,
            "cost_neutral_enabled": self.cost_neutral_enabled,
            "cost_neutral": dict(getattr(self, "_cost_neutral_status", {
                "enabled": self.cost_neutral_enabled,
                "effective_mode": (
                    "cost_neutral" if self.cost_neutral_enabled else "standard"
                ),
                "reason": "disabled" if not self.cost_neutral_enabled else "pending_solve",
            })),
            "charge_by_time_enabled": self.charge_by_time_enabled,
            "auto_apply_reserve_enabled": self.auto_apply_reserve_enabled,
            "manual_backup_reserve": self.manual_backup_reserve,
            "backup_reserve": self._config.backup_reserve,
            "settings_groups": self._settings_groups(),
            "settings_schema": optimizer_settings_schema(),
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
            "optimization_status": (
                "stale"
                if is_stale
                else ("active" if optimizer_available else "not_available")
            ),
            "schedule_age_s": schedule_age_s,
            "current_action": current_action,
            "current_action_detail": current_action_detail,
            "current_power_w": current_power_w,
            "planned_current_action": planned_current_action,
            "planned_current_action_detail": planned_current_action_detail,
            "planned_current_power_w": planned_current_power_w,
            "effective_current_action": effective_current_action,
            "effective_current_action_detail": effective_current_action_detail,
            "actual_battery_power_w": actual_battery_power_w,
            "current_action_end_time": current_action_end_time,
            "next_action": next_action,
            "next_action_detail": next_action_detail,
            "next_action_time": next_action_time,
            "next_action_power_w": next_action_power_w,
            "last_optimization": self._last_update_time.isoformat() if self._last_update_time else None,
            "predicted_cost": self._get_daily_cost(),
            "predicted_savings": self._get_daily_savings(),
            "lp_stats": lp_stats,
            "reserve_recommendation": reserve_recommendation,
            "profit_max_solar_export": {
                "capability": dict(
                    getattr(self, "_solar_export_capability_status", {}) or {}
                ),
                "planned_slots": sum(
                    bool(slot)
                    for slot in getattr(
                        self, "_last_profit_max_solar_export_slots", []
                    )
                ),
                "hold": (
                    getattr(self, "_solar_export_hold", None).status
                    if getattr(self, "_solar_export_hold", None)
                    else {}
                ),
            },
            "external_energy_resources": [
                plan.as_dict()
                for plan in getattr(
                    self,
                    "_last_external_energy_allocation",
                    ExternalEnergyAllocationResult(reason="disabled"),
                ).plans
            ],
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
                "min_export_price": round(
                    normalize_min_export_price(self._config.min_export_price) * 100,
                    3,
                ),
                "backup_energy_wh": self._config.backup_energy_wh,
                "backup_energy_max_power_w": self._config.backup_energy_max_power_w,
                "backup_energy_start": self._config.backup_energy_start,
                "backup_energy_end": self._config.backup_energy_end,
                "grid_charge_soc_cap": int(
                    round(self._soc_ratio(self._config.grid_charge_soc_cap, 1.0) * 100)
                ),
                "allow_grid_charge": self._config.allow_grid_charge,
                "grid_charge_blackout_windows": self._grid_charge_blackout_windows(),
                "spread_export_enabled": self._config.spread_export_enabled,
                "spread_import_enabled": self._config.spread_import_enabled,
                "disable_idle_enabled": self.disable_idle_enabled,
                "battery_efficiency_learning_enabled": (
                    self.battery_efficiency_learning_enabled
                ),
                "profit_max_enabled": self.profit_max_mode,
                "cost_neutral_enabled": self.cost_neutral_enabled,
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
        active_force = self._get_active_force_state()
        cached_manual_projection = dict(
            getattr(
                self,
                "_last_manual_control_projection",
                {"active": False},
            )
        )
        if (
            not active_force.get("active")
            or active_force.get("source") == "optimizer"
        ):
            cached_manual_projection = {"active": False}
        elif (
            not cached_manual_projection.get("active")
            or cached_manual_projection.get("control_type")
            != active_force.get("type")
        ):
            expires_at = self._as_utc_datetime(active_force.get("expires_at"))
            cached_manual_projection = {
                "active": True,
                "control_type": active_force.get("type"),
                "control_source": "manual",
                "projection": "pending",
                "expires_at": expires_at.isoformat() if expires_at else None,
                "projected_slots": 0,
            }
        data["manual_control_projection"] = cached_manual_projection

        energy_data = self._get_energy_data()
        if isinstance(energy_data, dict):
            try:
                battery_soc_percent = float(energy_data.get("battery_level"))
            except (TypeError, ValueError):
                battery_soc_percent = math.nan
            if math.isfinite(battery_soc_percent):
                data["battery_soc_percent"] = round(
                    max(0.0, min(100.0, battery_soc_percent)),
                    1,
                )

        network_manager = self.hass.data.get("power_sync", {}).get(
            self.entry_id, {}
        ).get("network_envelope_manager")
        if network_manager is not None:
            data["network_envelope"] = network_manager.snapshot.to_dict()

        # Add load forecast summary for mobile app
        load_summary = self._summarise_load_forecast()
        forecast_summary: dict[str, Any] = {}
        if load_summary:
            forecast_summary.update({
                "load_today_remaining_kwh": load_summary["today_remaining_kwh"],
                "load_tomorrow_kwh": load_summary["tomorrow_kwh"],
                "load_peak_kw": load_summary["peak_kw"],
                "temperature_adjusted": load_summary["temperature_adjusted"],
                "history_diagnostics": load_summary.get("history_diagnostics", {}),
                "recent_load_diagnostics": load_summary.get(
                    "recent_load_diagnostics", {}
                ),
                "away_mode": load_summary["away_mode"],
                "profit_max_mode": load_summary.get("profit_max_mode", False),
                "charge_by_time_enabled": load_summary.get("charge_by_time_enabled", False),
            })
        if self._last_solar_forecast:
            intervals_24h = min(
                len(self._last_solar_forecast),
                int(24 * 60 / max(1, self._config.interval_minutes)),
            )
            solar_24h = self._last_solar_forecast[:intervals_24h]
            if solar_24h:
                interval_hours = self._config.interval_minutes / 60
                forecast_summary.update(
                    {
                        "solar_next_24h_kwh": round(
                            sum(max(0.0, float(value or 0.0)) for value in solar_24h)
                            * interval_hours,
                            2,
                        ),
                        "solar_peak_kw": round(
                            max(max(0.0, float(value or 0.0)) for value in solar_24h),
                            2,
                        ),
                    }
                )
        if forecast_summary:
            data["forecast_summary"] = forecast_summary

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
        ev_optimizer_policy = getattr(self, "_last_ev_optimizer_policy", None)
        if ev_optimizer_policy:
            data["ev_optimizer_policy_by_vehicle"] = ev_optimizer_policy

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
        provider_contract = self.get_provider_contract()
        if provider_contract is not None:
            data["provider_contract"] = provider_contract
            provider_key = (
                "covau"
                if provider_contract.get("plan", {}).get("source_kind")
                != "custom_tariff"
                else "custom_tariff"
            )
            data["daily_cost_breakdown"][provider_key] = provider_contract

        # Add EV status if EV coordination is active
        if self._ev_coordinator:
            data["ev"] = self._ev_coordinator.get_status()

            # Also include auto-schedule plan data if available
            from ..automations.ev_charging_planner import get_auto_schedule_executor
            executor = get_auto_schedule_executor()
            if executor:
                data["ev"]["auto_schedule"] = executor.get_all_states(
                    enabled_only=True,
                )

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
            provenance = (
                getattr(self, "_last_raw_solar_forecast", None),
                getattr(self, "_last_planned_solar_forecast", None),
                getattr(self, "_last_solar_curtailment_forecast", None),
            )
            if all(value is not None for value in provenance):
                raw, planned, curtailed = provenance
                load = self._last_load_forecast
                if (
                    load is not None
                    and len(raw) == len(planned) == len(curtailed) == len(load)
                    == n_sched
                ):
                    data["forecast_series"] = {
                        "timestamps": list(api_response["timestamps"]),
                        "interval_minutes": self._config.interval_minutes,
                        "raw_forecast_values_kw": raw,
                        "planned_forecast_values_kw": planned,
                        "curtailment_values_kw": curtailed,
                        "load_forecast_values_kw": load,
                    }
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
            grid_export_limits = getattr(self, "_last_grid_export_limits_w", None)
            if grid_export_limits is not None:
                api_response["grid_export_limit_w"] = grid_export_limits[:n_sched]
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
            n_sched_pts = len(api_response["timestamps"])
            effective_ev_load = getattr(
                self, "_last_effective_ev_load_forecast_w", None
            )
            from .ev_load_plan import ev_chart_series

            ev_series = ev_chart_series(
                effective_ev_load,
                [
                    getattr(action, "ev_charge_w", 0.0)
                    for action in self._current_schedule.actions
                ],
                n_sched_pts,
            )
            if ev_series is not None:
                api_response["ev_charging_w"] = ev_series
            by_vehicle = getattr(self, "_last_ev_charge_by_vehicle_w", None)
            if by_vehicle:
                api_response["ev_charging_by_vehicle_w"] = {
                    vehicle_id: list(series[:n_sched_pts])
                    for vehicle_id, series in by_vehicle.items()
                }
            elif self._ev_coordinator and data.get("ev"):
                # Backward-compatible fallback when no optimizer EV overlay exists.
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
            source_by_vehicle = getattr(
                self, "_last_ev_source_by_vehicle_w", None
            )
            if source_by_vehicle:
                api_response["ev_charging_source_by_vehicle_w"] = {
                    vehicle_id: {
                        source: list(series[:n_sched_pts])
                        for source, series in sources.items()
                    }
                    for vehicle_id, sources in source_by_vehicle.items()
                }
            if ev_optimizer_policy:
                api_response["ev_optimizer_policy_by_vehicle"] = (
                    ev_optimizer_policy
                )

            projection_payload = getattr(
                self, "_last_price_level_projection", None
            )
            if projection_payload:
                api_response["ev_charging_projection"] = projection_payload

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
                    and action_ranges[-1].get("action_detail")
                    == ad.get("action_detail")
                    and action_ranges[-1].get("control_source")
                    == ad.get("control_source")
                    and action_ranges[-1].get("control_action")
                    == ad.get("control_action")
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
                            {"action_detail": ad["action_detail"]}
                            if ad.get("action_detail")
                            else {}
                        ),
                        **(
                            {"action_reason": ad["action_reason"]}
                            if ad.get("action_reason")
                            else {}
                        ),
                        **(
                            {"planned_action": ad["planned_action"]}
                            if ad.get("planned_action")
                            else {}
                        ),
                        **(
                            {"control_source": ad["control_source"]}
                            if ad.get("control_source")
                            else {}
                        ),
                        **(
                            {"control_action": ad["control_action"]}
                            if ad.get("control_action")
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
        data["calibration_source"] = _cal_entry_data.get("calibration_source")
        data["calibration_sources"] = list(
            _cal_entry_data.get("calibration_sources") or []
        )

        return data

    async def set_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Update optimization settings from API."""
        settings = dict(settings)
        response = {"success": True, "changes": []}
        if (
            settings.get("profit_max_enabled") is True
            and settings.get("cost_neutral_enabled") is True
        ):
            return {
                "success": False,
                "error": "Profit Max and Cost Neutral cannot both be enabled",
                "changes": [],
            }
        rerun_after_settings = False
        charge_by_time_display_changed = False

        if "daily_supply_charge" in settings:
            try:
                daily_supply_charge = float(
                    settings["daily_supply_charge"] or 0.0
                )
            except (TypeError, ValueError):
                daily_supply_charge = -1.0
            if not math.isfinite(daily_supply_charge) or daily_supply_charge < 0:
                return {
                    "success": False,
                    "error": "Daily supply charge must be a non-negative number",
                    "changes": [],
                }
            settings["daily_supply_charge"] = daily_supply_charge

        # A non-positive battery specification means "clear the manual
        # override", matching the mobile Reset to Auto action. Never push a
        # zero capacity/power into the live LP model while waiting for
        # detection; clear persistence first, then re-detect in place.
        battery_spec_keys = (
            "battery_capacity_wh",
            "max_charge_w",
            "max_discharge_w",
        )
        cleared_battery_specs: set[str] = set()
        for key in battery_spec_keys:
            if key not in settings:
                continue
            try:
                should_clear = float(settings[key]) <= 0
            except (TypeError, ValueError):
                should_clear = False
            if should_clear:
                cleared_battery_specs.add(key)
                settings.pop(key)

        if cleared_battery_specs and self._entry:
            from ..const import (
                CONF_OPTIMIZATION_BATTERY_CAPACITY_WH,
                CONF_OPTIMIZATION_MAX_CHARGE_W,
                CONF_OPTIMIZATION_MAX_DISCHARGE_W,
                DOMAIN as _SKIP_DOM,
            )

            option_by_setting = {
                "battery_capacity_wh": CONF_OPTIMIZATION_BATTERY_CAPACITY_WH,
                "max_charge_w": CONF_OPTIMIZATION_MAX_CHARGE_W,
                "max_discharge_w": CONF_OPTIMIZATION_MAX_DISCHARGE_W,
            }
            new_data = dict(self._entry.data)
            new_options = dict(self._entry.options)
            persisted_before = (dict(new_data), dict(new_options))
            for key in cleared_battery_specs:
                option_key = option_by_setting[key]
                new_data.pop(option_key, None)
                new_options.pop(option_key, None)
                response["changes"].append(f"cleared {key}")

            if (new_data, new_options) != persisted_before:
                self.hass.data.get(_SKIP_DOM, {}).get(
                    self.entry_id,
                    {},
                )["_skip_reload"] = True
            self.hass.config_entries.async_update_entry(
                self._entry,
                data=new_data,
                options=new_options,
            )

        if cleared_battery_specs:
            from ..const import BATTERY_CAPACITY_DEFAULTS, BATTERY_POWER_DEFAULTS

            default_config = OptimizationConfig()
            default_capacity_wh = BATTERY_CAPACITY_DEFAULTS.get(
                self.battery_system, default_config.battery_capacity_wh
            )
            default_power_w = BATTERY_POWER_DEFAULTS.get(
                self.battery_system,
                default_config.max_charge_w,
            )
            defaults_by_setting = {
                "battery_capacity_wh": default_capacity_wh,
                "max_charge_w": default_power_w,
                "max_discharge_w": default_power_w,
            }
            for key in cleared_battery_specs:
                setattr(self._config, key, defaults_by_setting[key])
            self._battery_specs_source = "default"
            await self._auto_detect_battery_specs()
            if self._optimizer:
                self._optimizer.update_config(
                    capacity_wh=self._config.battery_capacity_wh,
                    max_charge_w=self._config.max_charge_w,
                    max_discharge_w=self._config.max_discharge_w,
                )
            rerun_after_settings = True

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
            "max_grid_charge_price", "min_export_price", "grid_charge_soc_cap",
            "grid_charge_blackout_windows",
            "allow_grid_charge", "backup_reserve", "horizon_hours",
            "backup_energy_wh", "backup_energy_max_power_w",
            "backup_energy_start", "backup_energy_end",
        ]
        raw_config_updates = {k: v for k, v in settings.items() if k in config_keys}
        config_updates = dict(raw_config_updates)
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
            if "min_export_price" in config_updates:
                try:
                    min_export_price_cents = float(
                        config_updates["min_export_price"] or 0.0
                    )
                except (TypeError, ValueError, OverflowError):
                    min_export_price_cents = -1.0
                if (
                    not math.isfinite(min_export_price_cents)
                    or min_export_price_cents < 0
                ):
                    response["success"] = False
                    response["error"] = (
                        "min_export_price must be a non-negative number"
                    )
                    return response
                config_updates["min_export_price"] = normalize_min_export_price(
                    min_export_price_cents / 100.0
                )
            for key in ("backup_energy_wh", "backup_energy_max_power_w"):
                if key not in config_updates:
                    continue
                try:
                    config_updates[key] = max(
                        0,
                        int(float(config_updates[key] or 0)),
                    )
                except (TypeError, ValueError, OverflowError):
                    response["success"] = False
                    response["error"] = f"{key} must be a non-negative number"
                    return response
            if any(
                key in config_updates
                for key in (
                    "backup_energy_wh",
                    "backup_energy_max_power_w",
                    "backup_energy_start",
                    "backup_energy_end",
                )
            ):
                candidate_resource = ExternalEnergyResourceConfig(
                    resource_id="configured_backup_v2x",
                    usable_energy_wh=config_updates.get(
                        "backup_energy_wh",
                        getattr(self._config, "backup_energy_wh", 0),
                    ),
                    max_power_w=config_updates.get(
                        "backup_energy_max_power_w",
                        getattr(
                            self._config,
                            "backup_energy_max_power_w",
                            DEFAULT_OPTIMIZATION_BACKUP_ENERGY_MAX_POWER_W,
                        ),
                    ),
                    start_local=config_updates.get(
                        "backup_energy_start",
                        getattr(
                            self._config,
                            "backup_energy_start",
                            DEFAULT_OPTIMIZATION_BACKUP_ENERGY_START,
                        ),
                    ),
                    end_local=config_updates.get(
                        "backup_energy_end",
                        getattr(
                            self._config,
                            "backup_energy_end",
                            DEFAULT_OPTIMIZATION_BACKUP_ENERGY_END,
                        ),
                    ),
                )
                resource_error = candidate_resource.validate()
                if resource_error is not None:
                    response["success"] = False
                    response["error"] = (
                        "Invalid backup / V2X planning resource: "
                        f"{resource_error}"
                    )
                    return response
            if "grid_charge_soc_cap" in config_updates:
                config_updates["grid_charge_soc_cap"] = self._soc_ratio(
                    config_updates["grid_charge_soc_cap"],
                    1.0,
                )
            if "grid_charge_blackout_windows" in config_updates:
                try:
                    config_updates["grid_charge_blackout_windows"] = (
                        normalize_grid_charge_blackout_windows(
                            config_updates["grid_charge_blackout_windows"]
                        )
                    )
                except ValueError as err:
                    response["success"] = False
                    response["error"] = f"Invalid grid-charge blackout windows: {err}"
                    return response
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

            if self.battery_system == "sigenergy" and self._entry:
                from ..const import (
                    CONF_SIGENERGY_CHARGE_RATE_LIMIT_KW,
                    CONF_SIGENERGY_DISCHARGE_RATE_LIMIT_KW,
                )

                if "max_charge_w" in config_updates:
                    config_updates["max_charge_w"] = (
                        sigenergy_capped_optimizer_limit_w(
                            raw_config_updates["max_charge_w"],
                            self._entry.data.get(
                                CONF_SIGENERGY_CHARGE_RATE_LIMIT_KW
                            ),
                        )
                    )
                if "max_discharge_w" in config_updates:
                    config_updates["max_discharge_w"] = (
                        sigenergy_capped_optimizer_limit_w(
                            raw_config_updates["max_discharge_w"],
                            self._entry.data.get(
                                CONF_SIGENERGY_DISCHARGE_RATE_LIMIT_KW
                            ),
                        )
                    )

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
                    CONF_OPTIMIZATION_MIN_EXPORT_PRICE,
                    CONF_OPTIMIZATION_BACKUP_ENERGY_WH,
                    CONF_OPTIMIZATION_BACKUP_ENERGY_MAX_POWER_W,
                    CONF_OPTIMIZATION_BACKUP_ENERGY_START,
                    CONF_OPTIMIZATION_BACKUP_ENERGY_END,
                    CONF_OPTIMIZATION_GRID_CHARGE_SOC_CAP,
                    CONF_OPTIMIZATION_GRID_CHARGE_BLACKOUT_WINDOWS,
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
                if "min_export_price" in settings:
                    min_export_price = config_updates["min_export_price"]
                    new_options[CONF_OPTIMIZATION_MIN_EXPORT_PRICE] = min_export_price
                    new_data[CONF_OPTIMIZATION_MIN_EXPORT_PRICE] = min_export_price
                if "backup_energy_wh" in settings:
                    backup_energy_wh = config_updates["backup_energy_wh"]
                    new_options[CONF_OPTIMIZATION_BACKUP_ENERGY_WH] = backup_energy_wh
                    new_data[CONF_OPTIMIZATION_BACKUP_ENERGY_WH] = backup_energy_wh
                if "backup_energy_max_power_w" in settings:
                    backup_energy_max_power_w = config_updates[
                        "backup_energy_max_power_w"
                    ]
                    new_options[CONF_OPTIMIZATION_BACKUP_ENERGY_MAX_POWER_W] = (
                        backup_energy_max_power_w
                    )
                    new_data[CONF_OPTIMIZATION_BACKUP_ENERGY_MAX_POWER_W] = (
                        backup_energy_max_power_w
                    )
                if "backup_energy_start" in settings:
                    new_options[CONF_OPTIMIZATION_BACKUP_ENERGY_START] = str(
                        settings["backup_energy_start"]
                    )
                    new_data[CONF_OPTIMIZATION_BACKUP_ENERGY_START] = str(
                        settings["backup_energy_start"]
                    )
                if "backup_energy_end" in settings:
                    new_options[CONF_OPTIMIZATION_BACKUP_ENERGY_END] = str(
                        settings["backup_energy_end"]
                    )
                    new_data[CONF_OPTIMIZATION_BACKUP_ENERGY_END] = str(
                        settings["backup_energy_end"]
                    )
                if "grid_charge_soc_cap" in settings:
                    soc_cap = self._soc_ratio(settings["grid_charge_soc_cap"], 1.0)
                    new_options[CONF_OPTIMIZATION_GRID_CHARGE_SOC_CAP] = soc_cap
                    new_data[CONF_OPTIMIZATION_GRID_CHARGE_SOC_CAP] = soc_cap
                if "grid_charge_blackout_windows" in config_updates:
                    windows = config_updates["grid_charge_blackout_windows"]
                    new_options[CONF_OPTIMIZATION_GRID_CHARGE_BLACKOUT_WINDOWS] = windows
                    new_data[CONF_OPTIMIZATION_GRID_CHARGE_BLACKOUT_WINDOWS] = windows
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
            self._sync_brand_restore_targets(hw_int)
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

        if "cost_neutral_enabled" in settings:
            new_val = bool(settings["cost_neutral_enabled"])
            changed = self.set_cost_neutral_enabled(new_val)
            if changed:
                response["changes"].append(
                    f"cost_neutral_enabled: {settings['cost_neutral_enabled']}"
                )
                rerun_after_settings = True

        if "daily_supply_charge" in settings:
            daily_supply_charge = settings["daily_supply_charge"]
            if self._entry:
                from ..const import CONF_DAILY_SUPPLY_CHARGE, DOMAIN as _SKIP_DOM

                new_data = dict(self._entry.data)
                new_options = dict(self._entry.options)
                persisted_changed = (
                    new_data.get(CONF_DAILY_SUPPLY_CHARGE) != daily_supply_charge
                    or new_options.get(CONF_DAILY_SUPPLY_CHARGE)
                    != daily_supply_charge
                )
                new_data[CONF_DAILY_SUPPLY_CHARGE] = daily_supply_charge
                new_options[CONF_DAILY_SUPPLY_CHARGE] = daily_supply_charge
                if persisted_changed:
                    self.hass.data.get(_SKIP_DOM, {}).get(
                        self.entry_id, {}
                    )["_skip_reload"] = True
                    self.hass.config_entries.async_update_entry(
                        self._entry,
                        data=new_data,
                        options=new_options,
                    )
            # The HA options flow persists before applying live settings, so
            # the entry may already contain this value when we get here.
            # Rebuild the plan whenever the field was submitted regardless.
            rerun_after_settings = True
            response["changes"].append(
                f"daily_supply_charge: {daily_supply_charge:.2f}"
            )

        if "charge_by_time_enabled" in settings:
            new_val = bool(settings["charge_by_time_enabled"])
            changed = self.set_charge_by_time_enabled(new_val, publish=False)
            if changed:
                response["changes"].append(
                    f"charge_by_time_enabled: {settings['charge_by_time_enabled']}"
                )
                rerun_after_settings = True
                charge_by_time_display_changed = True

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

        if "battery_efficiency_learning_enabled" in settings:
            new_val = bool(settings["battery_efficiency_learning_enabled"])
            changed = self.set_battery_efficiency_learning_enabled(new_val)
            if changed:
                response["changes"].append(
                    "battery_efficiency_learning_enabled: "
                    f"{self.battery_efficiency_learning_enabled}"
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
                charge_by_time_display_changed = True

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
                charge_by_time_display_changed = True

        # Handle EV integration toggle
        if "ev_integration" in settings:
            ev_enabled = settings["ev_integration"]
            self._ev_integration_enabled = ev_enabled
            if self._entry:
                from ..const import CONF_OPTIMIZATION_EV_INTEGRATION
                new_options = dict(self._entry.options)
                persisted_changed = new_options.get(CONF_OPTIMIZATION_EV_INTEGRATION) != ev_enabled
                new_options[CONF_OPTIMIZATION_EV_INTEGRATION] = ev_enabled
                self.hass.config_entries.async_update_entry(self._entry, options=new_options)
                response["changes"].append(f"ev_integration: {ev_enabled}")
                # EV participation owns coordinator lifecycle as well as the
                # forecast flag. Deliberately allow the options listener to
                # reload when this value changes so EV coordinators are
                # started/stopped; an in-place flag flip is incomplete.
                if not persisted_changed:
                    _LOGGER.debug("EV integration setting was already %s", ev_enabled)

        if charge_by_time_display_changed:
            self.async_set_updated_data(self.get_api_data())

        if rerun_after_settings and getattr(self, "_enabled", False):
            self._schedule_settings_reoptimization()

        return response

    async def _async_update_data(self) -> dict[str, Any]:
        """Periodic data update — return cached API data.

        LP optimization is driven exclusively by _schedule_polling_loop and
        _initial_opt_task; running it here as well caused duplicate Modbus
        writes when both fired at the same 5-min boundary.
        """
        # Cost and quota settlement must still advance on every coordinator
        # refresh. A full LP solve can be delayed while an optimizer-owned
        # force action is active; tracking only after solves then hits the
        # 10-minute stale-sample cap and under-counts capped tariff energy.
        # Record the interval before applying a boundary action so it is
        # attributed to the hardware state that produced the latest telemetry.
        self._track_actual_cost()
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
