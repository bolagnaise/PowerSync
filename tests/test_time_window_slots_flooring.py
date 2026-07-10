"""Regression test for HD-8: _time_window_slots must floor ``dt_util.now()``
to the same interval boundary the price/slot grid uses before computing each
slot's timestamp. An unfloored ``now`` can offset the Happy Hour / Export
Boost window mask by up to one interval at the window's edges.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    # Default "now" — overridden per-test via monkeypatching opt_module.dt_util.now.
    ha_dt.now = lambda *args, **kwargs: datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    ha_dt.utcnow = lambda *args, **kwargs: datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
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

    @dataclass
    class _ScheduleAction:
        timestamp: datetime
        action: str
        power_w: float
        soc: float | None = None
        battery_charge_w: float = 0.0
        battery_discharge_w: float = 0.0

    @dataclass
    class _OptimizationSchedule:
        actions: list
        predicted_cost: float
        predicted_savings: float
        last_updated: datetime | None = None

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
    import importlib

    saved_modules = {
        name: sys.modules.get(name, _SENTINEL)
        for name in _STUB_MODULE_NAMES
    }
    for name in _STUB_MODULE_NAMES:
        sys.modules.pop(name, None)

    _install_ha_stubs()
    _install_power_sync_stubs()
    module = importlib.import_module("power_sync.optimization.coordinator")
    try:
        yield module
    finally:
        for name in _STUB_MODULE_NAMES:
            if saved_modules[name] is _SENTINEL:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved_modules[name]


def _coordinator(opt_module, provider: str, **options):
    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    base_options = {"electricity_provider": provider}
    base_options.update(options)
    coordinator._entry = SimpleNamespace(options=base_options, data={})
    coordinator._config = opt_module.OptimizationConfig(
        interval_minutes=5,
        horizon_hours=24,
    )
    coordinator._saving_session_coordinator = None
    coordinator._last_export_boost_allowed_slots = []
    coordinator._last_price_timestamps = None
    coordinator._optimizer = None
    coordinator.energy_coordinator = None
    return coordinator


def test_time_window_slots_floors_unaligned_now_to_interval_grid(opt_module, monkeypatch):
    """With interval=5 and an unfloored 'now' sitting 2 minutes past the grid
    boundary (17:32 rather than 17:30), slot 0's timestamp must be computed
    from the floored origin (17:30) — matching how the LP stamps its own
    slot-0 timestamp — not the raw unfloored clock read.

    A 1-minute window [17:30, 17:31) straddles exactly the 2-minute gap
    between the floored and unfloored origins, so slot 0 flips membership
    depending on which origin is used: the floored origin (17:30) falls
    inside the window, the unfloored one (17:32) falls outside it.
    """
    unaligned_now = datetime(2026, 5, 3, 17, 32, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(opt_module.dt_util, "now", lambda *a, **k: unaligned_now)

    coordinator = _coordinator(opt_module, "octopus")
    coordinator._config.interval_minutes = 5

    result = coordinator._time_window_slots(1, "17:30", "17:31")

    assert result == [True], (
        "slot 0 should be computed from the interval-floored origin "
        "(17:30, inside the window), not the raw unfloored now (17:32, "
        "outside the window)"
    )


def test_time_window_slots_matches_lp_slot_zero_origin(opt_module, monkeypatch):
    """The floored origin used here must be identical to the flooring
    battery_optimizer.py applies when it stamps schedule slot 0
    (``now.minute // interval * interval``), so Happy Hour / Export Boost
    masks stay aligned with the price grid across the whole horizon.
    """
    unaligned_now = datetime(2026, 5, 3, 17, 33, 47, 123456, tzinfo=timezone.utc)
    monkeypatch.setattr(opt_module.dt_util, "now", lambda *a, **k: unaligned_now)

    interval = 5
    expected_origin = unaligned_now.replace(
        minute=(unaligned_now.minute // interval) * interval,
        second=0,
        microsecond=0,
    )

    coordinator = _coordinator(opt_module, "octopus")
    coordinator._config.interval_minutes = interval

    n = 6
    result = coordinator._time_window_slots(n, "00:00", "23:59")
    for t in range(n):
        slot_ts = expected_origin + timedelta(minutes=t * interval)
        minutes_of_day = slot_ts.hour * 60 + slot_ts.minute
        expected_in_window = 0 <= minutes_of_day < (23 * 60 + 59)
        assert result[t] == expected_in_window


def test_current_import_price_for_action_uses_price_timestamps_fallback_for_dynamic_provider(
    opt_module, monkeypatch
):
    """HD-20 regression: _last_price_timestamps is only ever stamped on the
    static TOU-forecast path, so dynamic-price providers (Amber, ZeroHero,
    etc.) leave it None. _current_import_price_for_action must fall back to
    the existing _price_timestamps() synthetic interval grid — the same one
    _zerohero_window_slots/_zerocharge_window_slots already rely on — instead
    of unconditionally returning None whenever the attribute is unset.
    """
    aligned_now = datetime(2026, 5, 3, 17, 30, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(opt_module.dt_util, "now", lambda *a, **k: aligned_now)

    coordinator = _coordinator(opt_module, "amber")
    coordinator._config.interval_minutes = 5
    coordinator._last_price_timestamps = None  # dynamic provider: never stamped

    prices = [10.0, 20.0, 30.0]
    action = opt_module.ScheduleAction(
        timestamp=datetime(2026, 5, 3, 17, 36, 0, tzinfo=timezone.utc),
        action="charge",
        power_w=1000.0,
    )

    result = coordinator._current_import_price_for_action(prices, action)

    assert result == 20.0
