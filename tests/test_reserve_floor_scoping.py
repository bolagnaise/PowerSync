"""Regression tests for OB-14: per-slot export reserve floor must win over the
cross-run scalar recommendation for actions inside a tracked per-slot run.

Background: `_force_discharge_reserve_floor` first does an accurate per-slot
lookup against `_active_export_reserve_floor_timestamps` /
`_active_export_reserve_floor_slots` (populated by
`_post_processed_export_reserve_floor_slots`, which scopes a floor to each
individual export run). When Auto-Apply Reserve is on, it then used to
unconditionally `max()` in the scalar `home_load_export_floor_percent` from
`best_meta` -- the single highest-floor run of the day -- for any action whose
bridge run starts on the same calendar day. That meant a morning export window
the LP planned to drain to its own (lower) floor got blocked at runtime by an
unrelated evening window's (higher) floor: a plan-vs-execution divergence.

The fix: once the per-slot lookup finds an entry for the action's own slot,
that value is authoritative and the scalar must not be folded in. The scalar
remains the fallback base for actions outside the tracked per-slot runs (and
for the `action=None` global-floor call, which never has a timestamp to match
against).
"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"

_SENTINEL = object()

_STUB_MODULE_NAMES = (
    "homeassistant",
    "homeassistant.core",
    "homeassistant.exceptions",
    "homeassistant.helpers",
    "homeassistant.helpers.dispatcher",
    "homeassistant.helpers.event",
    "homeassistant.helpers.storage",
    "homeassistant.helpers.update_coordinator",
    "homeassistant.util",
    "homeassistant.util.dt",
    "power_sync",
    "power_sync.const",
    "power_sync.optimization",
    "power_sync.optimization.battery_optimizer",
    "power_sync.optimization.coordinator",
    "power_sync.optimization.ev_coordinator",
    "power_sync.optimization.executor",
    "power_sync.optimization.load_estimator",
    "power_sync.optimization.schedule_reader",
)

# "Now" for these tests: 2026-07-08 08:30 local (AEST, +10). All slots below
# are anchored relative to this so "today" / "same calendar day" checks land
# where the test expects.
_NOW = datetime(2026, 7, 8, 8, 30, tzinfo=timezone.utc)


def _install_ha_stubs() -> None:
    ha_root = types.ModuleType("homeassistant")
    ha_core = types.ModuleType("homeassistant.core")
    ha_exceptions = types.ModuleType("homeassistant.exceptions")
    ha_helpers = types.ModuleType("homeassistant.helpers")
    ha_dispatcher = types.ModuleType("homeassistant.helpers.dispatcher")
    ha_event = types.ModuleType("homeassistant.helpers.event")
    ha_storage = types.ModuleType("homeassistant.helpers.storage")
    ha_update = types.ModuleType("homeassistant.helpers.update_coordinator")
    ha_util = types.ModuleType("homeassistant.util")
    ha_dt = types.ModuleType("homeassistant.util.dt")

    class _Store:
        def __init__(self, *args, **kwargs) -> None:
            pass

    class _DataUpdateCoordinator:
        def __class_getitem__(cls, item):
            return cls

        def __init__(self, hass, logger, name=None, update_interval=None) -> None:
            self.hass = hass
            self.logger = logger
            self.name = name
            self.update_interval = update_interval
            self.data = None

    ha_core.HomeAssistant = type("HomeAssistant", (), {})
    ha_exceptions.ConfigEntryNotReady = type("ConfigEntryNotReady", (Exception,), {})
    ha_storage.Store = _Store
    ha_update.DataUpdateCoordinator = _DataUpdateCoordinator
    ha_dispatcher.async_dispatcher_send = lambda *args, **kwargs: None
    ha_event.async_track_point_in_utc_time = (
        lambda hass, callback, when: getattr(hass, "scheduled", []).append((callback, when)) or (lambda: None)
    )
    ha_dt.now = lambda *args, **kwargs: _NOW
    ha_dt.utcnow = lambda *args, **kwargs: _NOW
    ha_dt.UTC = timezone.utc
    ha_helpers.storage = ha_storage
    ha_helpers.dispatcher = ha_dispatcher
    ha_helpers.event = ha_event
    ha_helpers.update_coordinator = ha_update
    ha_util.dt = ha_dt
    ha_root.helpers = ha_helpers
    ha_root.util = ha_util

    sys.modules["homeassistant"] = ha_root
    sys.modules["homeassistant.core"] = ha_core
    sys.modules["homeassistant.exceptions"] = ha_exceptions
    sys.modules["homeassistant.helpers"] = ha_helpers
    sys.modules["homeassistant.helpers.dispatcher"] = ha_dispatcher
    sys.modules["homeassistant.helpers.event"] = ha_event
    sys.modules["homeassistant.helpers.storage"] = ha_storage
    sys.modules["homeassistant.helpers.update_coordinator"] = ha_update
    sys.modules["homeassistant.util"] = ha_util
    sys.modules["homeassistant.util.dt"] = ha_dt


def _install_power_sync_stubs() -> None:
    ps_module = types.ModuleType("power_sync")
    ps_module.__path__ = [str(COMPONENT_ROOT)]
    sys.modules["power_sync"] = ps_module

    optimization_module = types.ModuleType("power_sync.optimization")
    optimization_module.__path__ = [str(COMPONENT_ROOT / "optimization")]
    sys.modules["power_sync.optimization"] = optimization_module

    const_module = types.ModuleType("power_sync.const")
    const_module.DOMAIN = "power_sync"
    const_module.TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS = 30
    const_module.CONF_ELECTRICITY_PROVIDER = "electricity_provider"
    const_module.CONF_MONITORING_MODE = "monitoring_mode"
    const_module.CONF_FLOW_POWER_STATE = "flow_power_state"
    const_module.CONF_FLOW_POWER_EXPORT_RATE = "flow_power_export_rate"
    const_module.CONF_FP_TWAP_OVERRIDE = "fp_twap_override"
    const_module.CONF_HARDWARE_BACKUP_RESERVE = "hardware_backup_reserve"
    const_module.CONF_OPTIMIZATION_BACKUP_RESERVE = "optimization_backup_reserve"
    const_module.CONF_OPTIMIZATION_AUTO_APPLY_RESERVE = "optimization_auto_apply_reserve"
    const_module.CONF_OPTIMIZATION_MANUAL_RESERVE = "optimization_manual_reserve"
    const_module.CONF_GENERIC_CHARGER_POWER_ENTITY = "generic_charger_power_entity"
    const_module.CONF_OPTIMIZATION_HORIZON = "optimization_horizon"
    const_module.CONF_OPTIMIZATION_BATTERY_CAPACITY_WH = "battery_capacity_wh"
    const_module.CONF_OPTIMIZATION_ALLOW_GRID_CHARGE = "allow_grid_charge"
    const_module.CONF_OPTIMIZATION_SPREAD_EXPORT_ENABLED = "optimization_spread_export_enabled"
    const_module.CONF_OPTIMIZATION_SPREAD_IMPORT_ENABLED = "optimization_spread_import_enabled"
    const_module.CONF_OPTIMIZATION_DISABLE_IDLE = "optimization_disable_idle"
    const_module.NO_IDLE_MODE_PROVIDERS = frozenset({
        "flow_power",
        "globird",
        "aemo_vpp",
        "other",
        "tou_only",
        "nz",
    })
    const_module.supports_no_idle_mode_provider = (
        lambda provider: str(provider or "") in const_module.NO_IDLE_MODE_PROVIDERS
    )
    const_module.CONF_OPTIMIZATION_MAX_CHARGE_W = "max_charge_w"
    const_module.CONF_OPTIMIZATION_MAX_DISCHARGE_W = "max_discharge_w"
    const_module.CONF_OPTIMIZATION_MAX_GRID_IMPORT_W = "max_grid_import_w"
    const_module.CONF_OPTIMIZATION_MAX_GRID_EXPORT_W = "optimization_max_grid_export_w"
    const_module.CONF_OPTIMIZATION_MAX_GRID_CHARGE_PRICE = "optimization_max_grid_charge_price"
    const_module.CONF_OPTIMIZATION_GRID_CHARGE_SOC_CAP = "optimization_grid_charge_soc_cap"
    const_module.CONF_SIGENERGY_EXPORT_LIMIT_KW = "sigenergy_export_limit_kw"
    const_module.CONF_ALPHAESS_EXPORT_LIMIT_KW = "alphaess_export_limit_kw"
    const_module.CONF_CHARGE_BY_TIME_ENABLED = "charge_by_time_enabled"
    const_module.CONF_CHARGE_BY_TIME_TARGET_TIME = "charge_by_time_target_time"
    const_module.CONF_CHARGE_BY_TIME_TARGET_SOC = "charge_by_time_target_soc"
    const_module.CONF_PROFIT_MAX_TARGET_TIME = "profit_max_target_time"
    const_module.CONF_PROFIT_MAX_TARGET_SOC = "profit_max_target_soc"
    const_module.CONF_PROFIT_MAX_ENABLED = "profit_max_enabled"
    const_module.DEFAULT_CHARGE_BY_TIME_TARGET_TIME = "17:15"
    const_module.DEFAULT_CHARGE_BY_TIME_TARGET_SOC = 1.0
    const_module.DEFAULT_PROFIT_MAX_TARGET_TIME = "17:15"
    const_module.DEFAULT_PROFIT_MAX_TARGET_SOC = 1.0
    const_module.DEFAULT_OPTIMIZATION_INTERVAL = 5
    const_module.FLOW_POWER_BENCHMARK = 1.7
    const_module.FLOW_POWER_EXPORT_RATES = {"NSW1": 0.45}
    const_module.FLOW_POWER_GST = 1.1
    const_module.FLOW_POWER_MARKET_AVG = 8.0
    const_module.CONF_EXPORT_BOOST_ENABLED = "export_boost_enabled"
    const_module.CONF_EXPORT_PRICE_OFFSET = "export_price_offset"
    const_module.CONF_EXPORT_MIN_PRICE = "export_min_price"
    const_module.CONF_EXPORT_BOOST_START = "export_boost_start"
    const_module.CONF_EXPORT_BOOST_END = "export_boost_end"
    const_module.CONF_EXPORT_BOOST_THRESHOLD = "export_boost_threshold"
    const_module.DEFAULT_EXPORT_BOOST_START = "17:00"
    const_module.DEFAULT_EXPORT_BOOST_END = "21:00"
    const_module.DEFAULT_EXPORT_BOOST_THRESHOLD = 0.0
    const_module.CONF_CHIP_MODE_ENABLED = "chip_mode_enabled"
    const_module.CONF_CHIP_MODE_START = "chip_mode_start"
    const_module.CONF_CHIP_MODE_END = "chip_mode_end"
    const_module.CONF_CHIP_MODE_THRESHOLD = "chip_mode_threshold"
    const_module.DEFAULT_CHIP_MODE_START = "22:00"
    const_module.DEFAULT_CHIP_MODE_END = "06:00"
    const_module.DEFAULT_CHIP_MODE_THRESHOLD = 30.0
    const_module.DISCHARGE_DURATIONS = [5, 10, 15, 30, 45, 60, 75, 90, 105, 120, 135, 150, 165, 180, 195, 210, 225, 240]
    const_module.TARGET_EXPORT_POWER_BATTERY_SYSTEMS = {
        "goodwe", "sigenergy", "sungrow", "foxess",
        "alphaess", "solax", "saj_h2", "fronius_reserva", "neovolt",
    }
    const_module.TARGET_CHARGE_POWER_BATTERY_SYSTEMS = {
        "goodwe", "sigenergy", "sungrow", "foxess",
        "alphaess", "solax", "fronius_reserva", "neovolt",
    }
    sys.modules["power_sync.const"] = const_module

    battery_module = types.ModuleType("power_sync.optimization.battery_optimizer")
    battery_module.BatteryOptimizer = type("BatteryOptimizer", (), {})
    battery_module.OptimizerResult = type("OptimizerResult", (), {})
    sys.modules["power_sync.optimization.battery_optimizer"] = battery_module

    schedule_module = types.ModuleType("power_sync.optimization.schedule_reader")

    class _ScheduleAction:
        def __init__(self, timestamp, action, power_w=0.0, soc=None,
                     battery_charge_w=0.0, battery_discharge_w=0.0):
            self.timestamp = timestamp
            self.action = action
            self.power_w = power_w
            self.soc = soc
            self.battery_charge_w = battery_charge_w
            self.battery_discharge_w = battery_discharge_w

    class _OptimizationSchedule:
        def __init__(self, actions, predicted_cost=0, predicted_savings=0,
                     last_updated=None):
            self.actions = actions
            self.predicted_cost = predicted_cost
            self.predicted_savings = predicted_savings
            self.last_updated = last_updated

    schedule_module.ScheduleAction = _ScheduleAction
    schedule_module.OptimizationSchedule = _OptimizationSchedule
    sys.modules["power_sync.optimization.schedule_reader"] = schedule_module

    executor_module = types.ModuleType("power_sync.optimization.executor")
    executor_module.ScheduleExecutor = type("ScheduleExecutor", (), {})
    executor_module.ExecutionStatus = type("ExecutionStatus", (), {})
    executor_module.BatteryAction = type("BatteryAction", (), {})
    sys.modules["power_sync.optimization.executor"] = executor_module

    load_module = types.ModuleType("power_sync.optimization.load_estimator")
    load_module.LoadEstimator = type("LoadEstimator", (), {})
    load_module.SolcastForecaster = type("SolcastForecaster", (), {})
    sys.modules["power_sync.optimization.load_estimator"] = load_module

    ev_module = types.ModuleType("power_sync.optimization.ev_coordinator")
    ev_module.EVCoordinator = type("EVCoordinator", (), {})
    ev_module.EVConfig = type("EVConfig", (), {})
    ev_module.EVChargingMode = type("EVChargingMode", (), {})
    sys.modules["power_sync.optimization.ev_coordinator"] = ev_module


@pytest.fixture()
def opt_module():
    saved_modules = {
        name: sys.modules.get(name, _SENTINEL)
        for name in _STUB_MODULE_NAMES
    }
    for name in _STUB_MODULE_NAMES:
        sys.modules.pop(name, None)

    _install_ha_stubs()
    _install_power_sync_stubs()
    import importlib

    module = importlib.import_module("power_sync.optimization.coordinator")
    try:
        yield module
    finally:
        for name in _STUB_MODULE_NAMES:
            if saved_modules[name] is _SENTINEL:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved_modules[name]


def _coordinator(opt_module, backup_reserve: float = 0.10):
    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    coordinator._entry = SimpleNamespace(
        options={"electricity_provider": "amber"}, data={}
    )
    coordinator._config = opt_module.OptimizationConfig(
        interval_minutes=5,
        horizon_hours=24,
        backup_reserve=backup_reserve,
    )
    coordinator._optimizer = None
    coordinator.energy_coordinator = None
    coordinator._active_export_reserve_floor_slots = None
    coordinator._active_export_reserve_floor_timestamps = None
    coordinator._last_optimizer_result = None
    return coordinator


def _slot_timestamps(count: int, *, start_hour: int, start_minute: int = 0):
    base = _NOW.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
    from datetime import timedelta

    return [base + timedelta(minutes=5 * idx) for idx in range(count)]


def test_matched_per_slot_floor_wins_over_cross_run_scalar(opt_module):
    """Morning export run's own (lower) floor must not be overridden by the
    evening run's (higher) scalar recommendation for the same day."""
    coordinator = _coordinator(opt_module, backup_reserve=0.10)
    coordinator._auto_apply_reserve_enabled = True

    morning_timestamps = _slot_timestamps(3, start_hour=9)
    evening_timestamps = _slot_timestamps(3, start_hour=18)
    all_timestamps = morning_timestamps + evening_timestamps
    # Morning run is scoped to its own (lower) 30% floor; evening slots aren't
    # part of the tracked per-slot array in this scenario (only morning is).
    all_floors = [0.30, 0.30, 0.30, 0.0, 0.0, 0.0]

    coordinator._active_export_reserve_floor_timestamps = all_timestamps
    coordinator._active_export_reserve_floor_slots = all_floors

    # The scalar recommendation reflects the day's highest-floor run: the
    # evening window at 60%, with its bridge run starting today.
    coordinator._last_optimizer_result = SimpleNamespace(
        reserve_recommendation={
            "home_load_export_floor_percent": 60,
            "home_load_bridge_after_export_start": evening_timestamps[0].isoformat(),
        }
    )

    morning_action = SimpleNamespace(
        action="export", timestamp=morning_timestamps[0]
    )

    floor = coordinator._force_discharge_reserve_floor(morning_action)

    # Must be the morning run's own per-slot floor (30%), NOT the evening
    # scalar (60%).
    assert floor == pytest.approx(0.30)


def test_action_outside_tracked_slots_still_gets_scalar_base(opt_module):
    """An action whose timestamp isn't in the tracked per-slot arrays falls
    back to the scalar recommendation, as before."""
    coordinator = _coordinator(opt_module, backup_reserve=0.10)
    coordinator._auto_apply_reserve_enabled = True

    morning_timestamps = _slot_timestamps(3, start_hour=9)
    evening_timestamps = _slot_timestamps(3, start_hour=18)

    coordinator._active_export_reserve_floor_timestamps = morning_timestamps
    coordinator._active_export_reserve_floor_slots = [0.30, 0.30, 0.30]

    coordinator._last_optimizer_result = SimpleNamespace(
        reserve_recommendation={
            "home_load_export_floor_percent": 60,
            "home_load_bridge_after_export_start": evening_timestamps[0].isoformat(),
        }
    )

    # This action's timestamp (the evening run) was never part of the tracked
    # per-slot arrays, so it must fall back to the scalar base.
    evening_action = SimpleNamespace(
        action="export", timestamp=evening_timestamps[0]
    )

    floor = coordinator._force_discharge_reserve_floor(evening_action)

    assert floor == pytest.approx(0.60)


def test_no_action_call_still_uses_scalar_as_global_base(opt_module):
    """The `action=None` global-floor call has no timestamp to match against
    per-slot data, so the scalar recommendation must still apply."""
    coordinator = _coordinator(opt_module, backup_reserve=0.10)
    coordinator._auto_apply_reserve_enabled = True

    morning_timestamps = _slot_timestamps(3, start_hour=9)
    coordinator._active_export_reserve_floor_timestamps = morning_timestamps
    coordinator._active_export_reserve_floor_slots = [0.30, 0.30, 0.30]

    coordinator._last_optimizer_result = SimpleNamespace(
        reserve_recommendation={
            "home_load_export_floor_percent": 60,
            "home_load_bridge_after_export_start": _NOW.isoformat(),
        }
    )

    floor = coordinator._force_discharge_reserve_floor()

    assert floor == pytest.approx(0.60)


def test_matched_zero_per_slot_floor_still_wins_over_scalar(opt_module):
    """A matched slot with an explicit 0.0 (no elevated floor needed for that
    run) must not be topped up to the unrelated evening scalar either."""
    coordinator = _coordinator(opt_module, backup_reserve=0.10)
    coordinator._auto_apply_reserve_enabled = True

    morning_timestamps = _slot_timestamps(3, start_hour=9)
    evening_timestamps = _slot_timestamps(3, start_hour=18)
    all_timestamps = morning_timestamps + evening_timestamps
    all_floors = [0.0, 0.0, 0.0, 0.60, 0.60, 0.60]

    coordinator._active_export_reserve_floor_timestamps = all_timestamps
    coordinator._active_export_reserve_floor_slots = all_floors

    coordinator._last_optimizer_result = SimpleNamespace(
        reserve_recommendation={
            "home_load_export_floor_percent": 60,
            "home_load_bridge_after_export_start": evening_timestamps[0].isoformat(),
        }
    )

    morning_action = SimpleNamespace(
        action="export", timestamp=morning_timestamps[0]
    )

    floor = coordinator._force_discharge_reserve_floor(morning_action)

    # Matched slot floor is 0.0 -> result is just the configured backup
    # reserve (10%), not the evening scalar (60%).
    assert floor == pytest.approx(0.10)


def test_bridge_scan_stops_at_next_export_run_hd6(opt_module):
    """HD-6: an export window split by one sub-100 W dip slot becomes two
    runs. Run 1's bridge-to-recharge scan must stop at the start of run 2
    (another real export run), not fold run 2's own home load into run 1's
    floor and keep scanning past it to whatever charge opportunity follows.
    """
    coordinator = _coordinator(opt_module, backup_reserve=0.0)

    timestamps = _slot_timestamps(6, start_hour=9)
    actions = [
        opt_module.ScheduleAction(
            timestamp=timestamps[0], action="export", battery_discharge_w=1000.0
        ),
        opt_module.ScheduleAction(
            timestamp=timestamps[1], action="export", battery_discharge_w=1000.0
        ),
        # Sub-100W dip: ends run 1.
        opt_module.ScheduleAction(
            timestamp=timestamps[2], action="export", battery_discharge_w=50.0
        ),
        # Run 2 starts here -- its own home load must NOT be bridged into
        # run 1's floor.
        opt_module.ScheduleAction(
            timestamp=timestamps[3], action="export", battery_discharge_w=1000.0
        ),
        opt_module.ScheduleAction(
            timestamp=timestamps[4], action="export", battery_discharge_w=1000.0
        ),
        opt_module.ScheduleAction(
            timestamp=timestamps[5], action="charge", battery_charge_w=2000.0
        ),
    ]
    schedule = opt_module.OptimizationSchedule(actions=actions)

    solar_forecast = [0.0] * 6
    load_forecast = [0.0, 0.0, 3.0, 3.0, 3.0, 0.0]

    floors, meta = coordinator._post_processed_export_reserve_floor_slots(
        schedule, solar_forecast, load_forecast
    )

    assert meta != {}
    # Only the dip slot's (idx 2) home load should be bridged -- 3.0 kW for
    # one 5-minute slot = 0.25 kWh. The buggy version also folds in run 2's
    # two slots (idx 3, 4) and scans through to the charge action at idx 5,
    # producing 0.75 kWh and a misleading "scheduled_grid_charge" reason.
    assert meta["home_load_bridge_kwh"] == pytest.approx(0.25, abs=0.001)
    assert meta["home_load_bridge_next_charge_reason"] == "no_charge_in_horizon"
