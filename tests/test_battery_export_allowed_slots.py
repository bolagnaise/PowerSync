"""Regression tests for provider-scoped battery export permissions."""

from __future__ import annotations

import asyncio
import importlib
import logging
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
    const_module.CONF_OPTIMIZATION_ENABLED = "optimization_enabled"
    const_module.CONF_OPTIMIZATION_COST_FUNCTION = "optimization_cost_function"
    const_module.CONF_OPTIMIZATION_BACKUP_RESERVE = "optimization_backup_reserve"
    const_module.CONF_OPTIMIZATION_AUTO_APPLY_RESERVE = "optimization_auto_apply_reserve"
    const_module.CONF_OPTIMIZATION_MANUAL_RESERVE = "optimization_manual_reserve"
    const_module.CONF_GENERIC_CHARGER_POWER_ENTITY = "generic_charger_power_entity"
    const_module.CONF_COVAU_PLAN_SNAPSHOT = "covau_plan_snapshot"
    const_module.CONF_COVAU_IMPORT_ENERGY_ENTITY = "covau_import_energy_entity"
    const_module.CONF_COVAU_EXPORT_ENERGY_ENTITY = "covau_export_energy_entity"
    const_module.CONF_OPTIMIZATION_HORIZON = "optimization_horizon"
    const_module.CONF_OPTIMIZATION_BATTERY_CAPACITY_WH = "battery_capacity_wh"
    const_module.CONF_OPTIMIZATION_ALLOW_GRID_CHARGE = "allow_grid_charge"
    const_module.CONF_OPTIMIZATION_SPREAD_EXPORT_ENABLED = "optimization_spread_export_enabled"
    const_module.CONF_OPTIMIZATION_SPREAD_IMPORT_ENABLED = "optimization_spread_import_enabled"
    const_module.CONF_OPTIMIZATION_DISABLE_IDLE = "optimization_disable_idle"
    const_module.NO_IDLE_MODE_PROVIDERS = frozenset({
        "flow_power",
        "globird",
        "covau",
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


def _coordinator(
    opt_module,
    provider: str,
    profit_max: bool = False,
    charge_by_time: bool = False,
    **options,
):
    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    coordinator.battery_system = "tesla"
    base_options = {"electricity_provider": provider}
    base_options.update(options)
    coordinator._entry = SimpleNamespace(options=base_options, data={})
    coordinator.battery_system = "tesla"
    coordinator._config = opt_module.OptimizationConfig(
        interval_minutes=5,
        horizon_hours=24,
        profit_max_enabled=profit_max,
        charge_by_time_enabled=charge_by_time,
        charge_by_time_target_time=base_options.get(
            "charge_by_time_target_time",
            base_options.get("profit_max_target_time", "17:15"),
        ),
        charge_by_time_target_soc=base_options.get(
            "charge_by_time_target_soc",
            base_options.get("profit_max_target_soc", 1.0),
        ),
    )
    coordinator._saving_session_coordinator = None
    coordinator._last_export_boost_allowed_slots = []
    coordinator._last_price_timestamps = None
    coordinator._last_zerohero_bonus_cap_kwh = None
    coordinator._last_zerohero_bonus_prices = None
    coordinator._last_zerocharge_bonus_cap_kwh = None
    coordinator._last_zerocharge_bonus_prices = None
    coordinator._covau_ledger = None
    coordinator._covau_snapshot_cache = None
    coordinator._covau_snapshot_hash = None
    coordinator._last_covau_config_warning = None
    coordinator._pending_covau_settlement = {"import": 0.0, "export": 0.0}
    coordinator._actual_zerohero_import_kwh_today = 0.0
    coordinator._actual_zerohero_export_kwh_today = 0.0
    coordinator._actual_zerohero_bonus_export_kwh_today = 0.0
    coordinator._actual_zerohero_base_export_earnings_today = 0.0
    coordinator._actual_zerohero_bonus_export_earnings_today = 0.0
    coordinator._actual_zerohero_credit_value_today = 0.0
    coordinator._actual_zerocharge_import_kwh_today = 0.0
    coordinator._actual_zerocharge_credit_value_today = 0.0
    coordinator._pre_idle_backup_reserve = None
    coordinator._idle_hold_reserve = None
    coordinator._optimizer = None
    coordinator.energy_coordinator = None
    return coordinator


def test_initial_optimization_task_handle_clears_after_startup_pass(opt_module):
    async def _run():
        coordinator = _coordinator(opt_module, "globird")
        coordinator.hass = SimpleNamespace(is_running=True)
        coordinator._enabled = True
        coordinator._initial_opt_task = asyncio.current_task()
        calls = []

        async def _run_optimization():
            calls.append(True)

        coordinator._run_optimization = _run_optimization

        await coordinator._run_initial_optimization_after_startup_delay()

        assert calls == [True]
        assert coordinator._initial_opt_task is None

    asyncio.run(_run())


def test_tesla_force_charge_yields_to_live_solar(opt_module):
    coordinator = _coordinator(opt_module, "amber")
    coordinator.battery_system = "tesla"
    coordinator._last_import_prices = [0.12]
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "solar_power": 3.8,
            "load_power": 0.7,
            "battery_power": 0.0,
            "grid_power": 0.0,
            "battery_level": 46.0,
        }
    )

    assert coordinator._tesla_force_charge_should_yield_to_live_solar() is True


def test_tesla_force_charge_allowed_during_free_import(opt_module):
    coordinator = _coordinator(opt_module, "globird")
    coordinator.battery_system = "tesla"
    coordinator._last_import_prices = [0.0]
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "solar_power": 3.8,
            "load_power": 0.7,
            "battery_power": 0.0,
            "grid_power": 0.0,
            "battery_level": 46.0,
        }
    )

    assert coordinator._tesla_force_charge_should_yield_to_live_solar() is False


def test_tesla_force_charge_allowed_when_action_slot_is_free(opt_module):
    coordinator = _coordinator(opt_module, "globird")
    coordinator.battery_system = "tesla"
    start = datetime(2026, 6, 29, 10, 50, tzinfo=timezone(timedelta(hours=10)))
    coordinator._last_display_import_prices = [0.55, 0.55, 0.0]
    coordinator._last_price_timestamps = [
        start,
        start + timedelta(minutes=5),
        start + timedelta(minutes=10),
    ]
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "solar_power": 6.3,
            "load_power": 2.7,
            "battery_power": -3.6,
            "grid_power": 0.0,
            "battery_level": 14.0,
        }
    )

    action = SimpleNamespace(
        action="charge",
        timestamp=start + timedelta(minutes=10),
    )

    assert coordinator._tesla_force_charge_should_yield_to_live_solar() is True
    assert coordinator._tesla_force_charge_should_yield_to_live_solar(action) is False


def test_forecast_data_exposes_battery_discharge_split_for_lp_chart(opt_module):
    coordinator = _coordinator(opt_module, "amber")
    coordinator._solar_nowcast_derate = 1.0
    coordinator._last_solar_nowcast_ratio = None
    coordinator._last_solar_forecast = []
    coordinator._last_load_forecast = []
    coordinator._last_planned_ev_load_forecast_w = None
    coordinator._last_display_import_prices = []
    coordinator._last_import_prices = []
    coordinator._last_display_export_prices = []
    coordinator._last_export_prices = []
    start = datetime(2026, 7, 12, 17, 30, tzinfo=timezone(timedelta(hours=10)))
    actions = [
        opt_module.ScheduleAction(
            timestamp=start,
            action="export",
            power_w=5500,
            soc=0.80,
            battery_discharge_w=7500,
        )
    ]

    coordinator._current_schedule = SimpleNamespace(
        actions=actions,
        battery_consume_w=[2000],
        battery_export_w=[5500],
    )

    data = coordinator.get_forecast_data()

    assert data["battery_discharge_forecast"] == [7.5]
    assert data["battery_home_consumption_forecast"] == [2.0]
    assert data["battery_export_forecast"] == [5.5]


def test_tesla_force_charge_does_not_yield_when_solar_misses_charge_target(
    opt_module,
    monkeypatch,
):
    coordinator = _coordinator(opt_module, "amber")
    coordinator.battery_system = "tesla"
    coordinator._config.battery_capacity_wh = 13500
    start = datetime(2026, 7, 9, 13, 50, tzinfo=timezone(timedelta(hours=10)))
    monkeypatch.setattr(
        opt_module.dt_util,
        "now",
        lambda: start + timedelta(minutes=1),
    )
    actions = [
        opt_module.ScheduleAction(
            timestamp=start + timedelta(minutes=idx * 5),
            action="charge",
            power_w=10000,
            soc=soc,
            battery_charge_w=10000,
        )
        for idx, soc in enumerate(
            (0.58, 0.61, 0.65, 0.70, 0.75, 0.80, 0.86, 0.93)
        )
    ]
    actions.append(
        opt_module.ScheduleAction(
            timestamp=start + timedelta(minutes=40),
            action="self_consumption",
            power_w=0,
            soc=0.93,
        )
    )
    coordinator._current_schedule = opt_module.OptimizationSchedule(
        actions=actions,
        predicted_cost=0,
        predicted_savings=0,
        last_updated=start,
    )
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "solar_power": 5.9,
            "load_power": 0.4,
            "battery_power": -3.5,
            "grid_power": 0.0,
            "battery_level": 54.0,
        }
    )

    assert (
        coordinator._tesla_force_charge_should_yield_to_live_solar(actions[0])
        is False
    )


def test_tesla_force_charge_yields_when_solar_can_reach_charge_target(
    opt_module,
    monkeypatch,
):
    coordinator = _coordinator(opt_module, "amber")
    coordinator.battery_system = "tesla"
    coordinator._config.battery_capacity_wh = 13500
    start = datetime(2026, 7, 9, 13, 50, tzinfo=timezone(timedelta(hours=10)))
    monkeypatch.setattr(
        opt_module.dt_util,
        "now",
        lambda: start + timedelta(minutes=1),
    )
    actions = [
        opt_module.ScheduleAction(
            timestamp=start + timedelta(minutes=idx * 5),
            action="charge",
            power_w=5000,
            soc=soc,
            battery_charge_w=5000,
        )
        for idx, soc in enumerate((0.56, 0.58, 0.60, 0.62, 0.64, 0.66))
    ]
    actions.append(
        opt_module.ScheduleAction(
            timestamp=start + timedelta(minutes=30),
            action="self_consumption",
            power_w=0,
            soc=0.66,
        )
    )
    coordinator._current_schedule = opt_module.OptimizationSchedule(
        actions=actions,
        predicted_cost=0,
        predicted_savings=0,
        last_updated=start,
    )
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "solar_power": 5.9,
            "load_power": 0.4,
            "battery_power": -3.5,
            "grid_power": 0.0,
            "battery_level": 54.0,
        }
    )

    assert (
        coordinator._tesla_force_charge_should_yield_to_live_solar(actions[0])
        is True
    )


def test_tesla_force_charge_allowed_without_live_solar(opt_module):
    coordinator = _coordinator(opt_module, "amber")
    coordinator.battery_system = "tesla"
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "solar_power": 0.0,
            "battery_level": 46.0,
        }
    )

    assert coordinator._tesla_force_charge_should_yield_to_live_solar() is False


def test_tesla_force_charge_allowed_when_site_load_absorbs_live_solar(opt_module):
    coordinator = _coordinator(opt_module, "globird")
    coordinator.battery_system = "tesla"
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "solar_power": 3.1,
            "load_power": 11.8,
            "battery_power": 8.7,
            "grid_power": 0.0,
            "battery_level": 24.0,
        }
    )

    assert coordinator._tesla_force_charge_should_yield_to_live_solar() is False


class _FakeMinSocCoordinator:
    def __init__(self) -> None:
        self.min_soc_calls = []

    def set_min_soc_pct(self, min_soc_pct: float) -> None:
        self.min_soc_calls.append(min_soc_pct)


class _FakeTeslaBattery:
    def __init__(self, reserve: int) -> None:
        self.reserve = reserve

    async def get_backup_reserve(self) -> int:
        return self.reserve


def test_update_config_propagates_backup_reserve_to_software_floor(opt_module):
    coordinator = _coordinator(opt_module, "octopus")
    energy = _FakeMinSocCoordinator()
    coordinator.energy_coordinator = energy

    coordinator.update_config(backup_reserve=0.35)

    assert energy.min_soc_calls == [35]


def test_update_config_forces_fixed_five_minute_interval(opt_module):
    coordinator = _coordinator(opt_module, "amber")

    coordinator.update_config(interval_minutes=30)

    assert coordinator._config.interval_minutes == 5


def test_update_config_propagates_horizon_hours_to_optimizer(opt_module):
    coordinator = _coordinator(opt_module, "amber")
    update_calls = []
    coordinator._optimizer = SimpleNamespace(
        update_config=lambda **kwargs: update_calls.append(kwargs),
        max_grid_export_w=None,
        terminal_weight=0,
    )

    coordinator.update_config(horizon_hours=12)

    assert coordinator._config.horizon_hours == 12
    assert update_calls[-1]["horizon_hours"] == 12


def test_startup_restore_target_prefers_hardware_reserve_config(opt_module):
    coordinator = _coordinator(
        opt_module,
        "amber",
        hardware_backup_reserve=0.2,
        _user_backup_reserve=45,
        optimization_backup_reserve=30,
    )

    assert coordinator._configured_startup_backup_reserve() == (
        20,
        "hardware backup reserve config",
    )


def test_startup_restore_target_prefers_data_hardware_reserve_over_stale_options(opt_module):
    coordinator = _coordinator(
        opt_module,
        "amber",
        hardware_backup_reserve=0.45,
        _user_backup_reserve=45,
        optimization_backup_reserve=30,
    )
    coordinator._entry.data = {"hardware_backup_reserve": 0.2}

    assert coordinator._configured_startup_backup_reserve() == (
        20,
        "hardware backup reserve config",
    )


def test_startup_restore_target_uses_optimizer_floor_when_no_user_reserve(opt_module):
    coordinator = _coordinator(
        opt_module,
        "amber",
        optimization_backup_reserve=20,
    )

    assert coordinator._configured_startup_backup_reserve() == (
        20,
        "optimizer floor config",
    )


def test_startup_restore_target_does_not_treat_live_idle_reserve_as_persisted(opt_module):
    coordinator = _coordinator(opt_module, "amber")
    coordinator._config.backup_reserve = 0.20

    assert coordinator._configured_startup_backup_reserve() == (
        20,
        "optimizer floor",
    )


def test_tesla_startup_ignores_polluted_zero_user_reserve(opt_module):
    coordinator = _coordinator(
        opt_module,
        "amber",
        _user_backup_reserve=0,
        optimization_backup_reserve=60,
    )
    coordinator.battery_system = "tesla"

    assert coordinator._configured_startup_backup_reserve() == (
        60,
        "optimizer floor config",
    )


def test_tesla_startup_replaces_stale_persisted_reserve_with_lower_live_reserve(opt_module):
    coordinator = _coordinator(
        opt_module,
        "amber",
        _user_backup_reserve=52,
    )
    coordinator.battery_system = "tesla"
    coordinator.entry_id = "entry-1"
    updates = []

    class _ConfigEntries:
        def async_update_entry(self, entry, **kwargs):
            updates.append(kwargs)
            if "options" in kwargs:
                entry.options = kwargs["options"]

    coordinator.hass = SimpleNamespace(
        data={"power_sync": {"entry-1": {}}},
        config_entries=_ConfigEntries(),
    )

    assert asyncio.run(
        coordinator._resolve_startup_backup_reserve(
            _FakeTeslaBattery(30),
            52,
            "persisted user backup reserve",
        )
    ) == (30, "live Tesla backup reserve")
    assert coordinator._entry.options["_user_backup_reserve"] == 30
    assert updates[-1]["options"]["_user_backup_reserve"] == 30


def test_tesla_startup_does_not_replace_persisted_reserve_with_live_zero(opt_module):
    coordinator = _coordinator(
        opt_module,
        "amber",
        _user_backup_reserve=30,
    )
    coordinator.battery_system = "tesla"

    assert asyncio.run(
        coordinator._resolve_startup_backup_reserve(
            _FakeTeslaBattery(0),
            30,
            "persisted user backup reserve",
        )
    ) == (30, "persisted user backup reserve")


def test_tesla_startup_keeps_persisted_reserve_when_live_reserve_is_higher(opt_module):
    coordinator = _coordinator(
        opt_module,
        "amber",
        _user_backup_reserve=30,
    )
    coordinator.battery_system = "tesla"

    assert asyncio.run(
        coordinator._resolve_startup_backup_reserve(
            _FakeTeslaBattery(80),
            30,
            "persisted user backup reserve",
        )
    ) == (30, "persisted user backup reserve")


def test_set_settings_persists_hardware_reserve_to_data_and_options(opt_module):
    coordinator = _coordinator(
        opt_module,
        "amber",
        hardware_backup_reserve=0.45,
        _user_backup_reserve=52,
    )
    coordinator.entry_id = "entry-1"
    coordinator._startup_backup_reserve = 45
    coordinator._optimizer = SimpleNamespace(update_hardware_reserve=lambda reserve: None)

    updates = []

    class _ConfigEntries:
        def async_update_entry(self, entry, **kwargs):
            updates.append(kwargs)
            if "data" in kwargs:
                entry.data = kwargs["data"]
            if "options" in kwargs:
                entry.options = kwargs["options"]

    coordinator.hass = SimpleNamespace(
        data={"power_sync": {"entry-1": {}}},
        config_entries=_ConfigEntries(),
    )

    result = asyncio.run(coordinator.set_settings({"hardware_backup_reserve": 20}))

    assert result["success"] is True
    assert coordinator._startup_backup_reserve == 20
    assert coordinator._entry.data["hardware_backup_reserve"] == 0.2
    assert coordinator._entry.options["hardware_backup_reserve"] == 0.2
    assert "_user_backup_reserve" not in coordinator._entry.options
    assert updates[-1]["data"]["hardware_backup_reserve"] == 0.2
    assert updates[-1]["options"]["hardware_backup_reserve"] == 0.2
    assert "_user_backup_reserve" not in updates[-1]["options"]


def test_set_settings_enabled_noop_does_not_leave_stale_skip_reload_flag(opt_module):
    """OB-21: a no-op 'enabled' push (e.g. periodic API sync) must not set
    _skip_reload, or a later genuine structural reload gets silently swallowed
    when the update listener pops this stale flag."""
    coordinator = _coordinator(opt_module, "amber", optimization_enabled=True)
    coordinator.entry_id = "entry-1"
    coordinator._enabled = True

    class _ConfigEntries:
        def async_update_entry(self, entry, **kwargs):
            if "options" in kwargs:
                entry.options = kwargs["options"]

    entry_data: dict = {}
    coordinator.hass = SimpleNamespace(
        data={"power_sync": {"entry-1": entry_data}},
        config_entries=_ConfigEntries(),
    )

    result = asyncio.run(coordinator.set_settings({"enabled": True}))

    assert result["success"] is True
    assert entry_data.get("_skip_reload") is not True


def test_set_settings_cost_function_noop_does_not_leave_stale_skip_reload_flag(opt_module):
    """OB-21: resending the same cost_function value must not set _skip_reload."""
    coordinator = _coordinator(opt_module, "amber")
    coordinator.entry_id = "entry-1"
    coordinator._entry.data = {"optimization_cost_function": "cost"}

    class _ConfigEntries:
        def async_update_entry(self, entry, **kwargs):
            if "data" in kwargs:
                entry.data = kwargs["data"]

    entry_data: dict = {}
    coordinator.hass = SimpleNamespace(
        data={"power_sync": {"entry-1": entry_data}},
        config_entries=_ConfigEntries(),
    )

    result = asyncio.run(coordinator.set_settings({"cost_function": "cost"}))

    assert result["success"] is True
    assert entry_data.get("_skip_reload") is not True


def test_set_settings_persists_optimizer_reserve_to_data_and_options(opt_module):
    coordinator = _coordinator(
        opt_module,
        "amber",
        optimization_backup_reserve=45,
    )
    coordinator.entry_id = "entry-1"
    coordinator._entry.data = {"optimization_backup_reserve": 0.45}

    updates = []

    class _ConfigEntries:
        def async_update_entry(self, entry, **kwargs):
            updates.append(kwargs)
            if "data" in kwargs:
                entry.data = kwargs["data"]
            if "options" in kwargs:
                entry.options = kwargs["options"]

    coordinator.hass = SimpleNamespace(
        data={"power_sync": {"entry-1": {}}},
        config_entries=_ConfigEntries(),
    )

    result = asyncio.run(coordinator.set_settings({"backup_reserve": 20}))

    assert result["success"] is True
    assert coordinator._config.backup_reserve == 0.2
    assert coordinator._entry.data["optimization_backup_reserve"] == 0.2
    assert coordinator._entry.options["optimization_backup_reserve"] == 0.2
    assert updates[-1]["data"]["optimization_backup_reserve"] == 0.2
    assert updates[-1]["options"]["optimization_backup_reserve"] == 0.2


def test_set_settings_persists_grid_export_cap_zero_and_clear(opt_module):
    coordinator = _coordinator(opt_module, "amber")
    coordinator.entry_id = "entry-1"

    updates = []

    class _ConfigEntries:
        def async_update_entry(self, entry, **kwargs):
            updates.append(kwargs)
            if "data" in kwargs:
                entry.data = kwargs["data"]
            if "options" in kwargs:
                entry.options = kwargs["options"]

    coordinator.hass = SimpleNamespace(
        data={"power_sync": {"entry-1": {}}},
        config_entries=_ConfigEntries(),
    )

    result = asyncio.run(coordinator.set_settings({"max_grid_export_w": 0}))

    assert result["success"] is True
    assert coordinator._config.max_grid_export_w == 0
    assert coordinator._entry.options["optimization_max_grid_export_w"] == 0
    assert updates[-1]["options"]["optimization_max_grid_export_w"] == 0

    result = asyncio.run(coordinator.set_settings({"max_grid_export_w": None}))

    assert result["success"] is True
    assert coordinator._config.max_grid_export_w is None
    assert "optimization_max_grid_export_w" not in coordinator._entry.options
    assert "optimization_max_grid_export_w" not in coordinator._entry.data


def test_auto_apply_reserve_enable_snapshots_current_optimizer_reserve(opt_module):
    coordinator = _coordinator(
        opt_module,
        "amber",
        optimization_backup_reserve=0.35,
    )
    coordinator.entry_id = "entry-1"
    coordinator._config.backup_reserve = 0.35
    coordinator._auto_apply_reserve_enabled = False
    coordinator._manual_backup_reserve = None
    coordinator._enabled = False

    updates = []

    class _ConfigEntries:
        def async_update_entry(self, entry, **kwargs):
            updates.append(kwargs)
            if "data" in kwargs:
                entry.data = kwargs["data"]
            if "options" in kwargs:
                entry.options = kwargs["options"]

    coordinator.hass = SimpleNamespace(
        data={"power_sync": {"entry-1": {}}},
        config_entries=_ConfigEntries(),
    )

    asyncio.run(coordinator.set_auto_apply_reserve_enabled(True))

    assert coordinator.auto_apply_reserve_enabled is True
    assert coordinator.manual_backup_reserve == 0.35
    assert coordinator._entry.data["optimization_auto_apply_reserve"] is True
    assert coordinator._entry.options["optimization_manual_reserve"] == 0.35
    assert updates[-1]["options"]["optimization_manual_reserve"] == 0.35


def test_auto_apply_reserve_enable_replaces_stale_manual_reserve(opt_module):
    coordinator = _coordinator(
        opt_module,
        "amber",
        optimization_backup_reserve=0.30,
        optimization_manual_reserve=0.15,
        optimization_auto_apply_reserve=False,
    )
    coordinator.entry_id = "entry-1"
    coordinator._config.backup_reserve = 0.30
    coordinator._auto_apply_reserve_enabled = False
    coordinator._manual_backup_reserve = 0.15
    coordinator._enabled = False

    class _ConfigEntries:
        def async_update_entry(self, entry, **kwargs):
            if "data" in kwargs:
                entry.data = kwargs["data"]
            if "options" in kwargs:
                entry.options = kwargs["options"]

    coordinator.hass = SimpleNamespace(
        data={"power_sync": {"entry-1": {}}},
        config_entries=_ConfigEntries(),
    )

    asyncio.run(coordinator.set_auto_apply_reserve_enabled(True))

    assert coordinator.auto_apply_reserve_enabled is True
    assert coordinator.manual_backup_reserve == 0.30
    assert coordinator._entry.data["optimization_manual_reserve"] == 0.30
    assert coordinator._entry.options["optimization_manual_reserve"] == 0.30


def test_auto_apply_reserve_manual_edit_updates_restore_value(opt_module):
    coordinator = _coordinator(
        opt_module,
        "amber",
        optimization_backup_reserve=0.45,
        optimization_manual_reserve=0.45,
        optimization_auto_apply_reserve=True,
    )
    coordinator.entry_id = "entry-1"
    coordinator._auto_apply_reserve_enabled = True
    coordinator._manual_backup_reserve = 0.45
    coordinator._enabled = False

    updates = []

    class _ConfigEntries:
        def async_update_entry(self, entry, **kwargs):
            updates.append(kwargs)
            if "data" in kwargs:
                entry.data = kwargs["data"]
            if "options" in kwargs:
                entry.options = kwargs["options"]

    coordinator.hass = SimpleNamespace(
        data={"power_sync": {"entry-1": {}}},
        config_entries=_ConfigEntries(),
    )

    result = asyncio.run(coordinator.set_settings({"backup_reserve": 25}))

    assert result["success"] is True
    assert coordinator._config.backup_reserve == 0.25
    assert coordinator.manual_backup_reserve == 0.25
    assert coordinator._entry.options["optimization_backup_reserve"] == 0.25
    assert coordinator._entry.options["optimization_manual_reserve"] == 0.25
    assert updates[-1]["options"]["optimization_manual_reserve"] == 0.25


def test_manual_reserve_tracks_backup_reserve_while_auto_apply_is_off(opt_module):
    coordinator = _coordinator(
        opt_module,
        "amber",
        optimization_backup_reserve=0.15,
        optimization_manual_reserve=0.15,
        optimization_auto_apply_reserve=False,
    )
    coordinator.entry_id = "entry-1"
    coordinator._auto_apply_reserve_enabled = False
    coordinator._manual_backup_reserve = 0.15
    coordinator._enabled = False

    class _ConfigEntries:
        def async_update_entry(self, entry, **kwargs):
            if "data" in kwargs:
                entry.data = kwargs["data"]
            if "options" in kwargs:
                entry.options = kwargs["options"]

    coordinator.hass = SimpleNamespace(
        data={"power_sync": {"entry-1": {}}},
        config_entries=_ConfigEntries(),
    )

    result = asyncio.run(coordinator.set_settings({"backup_reserve": 30}))

    assert result["success"] is True
    assert coordinator._config.backup_reserve == 0.30
    assert coordinator.manual_backup_reserve == 0.30
    assert coordinator._entry.data["optimization_manual_reserve"] == 0.30
    assert coordinator._entry.options["optimization_manual_reserve"] == 0.30


def test_auto_apply_reserve_disable_restores_manual_optimizer_reserve(opt_module):
    coordinator = _coordinator(
        opt_module,
        "amber",
        optimization_backup_reserve=0.60,
        optimization_manual_reserve=0.30,
        optimization_auto_apply_reserve=True,
        hardware_backup_reserve=0.10,
        _user_backup_reserve=55,
    )
    coordinator.entry_id = "entry-1"
    coordinator._auto_apply_reserve_enabled = True
    coordinator._manual_backup_reserve = 0.30
    coordinator._config.backup_reserve = 0.60
    coordinator._enabled = False

    updates = []

    class _ConfigEntries:
        def async_update_entry(self, entry, **kwargs):
            updates.append(kwargs)
            if "data" in kwargs:
                entry.data = kwargs["data"]
            if "options" in kwargs:
                entry.options = kwargs["options"]

    coordinator.hass = SimpleNamespace(
        data={"power_sync": {"entry-1": {}}},
        config_entries=_ConfigEntries(),
    )

    asyncio.run(coordinator.set_auto_apply_reserve_enabled(False))

    assert coordinator.auto_apply_reserve_enabled is False
    assert coordinator._config.backup_reserve == 0.30
    assert coordinator._entry.options["optimization_auto_apply_reserve"] is False
    assert coordinator._entry.options["optimization_backup_reserve"] == 0.30
    assert coordinator._entry.options["optimization_manual_reserve"] == 0.30
    assert coordinator._entry.options["hardware_backup_reserve"] == 0.10
    assert coordinator._entry.options["_user_backup_reserve"] == 55


def test_auto_apply_reserve_applies_clamped_optimizer_recommendation(opt_module):
    coordinator = _coordinator(
        opt_module,
        "amber",
        optimization_backup_reserve=0.50,
        optimization_manual_reserve=0.50,
        optimization_auto_apply_reserve=True,
        hardware_backup_reserve=0.20,
    )
    coordinator.entry_id = "entry-1"
    coordinator._auto_apply_reserve_enabled = True
    coordinator._manual_backup_reserve = 0.50
    coordinator._config.backup_reserve = 0.50
    coordinator._startup_backup_reserve = 20
    update_calls = []
    coordinator._optimizer = SimpleNamespace(
        update_config=lambda **kwargs: update_calls.append(kwargs),
        max_grid_export_w=None,
        terminal_weight=0,
    )

    updates = []

    class _ConfigEntries:
        def async_update_entry(self, entry, **kwargs):
            updates.append(kwargs)
            if "data" in kwargs:
                entry.data = kwargs["data"]
            if "options" in kwargs:
                entry.options = kwargs["options"]

    coordinator.hass = SimpleNamespace(
        data={"power_sync": {"entry-1": {}}},
        config_entries=_ConfigEntries(),
    )

    changed = coordinator._apply_auto_reserve_recommendation(
        SimpleNamespace(
            reserve_recommendation={"suggested_optimizer_reserve_percent": 70}
        )
    )

    assert changed is True
    assert coordinator._config.backup_reserve == 0.70
    assert update_calls[-1]["backup_reserve"] == 0.70
    # The forecast floor is applied to the running optimiser only — it must NOT
    # write the config entry (that fired a dashboard refresh every ~5 minutes).
    assert updates == []

    changed = coordinator._apply_auto_reserve_recommendation(
        SimpleNamespace(
            reserve_recommendation={"suggested_optimizer_reserve_percent": 10}
        )
    )

    assert changed is True
    assert coordinator._config.backup_reserve == 0.50
    assert update_calls[-1]["backup_reserve"] == 0.50
    assert updates == []


def test_profit_max_auto_apply_never_lowers_below_manual_reserve(opt_module):
    coordinator = _coordinator(
        opt_module,
        "flow_power",
        profit_max=True,
        optimization_backup_reserve=0.15,
        optimization_manual_reserve=0.15,
        optimization_auto_apply_reserve=True,
        hardware_backup_reserve=0.05,
    )
    coordinator.entry_id = "entry-1"
    coordinator._auto_apply_reserve_enabled = True
    coordinator._manual_backup_reserve = 0.15
    coordinator._config.backup_reserve = 0.15
    coordinator._startup_backup_reserve = 5
    update_calls = []
    coordinator._optimizer = SimpleNamespace(
        update_config=lambda **kwargs: update_calls.append(kwargs),
        max_grid_export_w=None,
        terminal_weight=0,
    )

    updates = []

    class _ConfigEntries:
        def async_update_entry(self, entry, **kwargs):
            updates.append(kwargs)
            if "data" in kwargs:
                entry.data = kwargs["data"]
            if "options" in kwargs:
                entry.options = kwargs["options"]

    coordinator.hass = SimpleNamespace(
        data={"power_sync": {"entry-1": {}}},
        config_entries=_ConfigEntries(),
    )
    recommendation = {"suggested_optimizer_reserve_percent": 5}

    changed = coordinator._apply_auto_reserve_recommendation(
        SimpleNamespace(reserve_recommendation=recommendation)
    )

    assert changed is False
    assert coordinator._config.backup_reserve == 0.15
    assert update_calls == []
    # Runtime-only: no per-cycle config-entry write.
    assert updates == []
    assert recommendation["manual_optimizer_reserve_percent"] == 15
    assert recommendation["applied_optimizer_reserve_percent"] == 15


def test_auto_apply_forecast_bridge_is_independent_of_export_run_length(opt_module):
    """Ticket #263: the manual seed must not choose between 0% and a ratchet."""
    coordinator = _coordinator(
        opt_module,
        "globird",
        optimization_backup_reserve=0.15,
        optimization_manual_reserve=0.15,
        optimization_auto_apply_reserve=True,
        hardware_backup_reserve=0.0,
    )
    coordinator._auto_apply_reserve_enabled = True
    coordinator._manual_backup_reserve = 0.15
    coordinator._config.backup_reserve = 0.15
    coordinator._config.battery_capacity_wh = 40000
    coordinator._config.interval_minutes = 60
    coordinator._startup_backup_reserve = 0
    coordinator._optimizer = SimpleNamespace(efficiency=1.0)

    start = datetime(2026, 7, 13, 17, 0, tzinfo=timezone.utc)
    export_allowed = [False, True, True, True] + [False] * 6

    def _result(export_slots):
        actions = []
        for idx in range(10):
            is_export = idx in export_slots
            is_charge = idx == 8
            actions.append(
                opt_module.ScheduleAction(
                    timestamp=start + timedelta(hours=idx),
                    action=(
                        "export"
                        if is_export
                        else ("charge" if is_charge else "self_consumption")
                    ),
                    power_w=5000 if is_export or is_charge else 0,
                    soc=0.50,
                    battery_charge_w=5000 if is_charge else 0,
                    battery_discharge_w=5000 if is_export else 0,
                )
            )
        return SimpleNamespace(
            schedule=opt_module.OptimizationSchedule(
                actions=actions,
                predicted_cost=0,
                predicted_savings=0,
                last_updated=start,
            ),
            reserve_recommendation={"suggested_optimizer_reserve_percent": 0},
        )

    short_export = _result({1})
    full_window_export = _result({1, 2, 3})
    solar = None
    load = [0.0] * 4 + [1.0] * 4 + [0.0] * 2

    coordinator._set_forecast_bridge_reserve_recommendation(
        short_export,
        export_allowed,
        solar,
        load,
    )
    coordinator._set_forecast_bridge_reserve_recommendation(
        full_window_export,
        export_allowed,
        solar,
        load,
    )

    for result in (short_export, full_window_export):
        recommendation = result.reserve_recommendation
        assert recommendation["manual_optimizer_reserve_percent"] == 15
        assert recommendation["forecast_bridge_kwh"] == pytest.approx(4.0)
        assert recommendation["forecast_bridge_reserve_percent"] == 10
        assert recommendation["suggested_optimizer_reserve_percent"] == 25
        assert recommendation["next_charge_reason"] == "scheduled_grid_charge"


def test_auto_apply_reserve_ignores_home_load_export_bridge_floor(opt_module):
    coordinator = _coordinator(
        opt_module,
        "flow_power",
        profit_max=True,
        optimization_backup_reserve=0.05,
        optimization_manual_reserve=0.05,
        optimization_auto_apply_reserve=True,
        hardware_backup_reserve=0.05,
    )
    coordinator.entry_id = "entry-1"
    coordinator._auto_apply_reserve_enabled = True
    coordinator._manual_backup_reserve = 0.05
    coordinator._config.backup_reserve = 0.05
    coordinator._startup_backup_reserve = 5
    update_calls = []
    coordinator._optimizer = SimpleNamespace(
        update_config=lambda **kwargs: update_calls.append(kwargs),
        max_grid_export_w=None,
        terminal_weight=0,
    )

    updates = []

    class _ConfigEntries:
        def async_update_entry(self, entry, **kwargs):
            updates.append(kwargs)
            if "data" in kwargs:
                entry.data = kwargs["data"]
            if "options" in kwargs:
                entry.options = kwargs["options"]

    coordinator.hass = SimpleNamespace(
        data={"power_sync": {"entry-1": {}}},
        config_entries=_ConfigEntries(),
    )
    recommendation = {
        "suggested_optimizer_reserve_percent": 5,
        "home_load_export_floor_percent": 25,
    }

    changed = coordinator._apply_auto_reserve_recommendation(
        SimpleNamespace(reserve_recommendation=recommendation)
    )

    assert changed is False
    assert coordinator._config.backup_reserve == 0.05
    assert update_calls == []
    assert updates == []
    assert recommendation["applied_optimizer_reserve_percent"] == 5


def test_auto_export_reserve_floor_ignores_future_export_bridge(opt_module):
    coordinator = _coordinator(
        opt_module,
        "flow_power",
        profit_max=True,
        optimization_backup_reserve=0.15,
    )
    coordinator._auto_apply_reserve_enabled = True
    coordinator._config.backup_reserve = 0.15

    floor = coordinator._auto_export_reserve_floor(
        {
            "home_load_export_floor_percent": 83,
            "home_load_bridge_after_export_start": "2026-05-04T17:30:00+10:00",
        }
    )

    assert floor is None


def test_auto_export_reserve_floor_scopes_future_export_bridge(opt_module):
    coordinator = _coordinator(
        opt_module,
        "flow_power",
        profit_max=True,
        optimization_backup_reserve=0.15,
    )
    coordinator._auto_apply_reserve_enabled = True
    coordinator._config.backup_reserve = 0.15
    coordinator._config.interval_minutes = 5

    floors = coordinator._auto_export_reserve_floor_slots(
        {
            "home_load_export_floor_percent": 83,
            "home_load_bridge_after_export_start": "2026-05-04T17:30:00+10:00",
        },
        576,
    )

    assert floors is not None
    first_scoped_slot = next(idx for idx, value in enumerate(floors) if value)
    assert first_scoped_slot > 200
    assert max(floors[:first_scoped_slot]) == 0
    assert max(floors[first_scoped_slot:]) == pytest.approx(0.83)


def test_auto_export_reserve_floor_applies_same_day_export_bridge(opt_module):
    coordinator = _coordinator(
        opt_module,
        "flow_power",
        profit_max=True,
        optimization_backup_reserve=0.15,
    )
    coordinator._auto_apply_reserve_enabled = True
    coordinator._config.backup_reserve = 0.15

    floor = coordinator._auto_export_reserve_floor(
        {
            "home_load_export_floor_percent": 83,
            "home_load_bridge_after_export_start": "2026-05-03T17:30:00+10:00",
        }
    )

    assert floor == pytest.approx(0.83)


def test_auto_apply_export_bridge_floor_can_exceed_lowered_active_reserve(opt_module):
    coordinator = _coordinator(
        opt_module,
        "flow_power",
        profit_max=True,
        optimization_backup_reserve=0.40,
        optimization_manual_reserve=0.20,
        optimization_auto_apply_reserve=True,
        hardware_backup_reserve=0.0,
    )
    coordinator._auto_apply_reserve_enabled = True
    coordinator._manual_backup_reserve = 0.20
    coordinator._config.backup_reserve = 0.40
    coordinator._config.battery_capacity_wh = 40000
    coordinator._startup_backup_reserve = 0
    update_calls = []
    coordinator._optimizer = SimpleNamespace(
        update_config=lambda **kwargs: update_calls.append(kwargs),
        efficiency=0.95,
        hardware_reserve_known=True,
        hardware_reserve=0.0,
        max_grid_export_w=None,
        terminal_weight=0,
    )

    changed = coordinator._apply_auto_reserve_recommendation(
        SimpleNamespace(
            reserve_recommendation={"suggested_optimizer_reserve_percent": 20}
        )
    )

    assert changed is True
    assert coordinator._config.backup_reserve == pytest.approx(0.20)
    start = datetime(2026, 5, 3, 17, 30, tzinfo=timezone.utc)
    actions = [
        opt_module.ScheduleAction(
            timestamp=start + idx * timedelta(minutes=5),
            action="export" if idx < 3 else ("charge" if idx == 30 else "idle"),
            power_w=20000 if idx < 3 else 0,
            soc=0.70,
            battery_charge_w=20000 if idx == 30 else 0,
            battery_discharge_w=20000 if idx < 3 else 0,
        )
        for idx in range(36)
    ]
    schedule = opt_module.OptimizationSchedule(
        actions=actions,
        predicted_cost=0,
        predicted_savings=0,
        last_updated=start,
    )

    floors, metadata = coordinator._post_processed_export_reserve_floor_slots(
        schedule,
        solar_forecast=[0.0] * 30 + [10.0] * 6,
        load_forecast=[5.0] * 36,
    )

    assert floors is not None
    assert max(floors) > coordinator._config.backup_reserve
    assert metadata["home_load_export_floor_percent"] > 20
    assert metadata["home_load_bridge_next_charge_reason"] == "scheduled_grid_charge"


def test_auto_apply_export_bridge_includes_post_no_idle_home_load(opt_module):
    coordinator = _coordinator(
        opt_module,
        "flow_power",
        profit_max=True,
        optimization_backup_reserve=0.05,
        optimization_auto_apply_reserve=True,
        hardware_backup_reserve=0.05,
    )
    coordinator._auto_apply_reserve_enabled = True
    coordinator._config.backup_reserve = 0.05
    coordinator._config.battery_capacity_wh = 10000
    coordinator._startup_backup_reserve = 5
    coordinator._optimizer = SimpleNamespace(
        efficiency=0.95,
        hardware_reserve_known=True,
        hardware_reserve=0.05,
    )
    start = datetime(2026, 5, 3, 18, 0, tzinfo=timezone.utc)
    actions = [
        opt_module.ScheduleAction(
            timestamp=start + idx * timedelta(minutes=5),
            action="export" if idx == 0 else ("charge" if idx == 13 else "self_consumption"),
            power_w=5000 if idx == 0 else 0,
            soc=0.70,
            battery_charge_w=5000 if idx == 13 else 0,
            battery_discharge_w=5000 if idx == 0 else 0,
        )
        for idx in range(18)
    ]
    schedule = opt_module.OptimizationSchedule(
        actions=actions,
        predicted_cost=0,
        predicted_savings=0,
        last_updated=start,
    )

    floors, metadata = coordinator._post_processed_export_reserve_floor_slots(
        schedule,
        solar_forecast=[0.0] * 13 + [6.0] * 5,
        load_forecast=[2.0] * 18,
    )

    assert floors is not None
    assert floors[0] > 0.05
    assert metadata["home_load_bridge_kwh"] == pytest.approx(2.0)
    assert metadata["home_load_bridge_next_charge_reason"] == "scheduled_grid_charge"


def test_force_discharge_reserve_floor_ignores_future_export_bridge(opt_module):
    coordinator = _coordinator(
        opt_module,
        "flow_power",
        profit_max=True,
        optimization_backup_reserve=0.15,
    )
    coordinator._auto_apply_reserve_enabled = True
    coordinator._config.backup_reserve = 0.15
    coordinator._last_optimizer_result = SimpleNamespace(
        reserve_recommendation={
            "home_load_export_floor_percent": 83,
            "home_load_bridge_after_export_start": "2026-05-04T17:30:00+10:00",
        }
    )

    floor = coordinator._force_discharge_reserve_floor()

    assert floor == pytest.approx(0.15)


def test_auto_apply_reserve_ignores_relaxed_infeasible_result(opt_module):
    """A relaxed/infeasible solve must never lower the optimiser reserve.

    Relaxed solves run with an artificially lowered 5% floor, so their reserve
    recommendation is bogus. Regression for the optimiser reserve collapsing to
    the hardware floor under Profit Max + Auto-Apply.
    """
    coordinator = _coordinator(
        opt_module,
        "flow_power",
        profit_max=True,
        optimization_backup_reserve=0.15,
        optimization_manual_reserve=0.15,
        optimization_auto_apply_reserve=True,
        hardware_backup_reserve=0.05,
    )
    coordinator.entry_id = "entry-1"
    coordinator._auto_apply_reserve_enabled = True
    coordinator._manual_backup_reserve = 0.15
    coordinator._config.backup_reserve = 0.15
    coordinator._startup_backup_reserve = 5
    update_calls = []
    coordinator._optimizer = SimpleNamespace(
        update_config=lambda **kwargs: update_calls.append(kwargs),
        max_grid_export_w=None,
        terminal_weight=0,
    )
    coordinator.hass = SimpleNamespace(
        data={"power_sync": {"entry-1": {}}},
        config_entries=SimpleNamespace(async_update_entry=lambda *a, **k: None),
    )

    changed = coordinator._apply_auto_reserve_recommendation(
        SimpleNamespace(
            feasible=False,
            reserve_recommendation={"suggested_optimizer_reserve_percent": 5},
        )
    )

    assert changed is False
    assert coordinator._config.backup_reserve == 0.15
    assert update_calls == []


def test_set_settings_ignores_interval_minutes_override(opt_module):
    coordinator = _coordinator(opt_module, "amber")
    coordinator.entry_id = "entry-1"
    coordinator._config.interval_minutes = 30
    coordinator._entry.data = {"optimization_interval": 30}
    coordinator._entry.options["optimization_interval"] = 30

    updates = []

    class _ConfigEntries:
        def async_update_entry(self, entry, **kwargs):
            updates.append(kwargs)
            if "data" in kwargs:
                entry.data = kwargs["data"]
            if "options" in kwargs:
                entry.options = kwargs["options"]

    coordinator.hass = SimpleNamespace(
        data={"power_sync": {"entry-1": {}}},
        config_entries=_ConfigEntries(),
    )

    result = asyncio.run(coordinator.set_settings({"interval_minutes": 30}))

    assert result["success"] is True
    assert result["changes"] == []
    assert coordinator._config.interval_minutes == 5
    assert updates == []


def _prepare_enabled_settings_coordinator(coordinator):
    coordinator.entry_id = "entry-1"
    coordinator._enabled = True
    coordinator._load_estimator = None
    coordinator._settings_reoptimize_task = None
    coordinator._settings_reoptimize_requested = False
    updates = []
    run_calls = []
    background_tasks = []

    class _ConfigEntries:
        def async_update_entry(self, entry, **kwargs):
            updates.append(kwargs)
            if "data" in kwargs:
                entry.data = kwargs["data"]
            if "options" in kwargs:
                entry.options = kwargs["options"]

    async def _run_optimization():
        run_calls.append(True)

    def async_create_background_task(coro, name):
        background_tasks.append(name)
        coro.close()
        return SimpleNamespace(done=lambda: True, cancel=lambda: None)

    coordinator.hass = SimpleNamespace(
        data={"power_sync": {"entry-1": {}}},
        config_entries=_ConfigEntries(),
        async_create_background_task=async_create_background_task,
    )
    coordinator._run_optimization = _run_optimization
    return updates, run_calls, background_tasks


def test_auto_apply_reserve_setting_schedules_background_reoptimization(opt_module):
    coordinator = _coordinator(
        opt_module,
        "amber",
        optimization_backup_reserve=0.60,
        optimization_manual_reserve=0.30,
        optimization_auto_apply_reserve=True,
    )
    coordinator._auto_apply_reserve_enabled = True
    coordinator._manual_backup_reserve = 0.30
    coordinator._config.backup_reserve = 0.60
    updates, run_calls, background_tasks = _prepare_enabled_settings_coordinator(
        coordinator
    )

    result = asyncio.run(
        coordinator.set_settings({"auto_apply_reserve_enabled": False})
    )

    assert result["success"] is True
    assert result["changes"] == ["auto_apply_reserve_enabled: False"]
    assert coordinator.auto_apply_reserve_enabled is False
    assert coordinator._config.backup_reserve == 0.30
    assert updates[-1]["options"]["optimization_auto_apply_reserve"] is False
    assert run_calls == []
    assert background_tasks == ["powersync_settings_reoptimize"]


def test_profit_max_setting_change_schedules_background_reoptimization(opt_module):
    coordinator = _coordinator(opt_module, "flow_power", profit_max=False)
    updates, run_calls, background_tasks = _prepare_enabled_settings_coordinator(coordinator)

    result = asyncio.run(coordinator.set_settings({"profit_max_enabled": True}))

    assert result["success"] is True
    assert result["changes"] == ["profit_max_enabled: True"]
    assert coordinator._config.profit_max_enabled is True
    assert updates[-1]["options"]["profit_max_enabled"] is True
    assert run_calls == []
    assert background_tasks == ["powersync_settings_reoptimize"]


@pytest.mark.parametrize(
    ("settings", "expected_change"),
    [
        ({"spread_export_enabled": True}, "spread_export_enabled: True"),
        ({"spread_import_enabled": True}, "spread_import_enabled: True"),
        ({"disable_idle_enabled": True}, "disable_idle_enabled: True"),
    ],
)
def test_optimizer_mode_setting_change_schedules_background_reoptimization(
    opt_module,
    settings,
    expected_change,
):
    coordinator = _coordinator(opt_module, "flow_power")
    updates, run_calls, background_tasks = _prepare_enabled_settings_coordinator(
        coordinator
    )

    result = asyncio.run(coordinator.set_settings(settings))

    assert result["success"] is True
    assert expected_change in result["changes"]
    assert updates
    assert run_calls == []
    assert background_tasks == ["powersync_settings_reoptimize"]


def test_optimizer_config_setting_change_schedules_background_reoptimization(opt_module):
    coordinator = _coordinator(opt_module, "flow_power")
    updates, run_calls, background_tasks = _prepare_enabled_settings_coordinator(coordinator)

    result = asyncio.run(coordinator.set_settings({"allow_grid_charge": False}))

    assert result["success"] is True
    assert "config: ['allow_grid_charge']" in result["changes"]
    assert coordinator._config.allow_grid_charge is False
    assert updates[-1]["options"]["allow_grid_charge"] is False
    assert run_calls == []
    assert background_tasks == ["powersync_settings_reoptimize"]


def test_grid_charge_advanced_settings_persist_and_reoptimize(opt_module):
    coordinator = _coordinator(opt_module, "flow_power")
    updates, run_calls, background_tasks = _prepare_enabled_settings_coordinator(coordinator)

    result = asyncio.run(
        coordinator.set_settings({
            "max_grid_charge_price": 30,
            "grid_charge_soc_cap": 80,
        })
    )

    assert result["success"] is True
    assert "config: ['max_grid_charge_price', 'grid_charge_soc_cap']" in result["changes"]
    assert coordinator._config.max_grid_charge_price == pytest.approx(0.30)
    assert coordinator._config.grid_charge_soc_cap == pytest.approx(0.80)
    assert updates[-1]["options"]["optimization_max_grid_charge_price"] == pytest.approx(0.30)
    assert updates[-1]["options"]["optimization_grid_charge_soc_cap"] == pytest.approx(0.80)
    assert run_calls == []
    assert background_tasks == ["powersync_settings_reoptimize"]


def test_grid_charge_allowed_slots_apply_price_caps_before_lp_soc_cap(opt_module):
    coordinator = _coordinator(opt_module, "flow_power")
    coordinator._config.battery_capacity_wh = 10000
    coordinator._config.max_charge_w = 5000
    coordinator._config.max_grid_charge_price = 0.30
    coordinator._config.grid_charge_soc_cap = 0.80

    allowed = coordinator._grid_charge_allowed_slots(
        import_prices=[0.20, 0.20, 0.40, 0.20],
        solar_forecast=[5.0, 5.0, 0.0, 0.0],
        load_forecast=[0.0, 0.0, 0.0, 0.0],
        current_soc=0.79,
    )

    assert allowed == [True, True, False, True]


def test_globird_zerocharge_limits_grid_charge_to_configured_window(opt_module):
    coordinator = _coordinator(
        opt_module,
        "globird",
        globird_plan="zerohero_custom",
        globird_zerocharge_start="11:00",
        globird_zerocharge_end="14:00",
        globird_zerocharge_import_cap_kwh=50.0,
    )
    coordinator._last_price_timestamps = [
        datetime(2026, 7, 7, 13, 50, tzinfo=timezone(timedelta(hours=10)))
        + timedelta(minutes=5 * idx)
        for idx in range(6)
    ]

    allowed = coordinator._grid_charge_allowed_slots(
        import_prices=[0.0] * 6,
        solar_forecast=[0.0] * 6,
        load_forecast=[0.0] * 6,
        current_soc=0.50,
    )

    assert allowed == [True, True, False, False, False, False]

    coordinator._actual_zerocharge_import_kwh_today = 50.0
    assert coordinator._grid_charge_allowed_slots(
        import_prices=[0.0] * 6,
        solar_forecast=[0.0] * 6,
        load_forecast=[0.0] * 6,
        current_soc=0.50,
    ) == [False] * 6


@pytest.mark.parametrize(
    ("settings", "expected_change"),
    [
        ({"charge_by_time_enabled": True}, "charge_by_time_enabled: True"),
        ({"charge_by_time_target_time": "16:00"}, "charge_by_time_target_time: 16:00"),
        ({"charge_by_time_target_soc": 80}, "charge_by_time_target_soc: 80%"),
        ({"profit_max_target_time": "16:00"}, "profit_max_target_time: 16:00"),
        ({"profit_max_target_soc": 80}, "profit_max_target_soc: 80%"),
    ],
)
def test_charge_by_time_setting_change_schedules_background_reoptimization(
    opt_module,
    settings,
    expected_change,
):
    coordinator = _coordinator(
        opt_module,
        "flow_power",
        charge_by_time=False,
        charge_by_time_target_time="17:15",
        charge_by_time_target_soc=1.0,
    )
    updates, run_calls, background_tasks = _prepare_enabled_settings_coordinator(coordinator)

    result = asyncio.run(coordinator.set_settings(settings))

    assert result["success"] is True
    assert expected_change in result["changes"]
    assert updates
    assert run_calls == []
    assert background_tasks == ["powersync_settings_reoptimize"]
    if "target_time" in next(iter(settings)):
        assert coordinator._config.charge_by_time_target_time == "16:00"
    if "target_soc" in next(iter(settings)):
        assert coordinator._config.charge_by_time_target_soc == 0.8
    assert background_tasks == ["powersync_settings_reoptimize"]


def test_startup_uses_fixed_optimization_interval_not_persisted_value():
    init_source = (ROOT / "custom_components" / "power_sync" / "__init__.py").read_text()

    assert "saved_interval_minutes = DEFAULT_OPTIMIZATION_INTERVAL" in init_source
    assert "CONF_OPTIMIZATION_INTERVAL, entry.data.get" not in init_source


def _covau_snapshot_dict() -> dict:
    return {
        "schema_version": 1,
        "parser_version": 1,
        "plan_id": "COV1117616MRE2@EME",
        "display_name": "SolarMax SA Residential TOU",
        "distributor": "SA Power Networks",
        "state": "SA",
        "effective_date": "2026-07-01T00:00:00Z",
        "withdrawn_date": None,
        "timezone_token": "AEST",
        "supply_c_per_day": 172.0,
        "import_periods": [
            {"start": "00:00", "end": "06:00", "c_per_kwh": 16.5},
            {"start": "06:00", "end": "15:00", "c_per_kwh": 35.17},
            {"start": "15:00", "end": "21:00", "c_per_kwh": 58.78},
            {"start": "21:00", "end": "24:00", "c_per_kwh": 35.17},
        ],
        "export_base_c_per_kwh": 5.0,
        "free_import_start": "11:00",
        "free_import_end": "14:00",
        "free_import_cap_kwh": 50.0,
        "premium_export_start": "18:00",
        "premium_export_end": "21:00",
        "premium_export_cap_kwh": 30.0,
        "premium_export_total_c_per_kwh": 15.0,
        "source_kind": "aer_cdr",
        "source_url": "https://example.test/covau",
        "source_last_updated": "2026-06-30T14:06:51Z",
        "content_hash": "fixture-hash",
        "manual": False,
    }


def test_covau_forecast_partitions_caps_by_fixed_aest_tariff_day(opt_module):
    now = datetime(2026, 5, 3, 0, 30, tzinfo=timezone.utc)  # 10:30 AEST
    opt_module.dt_util.now = lambda: now
    coordinator = _coordinator(
        opt_module,
        "covau",
        covau_plan_snapshot=_covau_snapshot_dict(),
    )
    coordinator.hass = SimpleNamespace(data={}, states=SimpleNamespace(get=lambda _eid: None))
    coordinator._config.horizon_hours = 48
    state = opt_module.QuotaLedgerState(
        tariff_day="2026-05-03",
        timezone_token="AEST",
        confidence="authoritative",
        settled_kwh={
            opt_module.COVAU_IMPORT_RULE_ID: 1.0,
            opt_module.COVAU_EXPORT_RULE_ID: 2.0,
        },
    )
    coordinator._ensure_covau_ledger(state, now=now)

    prices = coordinator._covau_price_forecast()

    assert prices is not None
    assert coordinator._last_zerocharge_bonus_cap_kwh == pytest.approx(99.0)
    assert coordinator._last_zerohero_bonus_cap_kwh == pytest.approx(58.0)
    assert coordinator._last_import_bonus_caps_by_group == {
        "2026-05-03": pytest.approx(49.0),
        "2026-05-04": pytest.approx(50.0),
    }
    assert coordinator._last_export_bonus_caps_by_group == {
        "2026-05-03": pytest.approx(28.0),
        "2026-05-04": pytest.approx(30.0),
    }
    timestamps = coordinator._pending_price_timestamps
    for bonuses in (
        coordinator._last_zerocharge_bonus_prices,
        coordinator._last_zerohero_bonus_prices,
    ):
        active = [timestamps[idx] for idx, value in enumerate(bonuses) if value > 0]
        assert active
        assert {
            opt_module.tariff_datetime(value, "AEST").date().isoformat()
            for value in active
        } == {"2026-05-03", "2026-05-04"}

    priority = coordinator._priority_export_slots_for_run(
        len(timestamps),
        prices[1],
    )
    priority_times = [timestamps[idx] for idx, value in enumerate(priority) if value]
    assert priority_times
    assert {
        opt_module.tariff_datetime(value, "AEST").date().isoformat()
        for value in priority_times
    } == {"2026-05-03", "2026-05-04"}

    saved = coordinator._quota_state_v2_to_save()
    assert saved["provider"] == "covau"
    assert saved["settled_kwh"][opt_module.COVAU_IMPORT_RULE_ID] == 1.0


def test_covau_cumulative_pcc_settlement_counts_only_matching_windows(opt_module):
    snapshot = _covau_snapshot_dict()
    options = {
        "covau_plan_snapshot": snapshot,
        "covau_import_energy_entity": "sensor.grid_import_energy",
        "covau_export_energy_entity": "sensor.grid_export_energy",
    }
    coordinator = _coordinator(opt_module, "covau", **options)
    first = datetime(2026, 5, 3, 1, 30, tzinfo=timezone.utc)  # 11:30 AEST
    previous = first - timedelta(minutes=5)
    states = {
        "sensor.grid_import_energy": SimpleNamespace(
            state="100.5",
            attributes={"unit_of_measurement": "kWh"},
            last_updated=first,
        ),
        "sensor.grid_export_energy": SimpleNamespace(
            state="200.5",
            attributes={"unit_of_measurement": "kWh"},
            last_updated=first,
        ),
    }
    coordinator.hass = SimpleNamespace(
        data={},
        states=SimpleNamespace(get=states.get),
    )
    state = opt_module.QuotaLedgerState(
        tariff_day="2026-05-03",
        timezone_token="AEST",
        confidence="authoritative",
        settled_kwh={
            opt_module.COVAU_IMPORT_RULE_ID: 0.0,
            opt_module.COVAU_EXPORT_RULE_ID: 0.0,
        },
        last_meter_kwh={"import": 100.0, "export": 200.0},
        last_sample_at={
            "import": previous.isoformat(),
            "export": previous.isoformat(),
        },
        source_kind={"import": "total_increasing", "export": "total_increasing"},
    )
    coordinator._ensure_covau_ledger(state, now=first)

    settled = coordinator._settle_covau_measurements(first, 0.0, 0.0)
    assert settled == {"import": pytest.approx(0.5), "export": 0.0}

    export_time = datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)  # 18:30 AEST
    states["sensor.grid_import_energy"] = SimpleNamespace(
        state="100.5",
        attributes={"unit_of_measurement": "kWh"},
        last_updated=export_time,
    )
    states["sensor.grid_export_energy"] = SimpleNamespace(
        state="200.9",
        attributes={"unit_of_measurement": "kWh"},
        last_updated=export_time,
    )
    coordinator._covau_ledger.state.last_sample_at["export"] = (
        export_time - timedelta(minutes=5)
    ).isoformat()
    coordinator._covau_ledger.state.last_meter_kwh["export"] = 200.5
    settled = coordinator._settle_covau_measurements(export_time, 0.0, 0.0)
    assert settled == {"import": 0.0, "export": pytest.approx(0.4)}
    assert coordinator._covau_ledger.remaining_kwh(
        opt_module.COVAU_IMPORT_RULE_ID
    ) == pytest.approx(49.5)
    assert coordinator._covau_ledger.remaining_kwh(
        opt_module.COVAU_EXPORT_RULE_ID
    ) == pytest.approx(29.6)


def test_globird_legacy_status_is_preserved_while_dual_writing_quota_v2(opt_module):
    coordinator = _coordinator(
        opt_module,
        "globird",
        globird_plan="zerohero_current",
    )
    coordinator._last_cost_date = "2026-05-03"
    coordinator._actual_zerohero_bonus_export_kwh_today = 6.25
    coordinator._actual_zerocharge_import_kwh_today = 3.5

    legacy = coordinator._zerohero_cost_breakdown()
    quota_state = coordinator._quota_state_v2_to_save()

    assert legacy["status"] == "enabled"
    assert legacy["bonus_export_kwh_used"] == pytest.approx(6.25)
    assert quota_state["provider"] == "globird"
    assert quota_state["settled_kwh"][
        opt_module.GLOBIRD_QUOTA_EXPORT_RULE_ID
    ] == pytest.approx(6.25)
    assert quota_state["settled_kwh"][
        opt_module.GLOBIRD_QUOTA_IMPORT_RULE_ID
    ] == pytest.approx(3.5)


def _true_indexes(slots: list[bool]) -> list[int]:
    return [idx for idx, value in enumerate(slots) if value]


def test_positive_export_prices_allowed_when_profit_max_off(opt_module):
    coordinator = _coordinator(opt_module, "octopus", profit_max=False)

    assert coordinator._battery_export_allowed_slots(4, [0.0, 0.01, 0.08, -0.02]) == [
        False,
        True,
        True,
        False,
    ]
    assert coordinator._profit_max_terminal_weight() == 1.0


@pytest.mark.parametrize("provider", ["amber", "aemo_vpp", "globird", "octopus", "nz"])
@pytest.mark.parametrize("profit_max", [False, True])
def test_positive_export_prices_allowed_for_all_providers(
    opt_module,
    provider,
    profit_max,
):
    coordinator = _coordinator(opt_module, provider, profit_max=profit_max)

    slots = coordinator._battery_export_allowed_slots(
        6,
        [0.0, -0.02, 0.01, 0.08, 0.12, None],
    )

    assert _true_indexes(slots) == [2, 3, 4]


def test_zerohero_positive_base_fit_does_not_allow_export_outside_bonus_window(
    opt_module,
):
    coordinator = _coordinator(
        opt_module,
        "globird",
        globird_plan="zerohero_current",
    )
    coordinator._last_zerohero_bonus_cap_kwh = 5.0
    coordinator._last_price_timestamps = [
        datetime(2026, 5, 3, 17, 30, tzinfo=timezone.utc) + timedelta(minutes=5 * idx)
        for idx in range(48)
    ]

    slots = coordinator._battery_export_allowed_slots(48, [0.05] * 48)

    assert _true_indexes(slots) == list(range(6, 42))


def test_zerohero_bonus_window_is_priority_export_while_cap_remains(opt_module):
    coordinator = _coordinator(
        opt_module,
        "globird",
        globird_plan="zerohero_current",
    )
    coordinator._last_zerohero_bonus_cap_kwh = 5.0
    coordinator._last_price_timestamps = [
        datetime(2026, 5, 3, 17, 30, tzinfo=timezone.utc) + timedelta(minutes=5 * idx)
        for idx in range(48)
    ]

    export_allowed = coordinator._battery_export_allowed_slots(48, [0.05] * 48)
    priority_slots = coordinator._priority_export_slots_for_run(48, [0.05] * 48)

    assert _true_indexes(export_allowed) == list(range(6, 42))
    assert _true_indexes(priority_slots) == list(range(6, 42))


def test_zerohero_priority_export_survives_lost_no_import_credit(opt_module):
    coordinator = _coordinator(
        opt_module,
        "globird",
        globird_plan="zerohero_current",
    )
    coordinator._actual_zerohero_import_kwh_today = 1.0
    coordinator._actual_zerohero_bonus_export_kwh_today = 6.0
    coordinator._last_price_timestamps = [
        datetime(2026, 5, 3, 17, 30, tzinfo=timezone.utc) + timedelta(minutes=5 * idx)
        for idx in range(48)
    ]
    import_prices = [0.40] * 48
    export_prices = [0.05] * 48

    coordinator._apply_zerohero_optimizer_inputs(import_prices, export_prices)
    export_allowed = coordinator._battery_export_allowed_slots(48, export_prices)
    priority_slots = coordinator._priority_export_slots_for_run(48, export_prices)

    assert coordinator._zerohero_credit_lost()
    assert coordinator._last_zerohero_bonus_cap_kwh == pytest.approx(9.0)
    assert _true_indexes(export_allowed) == list(range(6, 42))
    assert _true_indexes(priority_slots) == list(range(6, 42))


def test_zerohero_priority_export_disabled_when_bonus_cap_exhausted(opt_module):
    coordinator = _coordinator(
        opt_module,
        "globird",
        globird_plan="zerohero_current",
    )
    coordinator._last_zerohero_bonus_cap_kwh = 0.0
    coordinator._last_price_timestamps = [
        datetime(2026, 5, 3, 17, 30, tzinfo=timezone.utc) + timedelta(minutes=5 * idx)
        for idx in range(48)
    ]

    export_allowed = coordinator._battery_export_allowed_slots(48, [0.05] * 48)
    priority_slots = coordinator._priority_export_slots_for_run(48, [0.05] * 48)

    assert export_allowed == [False] * 48
    assert priority_slots == [False] * 48


def test_flow_power_profit_window_is_priority_export(opt_module):
    coordinator = _coordinator(
        opt_module,
        "flow_power",
        profit_max=True,
        flow_power_state="NSW1",
    )

    slots = coordinator._priority_export_slots_for_run(288, [0.45] * 288)

    assert _true_indexes(slots) == list(range(108, 132))


def test_flow_power_happy_hour_is_priority_export_without_profit_max(opt_module):
    coordinator = _coordinator(
        opt_module,
        "flow_power",
        profit_max=False,
        flow_power_state="NSW1",
    )

    slots = coordinator._priority_export_slots_for_run(288, [0.45] * 288)

    assert _true_indexes(slots) == list(range(108, 132))


def test_zerohero_blocks_battery_charge_during_no_import_window(opt_module):
    coordinator = _coordinator(
        opt_module,
        "globird",
        globird_plan="zerohero_current",
    )
    coordinator._last_price_timestamps = [
        datetime(2026, 5, 3, 17, 30, tzinfo=timezone.utc) + timedelta(minutes=5 * idx)
        for idx in range(48)
    ]

    slots = coordinator._battery_charge_blocked_slots(48)

    assert _true_indexes(slots) == list(range(6, 42))


def test_non_positive_export_prices_are_blocked(opt_module):
    coordinator = _coordinator(opt_module, "amber", profit_max=False)

    assert coordinator._battery_export_allowed_slots(4, [0.0, -0.03, None, 0.0]) == [
        False,
        False,
        False,
        False,
    ]


def test_profit_max_reduces_terminal_soc_weight_for_all_providers(opt_module):
    coordinator = _coordinator(opt_module, "amber", profit_max=True)

    assert coordinator._profit_max_terminal_weight() == 0.3


def test_octopus_joined_saving_session_allows_only_session_slots(opt_module):
    coordinator = _coordinator(opt_module, "octopus", profit_max=False)
    coordinator._saving_session_coordinator = SimpleNamespace(
        data={
            "sessions": [
                SimpleNamespace(
                    joined=True,
                    session_type="saving",
                    start=datetime(2026, 5, 3, 9, 0, tzinfo=timezone.utc),
                    end=datetime(2026, 5, 3, 9, 30, tzinfo=timezone.utc),
                )
            ]
        }
    )

    slots = coordinator._battery_export_allowed_slots(12, [0.0] * 12)

    assert _true_indexes(slots) == list(range(6, 12))


def test_octopus_free_electricity_does_not_allow_battery_export(opt_module):
    coordinator = _coordinator(opt_module, "octopus", profit_max=False)
    coordinator._saving_session_coordinator = SimpleNamespace(
        data={
            "sessions": [
                SimpleNamespace(
                    joined=True,
                    session_type="free_electricity",
                    start=datetime(2026, 5, 3, 9, 0, tzinfo=timezone.utc),
                    end=datetime(2026, 5, 3, 9, 30, tzinfo=timezone.utc),
                )
            ]
        }
    )

    assert coordinator._battery_export_allowed_slots(12, [0.0] * 12) == [False] * 12


def test_saving_session_price_overlay_ignores_null_octopoints(opt_module):
    coordinator = _coordinator(opt_module, "octopus", profit_max=True)
    coordinator._saving_session_coordinator = SimpleNamespace(
        data={
            "sessions": [
                SimpleNamespace(
                    joined=True,
                    session_type="saving",
                    start=datetime(2026, 5, 3, 9, 0, tzinfo=timezone.utc),
                    end=datetime(2026, 5, 3, 9, 30, tzinfo=timezone.utc),
                    octopoints_per_kwh=None,
                )
            ]
        },
        _octopoints_per_penny=8,
    )

    import_prices, export_prices = coordinator._apply_saving_session_prices(
        [0.20] * 12,
        [0.05] * 12,
    )

    assert import_prices == [0.20] * 12
    assert export_prices == [0.05] * 12


def test_saving_session_price_overlay_normalizes_naive_session_datetimes(opt_module):
    coordinator = _coordinator(opt_module, "octopus", profit_max=True)
    coordinator._saving_session_coordinator = SimpleNamespace(
        data={
            "sessions": [
                SimpleNamespace(
                    joined=True,
                    session_type="saving",
                    start=datetime(2026, 5, 3, 9, 0),
                    end=datetime(2026, 5, 3, 9, 30),
                    octopoints_per_kwh=800,
                )
            ]
        },
        _octopoints_per_penny=8,
    )

    import_prices, export_prices = coordinator._apply_saving_session_prices(
        [0.20] * 12,
        [0.05] * 12,
    )

    assert import_prices[:6] == [0.20] * 6
    assert export_prices[:6] == [0.05] * 6
    assert import_prices[6:] == [2.0] * 6
    assert export_prices[6:] == [1.05] * 6


def test_flow_power_profit_max_allows_only_happy_hour(opt_module):
    coordinator = _coordinator(
        opt_module,
        "flow_power",
        profit_max=True,
        flow_power_state="NSW1",
    )

    slots = coordinator._battery_export_allowed_slots(288, [0.0] * 288)

    assert _true_indexes(slots) == list(range(108, 132))
    assert coordinator._profit_max_terminal_weight() == 0.3


def test_profit_max_without_charge_by_time_does_not_set_prefill_target(opt_module):
    coordinator = _coordinator(
        opt_module,
        "amber",
        profit_max=True,
        charge_by_time=False,
    )

    assert coordinator._next_charge_by_time_target_slot() is None
    assert coordinator._profit_max_terminal_weight() == 0.3


@pytest.mark.parametrize("provider", ["amber", "aemo_vpp", "globird", "octopus", "nz", "flow_power"])
def test_charge_by_time_uses_default_target_for_all_providers(opt_module, provider):
    coordinator = _coordinator(
        opt_module,
        provider,
        charge_by_time=True,
    )

    assert coordinator._next_charge_by_time_target_slot() == 105


def test_charge_by_time_uses_configured_target(opt_module):
    coordinator = _coordinator(
        opt_module,
        "octopus",
        charge_by_time=True,
        charge_by_time_target_time="16:00",
    )

    assert coordinator._next_charge_by_time_target_slot() == 90


def test_charge_by_time_floors_non_boundary_target(opt_module):
    coordinator = _coordinator(
        opt_module,
        "globird",
        charge_by_time=True,
        charge_by_time_target_time="16:01",
    )

    assert coordinator._next_charge_by_time_target_slot() == 90


def test_charge_by_time_target_uses_live_coordinator_setting(opt_module):
    coordinator = _coordinator(
        opt_module,
        "aemo_vpp",
        charge_by_time=True,
        charge_by_time_target_time="16:00",
    )
    coordinator._entry.options["charge_by_time_target_time"] = "17:15"

    assert coordinator._next_charge_by_time_target_slot() == 105


def test_charge_by_time_accepts_legacy_live_target_setting(opt_module):
    coordinator = _coordinator(
        opt_module,
        "aemo_vpp",
        charge_by_time=True,
        charge_by_time_target_time="16:00",
    )
    coordinator._entry.options.pop("charge_by_time_target_time", None)
    coordinator._entry.options["profit_max_target_time"] = "17:15"

    assert coordinator._next_charge_by_time_target_slot() == 105


def test_charge_by_time_accepts_compact_target(opt_module):
    coordinator = _coordinator(
        opt_module,
        "nz",
        charge_by_time=True,
        charge_by_time_target_time="1615",
    )

    assert coordinator._next_charge_by_time_target_slot() == 93


def test_charge_by_time_uses_default_soc_target(opt_module):
    coordinator = _coordinator(
        opt_module,
        "flow_power",
        charge_by_time=True,
    )

    assert coordinator._charge_by_time_target_soc() == 1.0


def test_charge_by_time_uses_configured_soc_target(opt_module):
    coordinator = _coordinator(
        opt_module,
        "flow_power",
        charge_by_time=True,
        charge_by_time_target_soc=0.8,
    )

    assert coordinator._charge_by_time_target_soc() == 0.8
    assert coordinator._next_charge_by_time_target_slot() == 105


def test_charge_by_time_accepts_percent_soc_target(opt_module):
    coordinator = _coordinator(
        opt_module,
        "flow_power",
        charge_by_time=True,
        charge_by_time_target_soc=80,
    )

    assert coordinator._charge_by_time_target_soc() == 0.8


def test_charge_by_time_accepts_target_after_flow_power_happy_hour_start(opt_module):
    coordinator = _coordinator(
        opt_module,
        "flow_power",
        charge_by_time=True,
        charge_by_time_target_time="18:00",
    )

    assert coordinator._next_charge_by_time_target_slot() == 114


def test_flow_power_blocks_battery_charge_during_happy_hour(opt_module):
    coordinator = _coordinator(
        opt_module,
        "flow_power",
        profit_max=False,
        flow_power_state="NSW1",
    )

    slots = coordinator._battery_charge_blocked_slots(288)

    assert _true_indexes(slots) == list(range(108, 132))


def test_export_boost_allows_only_configured_window_above_threshold(opt_module):
    coordinator = _coordinator(
        opt_module,
        "octopus",
        export_boost_enabled=True,
        export_price_offset=5.0,
        export_boost_start="09:00",
        export_boost_end="09:30",
        export_boost_threshold=10.0,
    )
    export_prices = [0.09] * 6 + [0.12] * 6

    boosted, boost_mask = coordinator._apply_export_boost(
        export_prices,
        [0.05] * 12,
    )
    slots = coordinator._battery_export_allowed_slots(12, boosted)

    assert _true_indexes(boost_mask) == list(range(6, 12))
    assert _true_indexes(slots) == list(range(12))
    assert boosted[:6] == export_prices[:6]
    assert all(price > 0.12 for price in boosted[6:])


def test_chip_mode_threshold_uses_real_export_price_after_boost(opt_module):
    coordinator = _coordinator(
        opt_module,
        "amber",
        export_boost_enabled=True,
        export_price_offset=10.0,
        export_boost_start="08:30",
        export_boost_end="09:30",
        export_boost_threshold=0.0,
        chip_mode_enabled=True,
        chip_mode_start="08:30",
        chip_mode_end="09:30",
        chip_mode_threshold=25.0,
    )
    export_prices = [0.208] * 12

    boosted, _ = coordinator._apply_export_boost(export_prices, [0.05] * 12)
    chipped = coordinator._apply_chip_mode(boosted, export_prices)

    assert all(price > 0.25 for price in boosted)
    assert chipped == [0.0] * 12


class _FakeBattery:
    def __init__(
        self,
        hardware_mode: str | None = None,
        backup_reserve: int | None = None,
    ) -> None:
        self.hardware_mode = hardware_mode
        self.backup_reserve = backup_reserve
        self.self_consumption_calls = 0
        self.restore_normal_calls = 0
        self.backup_reserve_calls = []
        self.force_charge_calls = []
        self.force_discharge_calls = []

    async def get_tesla_operation_mode(self):
        return self.hardware_mode

    async def get_backup_reserve(self):
        return self.backup_reserve

    async def set_self_consumption_mode(self):
        self.self_consumption_calls += 1

    async def restore_normal(self):
        self.restore_normal_calls += 1

    async def set_backup_reserve(self, percent):
        self.backup_reserve_calls.append(percent)

    async def force_charge(self, duration_minutes=60, power_w=5000, _extend_hardware=False):
        self.force_charge_calls.append((duration_minutes, power_w, _extend_hardware))

    async def force_discharge(
        self,
        duration_minutes=60,
        power_w=5000,
        _extend_hardware=False,
        _tariff_duration=None,
    ):
        self.force_discharge_calls.append(
            (duration_minutes, power_w, _extend_hardware, _tariff_duration)
        )


class _FakeEnergyCoordinator:
    def __init__(self) -> None:
        self.restore_work_mode_from_idle_calls = 0
        self.no_discharge_calls = 0
        self.restore_no_discharge_calls = 0
        self.discharge_blocked_after_restore = False

    async def restore_work_mode_from_idle(self):
        self.restore_work_mode_from_idle_calls += 1

    async def set_no_discharge_mode(self):
        self.no_discharge_calls += 1
        return True

    async def restore_no_discharge_mode(self):
        self.restore_no_discharge_calls += 1
        return True

    def _discharge_appears_blocked_after_restore(self):
        return self.discharge_blocked_after_restore


def _execution_coordinator(opt_module, battery: _FakeBattery, soc: float):
    coordinator = _coordinator(opt_module, "octopus")
    coordinator.hass = SimpleNamespace(data={}, scheduled=[])
    coordinator.entry_id = "entry-1"
    coordinator._entry = SimpleNamespace(options={}, data={})
    coordinator._executor = SimpleNamespace(battery_controller=battery)
    coordinator._force_state_getter = None
    coordinator._force_state_clearer = None
    coordinator._last_executed_action = "self_consumption"
    coordinator._startup_backup_reserve = 20
    coordinator._pre_idle_backup_reserve = None
    coordinator._idle_hold_reserve = None
    coordinator._scheduled_ev_no_discharge_active = False
    coordinator._optimizer_force_state = {
        "active": False,
        "type": None,
        "expires_at": None,
        "hardware_expires_at": None,
        "power_w": 0,
        "started_at": None,
        "source": "optimizer",
        "scope": "optimizer",
    }
    coordinator._last_export_prices = None
    coordinator.energy_coordinator = None
    coordinator.battery_system = "tesla"
    coordinator._is_in_demand_window = lambda: False
    coordinator._should_block_export_for_demand = lambda: False
    coordinator._minutes_to_demand_start = lambda: None

    async def _battery_state():
        return soc, 13500

    coordinator._get_battery_state = _battery_state
    return coordinator


def _api_action(timestamp: datetime, action: str, power_w: float, soc: float):
    return SimpleNamespace(
        timestamp=timestamp,
        action=action,
        power_w=power_w,
        soc=soc,
        battery_charge_w=0,
        battery_discharge_w=power_w if action in ("discharge", "export") else 0,
        to_dict=lambda: {
            "timestamp": timestamp.isoformat(),
            "action": action,
            "power_w": power_w,
            "soc": soc,
        },
    )


def test_api_current_action_uses_effective_runtime_action(opt_module):
    coordinator = _coordinator(opt_module, "octopus")
    now = datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    actions = [
        _api_action(now, "export", 5000, 0.55),
        _api_action(now + timedelta(minutes=5), "export", 5000, 0.54),
        _api_action(now + timedelta(minutes=10), "self_consumption", 0, 0.54),
    ]
    coordinator._current_schedule = SimpleNamespace(
        actions=actions,
        to_api_response=lambda: {
            "timestamps": [action.timestamp.isoformat() for action in actions],
            "soc": [action.soc for action in actions],
            "actions": [action.action for action in actions],
        },
    )
    coordinator._optimizer = object()
    coordinator._enabled = True
    coordinator._cost_function = opt_module.CostFunction("cost")
    coordinator._last_update_time = now
    coordinator._last_optimizer_result = None
    coordinator._last_executed_planned_action = "export"
    coordinator._last_executed_action = "self_consumption"
    coordinator._startup_backup_reserve = 20
    coordinator._battery_specs_source = "config"
    coordinator._planned_ev_load_entity_id = None
    coordinator._ev_integration_enabled = False
    coordinator._ev_configs = []
    coordinator._ev_coordinator = None
    coordinator._last_planned_ev_load_forecast_w = []
    coordinator._last_import_prices = None
    coordinator._last_export_prices = None
    coordinator._last_display_import_prices = None
    coordinator._last_display_export_prices = None
    coordinator._actual_cost_today = 0.0
    coordinator._actual_baseline_today = 0.0
    coordinator._actual_import_cost_today = 0.0
    coordinator._actual_export_earnings_today = 0.0
    coordinator._actual_import_kwh_today = 0.0
    coordinator._actual_export_kwh_today = 0.0
    coordinator._actual_charge_kwh_today = 0.0
    coordinator._actual_discharge_kwh_today = 0.0
    coordinator.hass = SimpleNamespace(data={})
    coordinator.entry_id = "entry-1"
    coordinator._get_actual_battery_power_w = lambda: -300
    coordinator._get_current_action = lambda: actions[0]
    coordinator._get_daily_cost = lambda: 0.0
    coordinator._get_daily_savings = lambda: 0.0
    coordinator._get_predicted_cost_to_midnight = lambda: (0.0, 0.0)
    coordinator._get_warnings = lambda: []
    coordinator._summarise_load_forecast = lambda: None
    coordinator._zerohero_cost_breakdown = lambda: {}
    coordinator._should_spread_export_schedule = lambda: False
    coordinator._should_spread_import_schedule = lambda: False
    coordinator._get_demand_window_config = lambda: None
    coordinator._is_in_demand_window_at = lambda timestamp: False

    data = coordinator.get_api_data()

    assert data["planned_current_action"] == "export"
    assert data["planned_current_power_w"] == 5000
    assert data["effective_current_action"] == "self_consumption"
    assert data["current_action"] == "self_consumption"
    assert data["current_power_w"] == -300
    assert data["next_action"] == "self_consumption"


def test_api_no_idle_publishes_modeled_self_use_and_exempt_idle(opt_module):
    coordinator = _coordinator(opt_module, "flow_power")
    coordinator._config.disable_idle_enabled = True
    now = datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    actions = [
        _api_action(now, "idle", 0, 0.55),
        _api_action(now + timedelta(minutes=5), "idle", 0, 0.55),
        _api_action(now + timedelta(minutes=10), "charge", 5000, 0.60),
    ]
    coordinator._current_schedule = SimpleNamespace(
        actions=actions,
        to_api_response=lambda: {
            "timestamps": [action.timestamp.isoformat() for action in actions],
            "soc": [action.soc for action in actions],
            "actions": [action.action for action in actions],
        },
    )
    coordinator._optimizer = object()
    coordinator._enabled = True
    coordinator._cost_function = opt_module.CostFunction("cost")
    coordinator._last_update_time = now
    coordinator._last_optimizer_result = None
    coordinator._last_executed_planned_action = None
    coordinator._last_executed_action = None
    coordinator._startup_backup_reserve = 20
    coordinator._battery_specs_source = "config"
    coordinator._planned_ev_load_entity_id = None
    coordinator._ev_integration_enabled = False
    coordinator._ev_configs = []
    coordinator._ev_coordinator = None
    coordinator._last_planned_ev_load_forecast_w = []
    coordinator._last_import_prices = None
    coordinator._last_export_prices = None
    coordinator._last_display_import_prices = None
    coordinator._last_display_export_prices = None
    coordinator._actual_cost_today = 0.0
    coordinator._actual_baseline_today = 0.0
    coordinator._actual_import_cost_today = 0.0
    coordinator._actual_export_earnings_today = 0.0
    coordinator._actual_import_kwh_today = 0.0
    coordinator._actual_export_kwh_today = 0.0
    coordinator._actual_charge_kwh_today = 0.0
    coordinator._actual_discharge_kwh_today = 0.0
    coordinator.hass = SimpleNamespace(data={})
    coordinator.entry_id = "entry-1"
    coordinator._get_actual_battery_power_w = lambda: -300
    coordinator._get_current_action = lambda: actions[0]
    coordinator._get_daily_cost = lambda: 0.0
    coordinator._get_daily_savings = lambda: 0.0
    coordinator._get_predicted_cost_to_midnight = lambda: (0.0, 0.0)
    coordinator._get_warnings = lambda: []
    coordinator._summarise_load_forecast = lambda: None
    coordinator._zerohero_cost_breakdown = lambda: {}
    coordinator._should_spread_export_schedule = lambda: False
    coordinator._should_spread_import_schedule = lambda: False
    coordinator._get_demand_window_config = lambda: None
    coordinator._is_in_demand_window_at = lambda timestamp: False

    data = coordinator.get_api_data()

    assert data["planned_current_action"] == "idle"
    assert data["effective_current_action"] == "idle"
    assert data["current_action"] == "idle"
    assert data["current_power_w"] == -300
    assert data["next_action"] == "charge"
    assert data["next_action_power_w"] == 5000
    assert data["next_actions"][0]["action"] == "idle"
    assert "planned_action" not in data["next_actions"][0]

    # Ordinary No Idle slots are modeled as self-consumption before
    # publication, so the 24-hour Action Plan must show that same action.
    self_use_actions = [
        _api_action(now, "self_consumption", 1200, 0.55),
        _api_action(now + timedelta(minutes=5), "self_consumption", 1200, 0.54),
        _api_action(now + timedelta(minutes=10), "charge", 5000, 0.60),
    ]
    coordinator._current_schedule = SimpleNamespace(
        actions=self_use_actions,
        to_api_response=lambda: {
            "timestamps": [action.timestamp.isoformat() for action in self_use_actions],
            "soc": [action.soc for action in self_use_actions],
            "actions": [action.action for action in self_use_actions],
        },
    )
    coordinator._get_current_action = lambda: self_use_actions[0]

    data = coordinator.get_api_data()

    assert data["current_action"] == "self_consumption"
    assert data["next_actions"][0]["action"] == "self_consumption"


def test_get_current_action_returns_none_past_schedule_end(opt_module):
    """HD-4: a schedule whose slots have all elapsed must not pin the final action forever."""
    coordinator = _coordinator(opt_module, "octopus")
    now = datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    actions = [
        _api_action(now - timedelta(hours=4), "charge", 5000, 0.5),
        _api_action(now - timedelta(hours=3, minutes=55), "charge", 5000, 0.5),
        _api_action(now - timedelta(hours=3, minutes=50), "self_consumption", 0, 0.5),
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)

    assert coordinator._get_current_action() is None


def test_api_reports_stale_status_when_schedule_and_update_time_expired(opt_module):
    """HD-4: a swallowed solve failure must surface as a stale status, not silent 'active'."""
    coordinator = _coordinator(opt_module, "octopus")
    now = datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    stale_update_time = now - timedelta(hours=4)
    actions = [
        _api_action(now - timedelta(hours=4), "charge", 5000, 0.5),
        _api_action(now - timedelta(hours=3, minutes=55), "charge", 5000, 0.5),
        _api_action(now - timedelta(hours=3, minutes=50), "self_consumption", 0, 0.5),
    ]
    coordinator._current_schedule = SimpleNamespace(
        actions=actions,
        to_api_response=lambda: {
            "timestamps": [action.timestamp.isoformat() for action in actions],
            "soc": [action.soc for action in actions],
            "actions": [action.action for action in actions],
        },
    )
    coordinator._optimizer = object()
    coordinator._enabled = True
    coordinator._cost_function = opt_module.CostFunction("cost")
    coordinator._last_update_time = stale_update_time
    coordinator._last_optimizer_result = None
    coordinator._last_executed_planned_action = None
    coordinator._last_executed_action = None
    coordinator._startup_backup_reserve = 20
    coordinator._battery_specs_source = "config"
    coordinator._planned_ev_load_entity_id = None
    coordinator._ev_integration_enabled = False
    coordinator._ev_configs = []
    coordinator._ev_coordinator = None
    coordinator._last_planned_ev_load_forecast_w = []
    coordinator._last_import_prices = None
    coordinator._last_export_prices = None
    coordinator._last_display_import_prices = None
    coordinator._last_display_export_prices = None
    coordinator._actual_cost_today = 0.0
    coordinator._actual_baseline_today = 0.0
    coordinator._actual_import_cost_today = 0.0
    coordinator._actual_export_earnings_today = 0.0
    coordinator._actual_import_kwh_today = 0.0
    coordinator._actual_export_kwh_today = 0.0
    coordinator._actual_charge_kwh_today = 0.0
    coordinator._actual_discharge_kwh_today = 0.0
    coordinator.hass = SimpleNamespace(data={})
    coordinator.entry_id = "entry-1"
    coordinator._get_actual_battery_power_w = lambda: 0
    coordinator._get_daily_cost = lambda: 0.0
    coordinator._get_daily_savings = lambda: 0.0
    coordinator._get_predicted_cost_to_midnight = lambda: (0.0, 0.0)
    coordinator._get_warnings = lambda: []
    coordinator._summarise_load_forecast = lambda: None
    coordinator._zerohero_cost_breakdown = lambda: {}
    coordinator._should_spread_export_schedule = lambda: False
    coordinator._should_spread_import_schedule = lambda: False
    coordinator._get_demand_window_config = lambda: None
    coordinator._is_in_demand_window_at = lambda timestamp: False

    data = coordinator.get_api_data()

    assert data["schedule_age_s"] == pytest.approx(4 * 3600, abs=5)
    assert data["optimization_status"] == "stale"


def test_api_current_action_uses_optimizer_force_command_power(opt_module):
    coordinator = _coordinator(opt_module, "octopus")
    now = datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    actions = [
        _api_action(now, "charge", 10000, 0.55),
        _api_action(now + timedelta(minutes=5), "charge", 10000, 0.56),
        _api_action(now + timedelta(minutes=10), "self_consumption", 0, 0.57),
    ]
    coordinator._current_schedule = SimpleNamespace(
        actions=actions,
        to_api_response=lambda: {
            "timestamps": [action.timestamp.isoformat() for action in actions],
            "soc": [action.soc for action in actions],
            "actions": [action.action for action in actions],
        },
    )
    coordinator._optimizer = object()
    coordinator._enabled = True
    coordinator._cost_function = opt_module.CostFunction("cost")
    coordinator._last_update_time = now
    coordinator._last_optimizer_result = None
    coordinator._last_executed_planned_action = "charge"
    coordinator._last_executed_action = "charge"
    coordinator._optimizer_force_state = {
        "active": True,
        "type": "charge",
        "power_w": 1019,
    }
    coordinator._startup_backup_reserve = 20
    coordinator._battery_specs_source = "config"
    coordinator._planned_ev_load_entity_id = None
    coordinator._ev_integration_enabled = False
    coordinator._ev_configs = []
    coordinator._ev_coordinator = None
    coordinator._last_planned_ev_load_forecast_w = []
    coordinator._last_import_prices = None
    coordinator._last_export_prices = None
    coordinator._last_display_import_prices = None
    coordinator._last_display_export_prices = None
    coordinator._actual_cost_today = 0.0
    coordinator._actual_baseline_today = 0.0
    coordinator._actual_import_cost_today = 0.0
    coordinator._actual_export_earnings_today = 0.0
    coordinator._actual_import_kwh_today = 0.0
    coordinator._actual_export_kwh_today = 0.0
    coordinator._actual_charge_kwh_today = 0.0
    coordinator._actual_discharge_kwh_today = 0.0
    coordinator.hass = SimpleNamespace(data={})
    coordinator.entry_id = "entry-1"
    coordinator._get_actual_battery_power_w = lambda: -900
    coordinator._get_current_action = lambda: actions[0]
    coordinator._get_daily_cost = lambda: 0.0
    coordinator._get_daily_savings = lambda: 0.0
    coordinator._get_predicted_cost_to_midnight = lambda: (0.0, 0.0)
    coordinator._get_warnings = lambda: []
    coordinator._summarise_load_forecast = lambda: None
    coordinator._zerohero_cost_breakdown = lambda: {}
    coordinator._should_spread_export_schedule = lambda: False
    coordinator._should_spread_import_schedule = lambda: False
    coordinator._get_demand_window_config = lambda: None
    coordinator._is_in_demand_window_at = lambda timestamp: False

    data = coordinator.get_api_data()

    assert data["planned_current_action"] == "charge"
    assert data["planned_current_power_w"] == 10000
    assert data["effective_current_action"] == "charge"
    assert data["current_action"] == "charge"
    assert data["current_power_w"] == 1019


def test_api_current_action_uses_held_optimizer_force_when_plan_changes(opt_module):
    coordinator = _coordinator(opt_module, "flow_power")
    now = datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    actions = [
        _api_action(now, "self_consumption", 0, 0.55),
        _api_action(now + timedelta(minutes=5), "self_consumption", 0, 0.56),
        _api_action(now + timedelta(minutes=10), "charge", 10000, 0.57),
    ]
    coordinator._current_schedule = SimpleNamespace(
        actions=actions,
        to_api_response=lambda: {
            "timestamps": [action.timestamp.isoformat() for action in actions],
            "soc": [action.soc for action in actions],
            "actions": [action.action for action in actions],
        },
    )
    coordinator._optimizer = object()
    coordinator._enabled = True
    coordinator._cost_function = opt_module.CostFunction("cost")
    coordinator._last_update_time = now
    coordinator._last_optimizer_result = None
    coordinator._last_executed_planned_action = "charge"
    coordinator._last_executed_action = "charge"
    coordinator._optimizer_force_state = {
        "active": True,
        "type": "charge",
        "power_w": 10000,
        "expires_at": now + timedelta(minutes=15),
        "source": "optimizer",
        "scope": "optimizer",
    }
    coordinator._startup_backup_reserve = 20
    coordinator._battery_specs_source = "config"
    coordinator._planned_ev_load_entity_id = None
    coordinator._ev_integration_enabled = False
    coordinator._ev_configs = []
    coordinator._ev_coordinator = None
    coordinator._last_planned_ev_load_forecast_w = []
    coordinator._last_import_prices = None
    coordinator._last_export_prices = None
    coordinator._last_display_import_prices = None
    coordinator._last_display_export_prices = None
    coordinator._actual_cost_today = 0.0
    coordinator._actual_baseline_today = 0.0
    coordinator._actual_import_cost_today = 0.0
    coordinator._actual_export_earnings_today = 0.0
    coordinator._actual_import_kwh_today = 0.0
    coordinator._actual_export_kwh_today = 0.0
    coordinator._actual_charge_kwh_today = 0.0
    coordinator._actual_discharge_kwh_today = 0.0
    coordinator.hass = SimpleNamespace(data={})
    coordinator.entry_id = "entry-1"
    coordinator._get_actual_battery_power_w = lambda: -9000
    coordinator._get_current_action = lambda: actions[0]
    coordinator._get_daily_cost = lambda: 0.0
    coordinator._get_daily_savings = lambda: 0.0
    coordinator._get_predicted_cost_to_midnight = lambda: (0.0, 0.0)
    coordinator._get_warnings = lambda: []
    coordinator._summarise_load_forecast = lambda: None
    coordinator._zerohero_cost_breakdown = lambda: {}
    coordinator._should_spread_export_schedule = lambda: False
    coordinator._should_spread_import_schedule = lambda: False
    coordinator._get_demand_window_config = lambda: None
    coordinator._is_in_demand_window_at = lambda timestamp: False

    data = coordinator.get_api_data()

    assert data["planned_current_action"] == "self_consumption"
    assert data["planned_current_power_w"] == 0
    assert data["effective_current_action"] == "charge"
    assert data["current_action"] == "charge"
    assert data["current_power_w"] == 10000


def test_solar_forecast_warning_waits_for_forecast_attempt(opt_module):
    coordinator = _coordinator(opt_module, "octopus")

    coordinator._has_solar_forecast = None
    assert coordinator._get_warnings() == []

    coordinator._has_solar_forecast = False
    warnings = coordinator._get_warnings()
    assert [warning["type"] for warning in warnings] == ["no_solar_forecast"]

    coordinator._has_solar_forecast = True
    assert coordinator._get_warnings() == []


def test_coordinator_refresh_executes_cached_charge_at_action_boundary(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.25)
    coordinator._enabled = True
    coordinator._optimization_lock = SimpleNamespace(locked=lambda: False)
    coordinator.get_api_data = lambda: {"ok": True}
    boundary = datetime(2026, 5, 3, 11, 0, tzinfo=timezone.utc)
    actions = [
        SimpleNamespace(
            action="self_consumption",
            power_w=0,
            timestamp=boundary - timedelta(minutes=5),
        ),
        SimpleNamespace(
            action="charge",
            power_w=5000,
            timestamp=boundary,
        ),
        SimpleNamespace(
            action="self_consumption",
            power_w=0,
            timestamp=boundary + timedelta(minutes=5),
        ),
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)
    original_now = opt_module.dt_util.now
    opt_module.dt_util.now = lambda *args, **kwargs: boundary

    try:
        result = asyncio.run(coordinator._async_update_data())
    finally:
        opt_module.dt_util.now = original_now

    assert result == {"ok": True}
    assert battery.force_charge_calls == [(5, 5000, False)]
    assert coordinator._last_executed_action == "charge"


def _enable_scheduled_ev_preserve(coordinator):
    coordinator.hass.data = {
        "power_sync": {
            coordinator.entry_id: {
                "scheduled_ev_preserve_state": {"active": True}
            }
        }
    }


def test_self_consumption_reapplies_when_tesla_mode_drifted_to_tou(opt_module):
    battery = _FakeBattery(hardware_mode="autonomous")
    coordinator = _execution_coordinator(opt_module, battery, soc=0.50)

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )

    assert battery.self_consumption_calls == 1
    assert battery.backup_reserve_calls == [20]
    assert coordinator._last_executed_action == "self_consumption"


def test_scheduled_ev_preserve_blocks_export_but_allows_charge(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    energy = _FakeEnergyCoordinator()
    coordinator.energy_coordinator = energy
    _enable_scheduled_ev_preserve(coordinator)

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="export", power_w=5000)
        )
    )

    assert battery.force_discharge_calls == []
    assert energy.no_discharge_calls == 1
    assert coordinator._last_executed_action == "no_discharge"

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="charge", power_w=3000)
        )
    )

    assert battery.force_charge_calls == [(10, 3000, False)]
    assert energy.no_discharge_calls == 1


def test_scheduled_ev_preserve_blocks_polling_backup_reserve_restore(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    coordinator._pre_idle_backup_reserve = 20
    coordinator._last_executed_action = "self_consumption"
    coordinator._scheduled_ev_no_discharge_active = True

    assert not coordinator._should_restore_pre_idle_backup_reserve_from_polling()

    coordinator._scheduled_ev_no_discharge_active = False

    assert coordinator._should_restore_pre_idle_backup_reserve_from_polling()


def test_free_import_charge_uses_live_solar_headroom_under_site_import_cap(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.25)
    coordinator.battery_system = "goodwe"
    coordinator._config.max_grid_import_w = 10000
    coordinator._config.max_charge_w = 13600
    coordinator._last_import_prices = [0.0]
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "solar_power": 6.2,
            "load_power": 1.9,
            "grid_power": 9.3,
            "battery_power": -13.5,
        }
    )

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="charge", power_w=11300)
        )
    )

    assert battery.force_charge_calls == [(10, 13600, False)]
    assert coordinator._optimizer_force_state["power_w"] == 13600


def test_free_import_charge_lowers_command_when_live_site_headroom_drops(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.25)
    coordinator.battery_system = "goodwe"
    coordinator._config.max_grid_import_w = 10000
    coordinator._config.max_charge_w = 13600
    coordinator._last_import_prices = [0.0]
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "solar_power": 0.5,
            "load_power": 2.0,
            "grid_power": 0.0,
            "battery_power": 0.0,
        }
    )

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="charge", power_w=12100)
        )
    )

    assert battery.force_charge_calls == [(10, 8500, False)]
    assert coordinator._optimizer_force_state["power_w"] == 8500


def test_paid_import_charge_keeps_scheduled_optimizer_power(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.25)
    coordinator.battery_system = "goodwe"
    coordinator._config.max_grid_import_w = 10000
    coordinator._config.max_charge_w = 13600
    coordinator._last_import_prices = [0.12]
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "solar_power": 6.2,
            "load_power": 1.9,
            "grid_power": 9.3,
            "battery_power": -13.5,
        }
    )

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="charge", power_w=11300)
        )
    )

    assert battery.force_charge_calls == [(10, 11300, False)]
    assert coordinator._optimizer_force_state["power_w"] == 11300


def test_free_import_charge_keeps_schedule_for_non_target_power_battery(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.25)
    coordinator.battery_system = "tesla"
    coordinator._config.max_grid_import_w = 10000
    coordinator._config.max_charge_w = 13600
    coordinator._last_import_prices = [0.0]
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "solar_power": 0.0,
            "load_power": 1.9,
            "grid_power": 9.3,
            "battery_power": -13.5,
        }
    )

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="charge", power_w=11300)
        )
    )

    assert battery.force_charge_calls == [(10, 11300, False)]


def test_tesla_force_charge_action_yields_to_live_solar(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.25)
    coordinator.battery_system = "tesla"
    coordinator._config.max_grid_import_w = 10000
    coordinator._config.max_charge_w = 13600
    coordinator._last_import_prices = [0.12]
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "solar_power": 3.8,
            "load_power": 3.3,
            "grid_power": 0.0,
            "battery_power": -0.5,
            "battery_level": 46.0,
        }
    )

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="charge", power_w=10000)
        )
    )

    assert battery.force_charge_calls == []
    assert battery.self_consumption_calls == 1
    assert coordinator._last_executed_action == "self_consumption"


def test_tesla_force_charge_action_runs_during_free_import_with_live_solar(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.25)
    coordinator.battery_system = "tesla"
    coordinator._config.max_grid_import_w = 10000
    coordinator._config.max_charge_w = 13600
    coordinator._last_import_prices = [0.0]
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "solar_power": 3.8,
            "load_power": 0.7,
            "grid_power": 0.0,
            "battery_power": -0.5,
            "battery_level": 46.0,
        }
    )

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="charge", power_w=10000)
        )
    )

    assert battery.force_charge_calls == [(10, 10000, False)]
    assert battery.self_consumption_calls == 0


def test_scheduled_ev_preserve_cancels_active_optimizer_export(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    energy = _FakeEnergyCoordinator()
    coordinator.energy_coordinator = energy
    coordinator.battery_system = "goodwe"
    start = datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    actions = [
        SimpleNamespace(
            action="export",
            power_w=5000,
            timestamp=start + idx * timedelta(minutes=5),
        )
        for idx in range(3)
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))
    _enable_scheduled_ev_preserve(coordinator)
    asyncio.run(coordinator._execute_optimizer_action(actions[0]))

    assert battery.force_discharge_calls == [(15, 5000, False, None)]
    assert battery.restore_normal_calls == 1
    assert energy.no_discharge_calls == 1
    assert coordinator._optimizer_force_state["active"] is False
    assert coordinator._last_executed_action == "no_discharge"


def test_active_optimizer_export_at_reserve_is_canceled_not_extended(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.15)
    coordinator.battery_system = "goodwe"
    coordinator._config.backup_reserve = 0.15
    action = SimpleNamespace(
        action="export",
        power_w=5000,
        timestamp=datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc),
    )
    coordinator._current_schedule = SimpleNamespace(actions=[action])
    coordinator._set_optimizer_force_state("discharge", 15, 5000)

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.force_discharge_calls == []
    assert battery.restore_normal_calls == 1
    assert coordinator._optimizer_force_state["active"] is False
    assert coordinator._last_executed_action == "self_consumption"


def test_optimizer_export_may_finish_exactly_at_reserve(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.18)
    coordinator.battery_system = "goodwe"
    coordinator._config.backup_reserve = 0.15
    action = SimpleNamespace(
        action="export",
        power_w=3000,
        soc=0.15,
        timestamp=datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc),
    )
    coordinator._current_schedule = SimpleNamespace(actions=[action])

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.force_discharge_calls
    assert coordinator._last_executed_action == "export"


def test_stale_profit_max_bridge_metadata_does_not_raise_export_floor(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.10)
    coordinator.battery_system = "goodwe"
    coordinator._config.backup_reserve = 0.05
    coordinator._config.profit_max_enabled = True
    coordinator._auto_apply_reserve_enabled = True
    coordinator._last_optimizer_result = SimpleNamespace(
        reserve_recommendation={"home_load_export_floor_percent": 15}
    )
    action = SimpleNamespace(
        action="export",
        power_w=5000,
        timestamp=datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc),
    )
    coordinator._current_schedule = SimpleNamespace(actions=[action])
    coordinator._set_optimizer_force_state("discharge", 5, 5000)

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.force_discharge_calls
    assert battery.restore_normal_calls == 0
    assert coordinator._optimizer_force_state["active"] is True


def test_profit_max_uses_configured_export_floor_not_bridge_metadata(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.18)
    coordinator.battery_system = "goodwe"
    coordinator._config.backup_reserve = 0.05
    coordinator._config.profit_max_enabled = True
    coordinator._auto_apply_reserve_enabled = True
    coordinator._last_optimizer_result = SimpleNamespace(
        reserve_recommendation={"home_load_export_floor_percent": 15}
    )
    action = SimpleNamespace(
        action="export",
        power_w=5000,
        soc=0.14,
        timestamp=datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc),
    )
    coordinator._current_schedule = SimpleNamespace(actions=[action])

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.force_discharge_calls
    assert coordinator._last_executed_action == "export"


def test_auto_apply_uses_active_reserve_not_bridge_metadata_without_profit_max(
    opt_module,
):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.18)
    coordinator.battery_system = "goodwe"
    coordinator._config.backup_reserve = 0.05
    coordinator._config.profit_max_enabled = False
    coordinator._auto_apply_reserve_enabled = True
    coordinator._last_optimizer_result = SimpleNamespace(
        reserve_recommendation={"home_load_export_floor_percent": 15}
    )
    action = SimpleNamespace(
        action="export",
        power_w=5000,
        soc=0.14,
        timestamp=datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc),
    )
    coordinator._current_schedule = SimpleNamespace(actions=[action])

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.force_discharge_calls
    assert coordinator._last_executed_action == "export"


def test_stale_per_slot_export_floor_does_not_override_active_reserve(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.30)
    coordinator.battery_system = "goodwe"
    coordinator._config.backup_reserve = 0.05
    coordinator._auto_apply_reserve_enabled = True
    timestamp = datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    action = SimpleNamespace(
        action="export",
        power_w=5000,
        soc=0.24,
        timestamp=timestamp,
    )
    coordinator._current_schedule = SimpleNamespace(actions=[action])
    coordinator._set_active_export_reserve_floor_slots(
        [0.25],
        coordinator._current_schedule,
    )

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.force_discharge_calls
    assert coordinator._last_executed_action == "export"


def test_scheduled_ev_preserve_release_restores_no_discharge_mode(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    energy = _FakeEnergyCoordinator()
    coordinator.energy_coordinator = energy
    _enable_scheduled_ev_preserve(coordinator)

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )
    coordinator.hass.data["power_sync"][coordinator.entry_id][
        "scheduled_ev_preserve_state"
    ]["active"] = False

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )

    assert energy.no_discharge_calls == 1
    assert energy.restore_no_discharge_calls == 1
    assert coordinator._last_executed_action == "self_consumption"


def test_self_consumption_skips_redundant_call_when_tesla_mode_matches(opt_module):
    battery = _FakeBattery(hardware_mode="self_consumption")
    coordinator = _execution_coordinator(opt_module, battery, soc=0.50)

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )

    assert battery.self_consumption_calls == 0
    assert battery.backup_reserve_calls == []
    assert coordinator._last_executed_action == "self_consumption"


def test_self_consumption_reapplies_tesla_reserve_floor_when_mode_matches(opt_module):
    battery = _FakeBattery(hardware_mode="self_consumption", backup_reserve=0)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.50)

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )

    assert battery.self_consumption_calls == 0
    assert battery.backup_reserve_calls == [20]
    assert coordinator._last_executed_action == "self_consumption"


def test_self_consumption_uses_hardware_reserve_when_startup_reserve_is_lower(opt_module):
    battery = _FakeBattery(hardware_mode="self_consumption", backup_reserve=0)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.50)
    coordinator._startup_backup_reserve = 0

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )

    assert battery.backup_reserve_calls == []


def test_self_consumption_adopts_manual_tesla_reserve_above_cached_target(opt_module):
    battery = _FakeBattery(hardware_mode="self_consumption", backup_reserve=10)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.65)
    coordinator._startup_backup_reserve = 5
    coordinator._config.backup_reserve = 0.10

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )

    assert battery.self_consumption_calls == 0
    assert battery.backup_reserve_calls == []
    assert coordinator._startup_backup_reserve == 10
    assert coordinator._last_executed_action == "self_consumption"


def test_self_consumption_does_not_adopt_pending_tesla_idle_hold(opt_module):
    battery = _FakeBattery(hardware_mode="self_consumption", backup_reserve=55)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.55)
    coordinator._startup_backup_reserve = 5
    coordinator._config.backup_reserve = 0.10
    coordinator._pre_idle_backup_reserve = 5
    coordinator._idle_hold_reserve = 55

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )

    assert battery.backup_reserve_calls == [5]
    assert coordinator._startup_backup_reserve == 5


def test_self_consumption_reapplies_stale_tesla_full_reserve(opt_module):
    battery = _FakeBattery(hardware_mode="autonomous", backup_reserve=100)
    coordinator = _execution_coordinator(opt_module, battery, soc=1.0)
    coordinator._startup_backup_reserve = 0
    coordinator._config.backup_reserve = 0.20

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )

    assert battery.self_consumption_calls == 1
    assert battery.backup_reserve_calls == [0]
    assert coordinator._startup_backup_reserve == 0
    assert coordinator._last_executed_action == "self_consumption"


def test_self_consumption_does_not_raise_tesla_reserve_above_current_soc(opt_module):
    battery = _FakeBattery(hardware_mode="self_consumption", backup_reserve=5)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.11)
    coordinator._config.backup_reserve = 0.25
    coordinator._startup_backup_reserve = 5

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )

    assert battery.self_consumption_calls == 0
    assert battery.backup_reserve_calls == []
    assert coordinator._last_executed_action == "self_consumption"


def test_self_consumption_lowers_stale_tesla_reserve_when_below_floor(opt_module):
    battery = _FakeBattery(hardware_mode="self_consumption", backup_reserve=25)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.11)
    coordinator._config.backup_reserve = 0.25
    coordinator._startup_backup_reserve = 5

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )

    assert battery.self_consumption_calls == 0
    assert battery.backup_reserve_calls == [5]
    assert coordinator._last_executed_action == "self_consumption"


def test_self_consumption_does_not_push_optimizer_floor_to_goodwe_reserve(opt_module):
    battery = _FakeBattery(hardware_mode="self_consumption", backup_reserve=20)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.43)
    coordinator.battery_system = "goodwe"
    coordinator._config.backup_reserve = 0.45
    coordinator._startup_backup_reserve = 20
    coordinator._last_executed_action = "idle"

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )

    assert battery.self_consumption_calls == 1
    assert battery.backup_reserve_calls == []
    assert coordinator._last_executed_action == "self_consumption"


def test_self_consumption_does_not_reapply_goodwe_reserve_when_mode_matches(opt_module):
    battery = _FakeBattery(hardware_mode="self_consumption", backup_reserve=20)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.43)
    coordinator.battery_system = "goodwe"
    coordinator._config.backup_reserve = 0.45
    coordinator._startup_backup_reserve = 20

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )

    assert battery.self_consumption_calls == 0
    assert battery.backup_reserve_calls == []
    assert coordinator._last_executed_action == "self_consumption"


def test_self_consumption_reapplies_goodwe_when_battery_is_exporting_to_grid(opt_module):
    battery = _FakeBattery(hardware_mode="self_consumption", backup_reserve=20)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.50)
    coordinator.battery_system = "goodwe"
    coordinator._config.backup_reserve = 0.45
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "grid_power": -5.09,
            "battery_power": 3.48,
        }
    )

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )

    assert battery.self_consumption_calls == 1
    assert battery.backup_reserve_calls == []
    assert coordinator._last_executed_action == "self_consumption"


def test_self_consumption_reapplies_sungrow_when_discharge_is_blocked(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.15)
    coordinator.battery_system = "sungrow"
    coordinator.energy_coordinator = _FakeEnergyCoordinator()
    coordinator.energy_coordinator.discharge_blocked_after_restore = True

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=435)
        )
    )

    assert battery.self_consumption_calls == 1
    assert battery.backup_reserve_calls == []
    assert coordinator._last_executed_action == "self_consumption"


def test_self_consumption_throttles_inferred_sungrow_restore(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.15)
    coordinator.battery_system = "sungrow"
    coordinator.energy_coordinator = _FakeEnergyCoordinator()
    coordinator.energy_coordinator.discharge_blocked_after_restore = True
    now = datetime(2026, 7, 10, 5, 20, tzinfo=timezone.utc)
    opt_module.dt_util.utcnow = lambda *args, **kwargs: now
    action = SimpleNamespace(action="self_consumption", power_w=435)

    asyncio.run(coordinator._execute_optimizer_action(action))
    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.self_consumption_calls == 1
    assert coordinator._last_executed_action == "self_consumption"


def test_explicit_sungrow_forced_mode_bypasses_inferred_restore_cooldown(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.50)
    coordinator.battery_system = "sungrow"
    coordinator.energy_coordinator = _FakeEnergyCoordinator()
    coordinator.energy_coordinator.data = {
        "ems_mode_name": "forced",
        "charge_cmd": 0xBB,
    }
    now = datetime(2026, 7, 10, 5, 20, tzinfo=timezone.utc)
    opt_module.dt_util.utcnow = lambda *args, **kwargs: now
    coordinator._last_sungrow_inferred_restore_at = now
    action = SimpleNamespace(action="self_consumption", power_w=435)

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.self_consumption_calls == 1
    assert coordinator._last_executed_action == "self_consumption"


def test_self_consumption_repairs_sungrow_reserve_when_it_blocks_discharge(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.149)
    coordinator.battery_system = "sungrow"
    coordinator._startup_backup_reserve = 5
    coordinator.energy_coordinator = _FakeEnergyCoordinator()
    coordinator.energy_coordinator.data = {
        "battery_power": 0.0,
        "grid_power": 0.232,
        "load_power": 0.227,
        "battery_level": 14.9,
        "backup_reserve": 15.0,
    }

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=418)
        )
    )

    assert battery.self_consumption_calls == 1
    assert battery.backup_reserve_calls == [5]
    assert coordinator._last_executed_action == "self_consumption"


def test_idle_at_reserve_floor_is_not_overridden_to_self_consumption(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.20)

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="idle", power_w=0)
        )
    )

    assert battery.self_consumption_calls == 1
    assert battery.backup_reserve_calls == [20]
    assert coordinator._last_executed_action == "idle"


def test_goodwe_idle_does_not_write_dod_reserve_hold(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    coordinator.battery_system = "goodwe"
    coordinator._config.backup_reserve = 0.20
    coordinator._startup_backup_reserve = 5

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="idle", power_w=0)
        )
    )

    assert battery.self_consumption_calls == 1
    assert battery.restore_normal_calls == 0
    assert battery.backup_reserve_calls == []
    assert coordinator._pre_idle_backup_reserve is None
    assert coordinator._idle_hold_reserve is None
    assert coordinator._last_executed_action == "idle"


def test_tesla_idle_holds_current_soc_when_below_optimizer_floor(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.32)
    coordinator._config.backup_reserve = 0.50
    coordinator._startup_backup_reserve = 0

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="idle", power_w=0)
        )
    )

    assert battery.self_consumption_calls == 1
    assert battery.backup_reserve_calls == [32]
    assert coordinator._pre_idle_backup_reserve == 0
    assert coordinator._last_executed_action == "idle"


def test_tesla_idle_uses_configured_restore_fallback_when_read_unavailable(opt_module):
    battery = _FakeBattery(backup_reserve=None)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.55)
    coordinator._startup_backup_reserve = None
    coordinator._config.backup_reserve = 0.20

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="idle", power_w=0)
        )
    )

    assert battery.backup_reserve_calls == [55]
    assert coordinator._pre_idle_backup_reserve == 20
    assert coordinator._idle_hold_reserve == 55
    assert coordinator._last_executed_action == "idle"


def test_tesla_idle_failed_reserve_write_keeps_previous_marker(opt_module):
    class FailingIdleBattery(_FakeBattery):
        async def set_backup_reserve(self, percent):
            self.backup_reserve_calls.append(percent)
            return False

    battery = FailingIdleBattery(backup_reserve=15)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.55)
    coordinator._startup_backup_reserve = 15
    coordinator._last_executed_action = "self_consumption"

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="idle", power_w=0)
        )
    )

    assert battery.backup_reserve_calls == [55]
    assert coordinator._pre_idle_backup_reserve == 15
    assert coordinator._idle_hold_reserve is None
    assert coordinator._last_executed_action == "self_consumption"


def test_tesla_idle_restore_false_keeps_idle_marker_and_retries(opt_module):
    class RetryRestoreBattery(_FakeBattery):
        async def set_backup_reserve(self, percent):
            self.backup_reserve_calls.append(percent)
            return len(self.backup_reserve_calls) > 1

    battery = RetryRestoreBattery(backup_reserve=55)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.55)
    coordinator._last_executed_action = "idle"
    coordinator._startup_backup_reserve = 15
    coordinator._pre_idle_backup_reserve = 15
    coordinator._idle_hold_reserve = 55
    action = SimpleNamespace(action="self_consumption", power_w=0)

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.backup_reserve_calls == [15]
    assert coordinator._pre_idle_backup_reserve == 15
    assert coordinator._idle_hold_reserve == 55
    assert coordinator._last_executed_action == "idle"

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.backup_reserve_calls == [15, 15, 15]
    assert coordinator._pre_idle_backup_reserve is None
    assert coordinator._idle_hold_reserve is None
    assert coordinator._last_executed_action == "self_consumption"


def test_charge_executes_immediately_above_reserve(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.50)
    coordinator.battery_system = "foxess"

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="charge", power_w=4200)
        )
    )

    assert battery.force_charge_calls == [(10, 4200, False)]
    assert battery.self_consumption_calls == 0
    assert coordinator._optimizer_force_state["active"] is True
    assert coordinator._optimizer_force_state["type"] == "charge"
    assert coordinator._last_executed_action == "charge"


def test_optimizer_owned_force_charge_holds_when_current_slot_stops_charging(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.50)
    coordinator.battery_system = "foxess"
    start = datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    current_time = {"now": start}
    opt_module.dt_util.utcnow = lambda *args, **kwargs: current_time["now"]
    initial_actions = [
        SimpleNamespace(
            action="charge",
            power_w=23500,
            timestamp=start,
        ),
        SimpleNamespace(
            action="charge",
            power_w=23500,
            timestamp=start + timedelta(minutes=5),
        ),
    ]
    coordinator._current_schedule = SimpleNamespace(actions=initial_actions)

    asyncio.run(coordinator._execute_optimizer_action(initial_actions[0]))
    current_time["now"] = start + timedelta(minutes=5)

    shuffled_actions = [
        SimpleNamespace(
            action="self_consumption",
            power_w=0,
            timestamp=start,
        ),
        SimpleNamespace(
            action="charge",
            power_w=23500,
            timestamp=start + timedelta(minutes=5),
        ),
    ]
    coordinator._current_schedule = SimpleNamespace(actions=shuffled_actions)

    asyncio.run(coordinator._execute_optimizer_action(shuffled_actions[0]))

    assert battery.force_charge_calls == [(10, 23500, False)]
    assert battery.self_consumption_calls == 0
    assert battery.restore_normal_calls == 0
    assert coordinator._optimizer_force_state["active"] is True
    assert coordinator._last_executed_action == "charge"


def test_optimizer_owned_force_discharge_holds_when_future_export_remains(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.60)
    coordinator.battery_system = "foxess"
    start = datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    current_time = {"now": start}
    opt_module.dt_util.utcnow = lambda *args, **kwargs: current_time["now"]
    initial_actions = [
        SimpleNamespace(
            action="export",
            power_w=5000,
            timestamp=start,
        ),
        SimpleNamespace(
            action="export",
            power_w=5000,
            timestamp=start + timedelta(minutes=5),
        ),
    ]
    coordinator._current_schedule = SimpleNamespace(actions=initial_actions)

    asyncio.run(coordinator._execute_optimizer_action(initial_actions[0]))
    current_time["now"] = start + timedelta(minutes=5)

    shuffled_actions = [
        SimpleNamespace(
            action="self_consumption",
            power_w=0,
            timestamp=start + timedelta(minutes=5),
        ),
        SimpleNamespace(
            action="export",
            power_w=5000,
            timestamp=start + timedelta(minutes=10),
        ),
    ]
    coordinator._current_schedule = SimpleNamespace(actions=shuffled_actions)

    asyncio.run(coordinator._execute_optimizer_action(shuffled_actions[0]))

    assert battery.force_discharge_calls == [(10, 5000.0, False, None)]
    assert battery.restore_normal_calls == 0
    assert coordinator._optimizer_force_state["active"] is True
    assert coordinator._last_executed_action == "export"


def test_optimizer_owned_force_discharge_cancels_when_export_window_ends(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.60)
    coordinator.battery_system = "foxess"
    start = datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    current_time = {"now": start}
    opt_module.dt_util.utcnow = lambda *args, **kwargs: current_time["now"]
    initial_actions = [
        SimpleNamespace(
            action="export",
            power_w=5000,
            timestamp=start,
        ),
        SimpleNamespace(
            action="export",
            power_w=5000,
            timestamp=start + timedelta(minutes=5),
        ),
    ]
    coordinator._current_schedule = SimpleNamespace(actions=initial_actions)

    asyncio.run(coordinator._execute_optimizer_action(initial_actions[0]))
    current_time["now"] = start + timedelta(minutes=5)

    end_action = SimpleNamespace(
        action="self_consumption",
        power_w=0,
        timestamp=start + timedelta(minutes=5),
    )
    coordinator._current_schedule = SimpleNamespace(actions=[end_action])

    asyncio.run(coordinator._execute_optimizer_action(end_action))

    assert battery.force_discharge_calls == [(10, 5000.0, False, None)]
    assert battery.restore_normal_calls == 1
    assert coordinator._optimizer_force_state["active"] is False
    assert coordinator._last_executed_action == "self_consumption"


def test_optimizer_owned_force_charge_preserves_commitment_start_on_refresh(opt_module):
    start = datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    current_time = {"now": start}
    opt_module.dt_util.utcnow = lambda *args, **kwargs: current_time["now"]
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.64)
    coordinator.battery_system = "foxess"
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "work_mode_name": "Self Use",
            "battery_power": -3.4,
        }
    )
    actions = [
        SimpleNamespace(
            action="charge",
            power_w=10000,
            timestamp=start + idx * timedelta(minutes=5),
        )
        for idx in range(6)
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)
    coordinator._set_optimizer_force_state("charge", 120, 10000)
    coordinator._optimizer_force_state["hardware_expires_at"] = (
        start + timedelta(minutes=4)
    )

    current_time["now"] = start + timedelta(minutes=10)
    asyncio.run(coordinator._execute_optimizer_action(actions[2]))

    assert battery.force_charge_calls == [(20, 10000, True)]
    assert coordinator._optimizer_force_state["active"] is True
    assert coordinator._optimizer_force_state["started_at"] == start


def test_optimizer_owned_force_charge_reissues_when_foxess_mode_drops_to_self_use(opt_module):
    now = datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    opt_module.dt_util.utcnow = lambda *args, **kwargs: now
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.64)
    coordinator.battery_system = "foxess"
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "work_mode_name": "Self Use",
            "battery_power": -3.4,
        }
    )
    actions = [
        SimpleNamespace(
            action="charge",
            power_w=10000,
            timestamp=now + idx * timedelta(minutes=5),
        )
        for idx in range(6)
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)
    coordinator._set_optimizer_force_state("charge", 120, 10000)
    coordinator._optimizer_force_state["hardware_expires_at"] = now + timedelta(hours=1)

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))

    assert battery.force_charge_calls == [(30, 10000, True)]
    assert coordinator._optimizer_force_state["active"] is True
    assert coordinator._optimizer_force_state["type"] == "charge"


def test_optimizer_owned_force_charge_reissues_when_sungrow_ems_is_not_charging(opt_module):
    now = datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    opt_module.dt_util.utcnow = lambda *args, **kwargs: now
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.64)
    coordinator.battery_system = "sungrow"
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "ems_mode_name": "self_consumption",
            "charge_cmd": 0xCC,
            "battery_power": 13.4,
        }
    )
    actions = [
        SimpleNamespace(
            action="charge",
            power_w=15000,
            timestamp=now + idx * timedelta(minutes=5),
        )
        for idx in range(6)
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)
    coordinator._set_optimizer_force_state("charge", 120, 15000)
    coordinator._optimizer_force_state["hardware_expires_at"] = now + timedelta(hours=1)

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))

    assert battery.force_charge_calls == [(30, 15000, True)]
    assert coordinator._optimizer_force_state["active"] is True
    assert coordinator._optimizer_force_state["type"] == "charge"


def test_optimizer_owned_force_charge_reissues_when_sungrow_mode_is_missing(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.64)
    coordinator.battery_system = "sungrow"
    coordinator.energy_coordinator = SimpleNamespace(
        data={"battery_power": 13.4}
    )

    assert coordinator._force_charge_hardware_needs_refresh(15000) is True


def test_optimizer_owned_force_charge_accepts_sungrow_forced_charge_cmd(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.64)
    coordinator.battery_system = "sungrow"
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "ems_mode_name": "forced",
            "charge_cmd": 0xAA,
            "battery_power": 0,
        }
    )

    assert coordinator._force_charge_hardware_needs_refresh(15000) is False


def test_optimizer_owned_force_discharge_reissues_when_goodwe_stays_self_consumption(opt_module):
    now = datetime(2026, 6, 6, 7, 35, tzinfo=timezone.utc)
    opt_module.dt_util.utcnow = lambda *args, **kwargs: now
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.99)
    coordinator.battery_system = "goodwe"
    coordinator._config.max_discharge_w = 15000
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "ems_mode_name": "self_consumption",
            "battery_power": 0.31,
            "grid_power": 0.0,
        }
    )
    actions = [
        SimpleNamespace(
            action="export",
            power_w=15000,
            timestamp=now + idx * timedelta(minutes=5),
        )
        for idx in range(6)
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)
    coordinator._set_optimizer_force_state("discharge", 120, 15000)
    coordinator._optimizer_force_state["hardware_expires_at"] = now + timedelta(hours=1)

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))

    assert battery.force_discharge_calls == [(30, 15000, True, None)]
    assert coordinator._optimizer_force_state["active"] is True
    assert coordinator._optimizer_force_state["type"] == "discharge"


def test_optimizer_owned_force_discharge_accepts_goodwe_sell_power_mode(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.99)
    coordinator.battery_system = "goodwe"
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "ems_mode_name": "sell_power",
            "battery_power": 0.25,
            "grid_power": 0.0,
        }
    )

    assert coordinator._force_discharge_hardware_needs_refresh(15000) is False


def test_optimizer_owned_force_charge_does_not_override_idle_with_lookahead_charge(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.50)
    coordinator.battery_system = "foxess"
    start = datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    current_time = {"now": start}
    opt_module.dt_util.utcnow = lambda *args, **kwargs: current_time["now"]
    initial_action = SimpleNamespace(
        action="charge",
        power_w=23500,
        timestamp=start,
    )
    coordinator._current_schedule = SimpleNamespace(actions=[initial_action])

    asyncio.run(coordinator._execute_optimizer_action(initial_action))
    coordinator._optimizer_force_state["expires_at"] = start + timedelta(hours=1)
    current_time["now"] = start + timedelta(minutes=21)

    shuffled_actions = [
        SimpleNamespace(
            action="idle",
            power_w=0,
            timestamp=start,
        ),
        SimpleNamespace(
            action="charge",
            power_w=23500,
            timestamp=start + timedelta(minutes=5),
        ),
    ]
    coordinator._current_schedule = SimpleNamespace(actions=shuffled_actions)
    coordinator._optimizer_force_state["hardware_expires_at"] = start

    asyncio.run(coordinator._execute_optimizer_action(shuffled_actions[0]))

    assert battery.force_charge_calls == [(5, 23500, False)]
    assert battery.restore_normal_calls == 1
    assert coordinator._optimizer_force_state["active"] is False
    assert coordinator._last_executed_action == "idle"


def test_optimizer_owned_force_charge_clears_when_lp_really_stops_charging(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.50)
    coordinator.battery_system = "foxess"
    start = datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    current_time = {"now": start}
    opt_module.dt_util.utcnow = lambda *args, **kwargs: current_time["now"]
    charge_action = SimpleNamespace(action="charge", power_w=23500, timestamp=start)
    coordinator._current_schedule = SimpleNamespace(actions=[charge_action])

    asyncio.run(coordinator._execute_optimizer_action(charge_action))
    coordinator._optimizer_force_state["expires_at"] = start + timedelta(hours=1)
    current_time["now"] = start + timedelta(minutes=21)

    stop_actions = [
        SimpleNamespace(
            action="self_consumption",
            power_w=0,
            timestamp=start,
        ),
        SimpleNamespace(
            action="self_consumption",
            power_w=0,
            timestamp=start + timedelta(minutes=5),
        ),
    ]
    coordinator._current_schedule = SimpleNamespace(actions=stop_actions)

    asyncio.run(coordinator._execute_optimizer_action(stop_actions[0]))

    assert battery.restore_normal_calls == 1
    assert battery.self_consumption_calls == 1
    assert coordinator._optimizer_force_state["active"] is False
    assert coordinator._last_executed_action == "self_consumption"


def test_charge_duration_clips_at_next_lp_action_boundary(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.95)
    coordinator.battery_system = "foxess"
    start = datetime(2026, 5, 3, 7, 25, tzinfo=timezone.utc)
    actions = [
        SimpleNamespace(
            action="charge",
            power_w=23500,
            timestamp=start,
        ),
        SimpleNamespace(
            action="export",
            power_w=23600,
            timestamp=start + timedelta(minutes=5),
        ),
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))

    assert battery.force_charge_calls == [(5, 23500, False)]
    assert coordinator._last_executed_action == "charge"


def test_contiguous_charge_duration_uses_full_lp_block(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.50)
    coordinator.battery_system = "foxess"
    start = datetime(2026, 5, 3, 7, 10, tzinfo=timezone.utc)
    actions = [
        SimpleNamespace(
            action="charge",
            power_w=12000,
            timestamp=start + idx * timedelta(minutes=5),
        )
        for idx in range(4)
    ]
    actions.append(
        SimpleNamespace(
            action="export",
            power_w=12000,
            timestamp=start + timedelta(minutes=20),
        )
    )
    coordinator._current_schedule = SimpleNamespace(actions=actions)

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))

    assert battery.force_charge_calls == [(20, 12000, False)]
    assert coordinator._last_executed_action == "charge"


def test_idle_to_self_consumption_exits_idle_immediately(opt_module):
    battery = _FakeBattery()
    energy_coordinator = _FakeEnergyCoordinator()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.50)
    coordinator._last_executed_action = "idle"
    coordinator._pre_idle_backup_reserve = 47
    coordinator.energy_coordinator = energy_coordinator

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )

    assert energy_coordinator.restore_work_mode_from_idle_calls == 1
    assert battery.self_consumption_calls == 1
    assert battery.backup_reserve_calls == [47, 20]
    assert coordinator._pre_idle_backup_reserve is None
    assert coordinator._last_executed_action == "self_consumption"


def test_foxess_idle_exit_restores_user_reserve_without_applying_optimizer_floor(opt_module):
    battery = _FakeBattery()
    energy_coordinator = _FakeEnergyCoordinator()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.50)
    coordinator.battery_system = "foxess"
    coordinator._config.backup_reserve = 0.45
    coordinator._startup_backup_reserve = 15
    coordinator._last_executed_action = "idle"
    coordinator._pre_idle_backup_reserve = 15
    coordinator._idle_hold_reserve = 100
    coordinator.energy_coordinator = energy_coordinator

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )

    assert energy_coordinator.restore_work_mode_from_idle_calls == 1
    assert battery.self_consumption_calls == 1
    assert battery.backup_reserve_calls == [15]
    assert coordinator._pre_idle_backup_reserve is None
    assert coordinator._idle_hold_reserve is None
    assert coordinator._last_executed_action == "self_consumption"


def test_tesla_export_uses_contiguous_export_window_duration(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    start = datetime(2026, 5, 3, 18, 30, tzinfo=timezone.utc)
    actions = [
        SimpleNamespace(
            action="export",
            power_w=4200,
            timestamp=start + idx * timedelta(minutes=5),
        )
        for idx in range(8)
    ]
    actions.append(
        SimpleNamespace(
            action="self_consumption",
            power_w=0,
            timestamp=start + timedelta(minutes=40),
        )
    )
    coordinator._current_schedule = SimpleNamespace(actions=actions)

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))

    assert battery.force_discharge_calls == [(40, 5000, False, None)]
    assert coordinator._last_executed_action == "export"


def test_single_slot_self_consumption_gap_between_exports_is_bridged(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    coordinator._config.max_discharge_w = 10000
    start = datetime(2026, 5, 3, 18, 30, tzinfo=timezone.utc)
    actions = [
        SimpleNamespace(
            action="export",
            power_w=9000,
            battery_charge_w=0,
            battery_discharge_w=9000,
            timestamp=start,
        ),
        SimpleNamespace(
            action="self_consumption",
            power_w=1200,
            battery_charge_w=0,
            battery_discharge_w=1200,
            timestamp=start + timedelta(minutes=5),
        ),
        SimpleNamespace(
            action="export",
            power_w=7000,
            battery_charge_w=0,
            battery_discharge_w=7000,
            timestamp=start + timedelta(minutes=10),
        ),
    ]
    schedule = SimpleNamespace(actions=actions)

    coordinator._bridge_short_export_gaps(schedule, [0.45, 0.45, 0.45])

    assert [action.action for action in actions] == ["export", "export", "export"]
    assert actions[1].power_w == 7000
    assert actions[1].battery_discharge_w == 8200


def test_single_slot_export_gap_is_not_bridged_below_reserve(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.32)
    coordinator._config.backup_reserve = 0.30
    coordinator._config.battery_capacity_wh = 10000
    coordinator._optimizer = SimpleNamespace(efficiency=1.0)
    start = datetime(2026, 5, 3, 18, 30, tzinfo=timezone.utc)
    actions = [
        SimpleNamespace(
            action="export",
            power_w=9000,
            battery_charge_w=0,
            battery_discharge_w=9000,
            soc=0.32,
            timestamp=start,
        ),
        SimpleNamespace(
            action="self_consumption",
            power_w=1200,
            battery_charge_w=0,
            battery_discharge_w=1200,
            soc=0.32,
            timestamp=start + timedelta(minutes=5),
        ),
        SimpleNamespace(
            action="export",
            power_w=7000,
            battery_charge_w=0,
            battery_discharge_w=7000,
            soc=0.31,
            timestamp=start + timedelta(minutes=10),
        ),
    ]
    schedule = SimpleNamespace(actions=actions)

    coordinator._bridge_short_export_gaps(schedule, [0.45, 0.45, 0.45])

    assert [action.action for action in actions] == [
        "export",
        "self_consumption",
        "export",
    ]
    assert actions[1].power_w == 1200
    assert actions[1].battery_discharge_w == 1200
    assert actions[1].soc == 0.32


def test_single_slot_export_gap_respects_transient_export_floor(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.88)
    coordinator._config.backup_reserve = 0.06
    coordinator._config.battery_capacity_wh = 32000
    coordinator._optimizer = SimpleNamespace(efficiency=0.92)
    start = datetime(2026, 5, 3, 17, 30, tzinfo=timezone.utc)
    actions = [
        SimpleNamespace(
            action="export",
            power_w=15000,
            battery_charge_w=0,
            battery_discharge_w=15000,
            soc=0.91,
            timestamp=start,
        ),
        SimpleNamespace(
            action="self_consumption",
            power_w=900,
            battery_charge_w=0,
            battery_discharge_w=900,
            soc=0.90,
            timestamp=start + timedelta(minutes=5),
        ),
        SimpleNamespace(
            action="export",
            power_w=15000,
            battery_charge_w=0,
            battery_discharge_w=15000,
            soc=0.88,
            timestamp=start + timedelta(minutes=10),
        ),
    ]
    schedule = SimpleNamespace(actions=actions)

    coordinator._bridge_short_export_gaps(
        schedule,
        [0.45, 0.45, 0.45],
        export_reserve_floor=0.90,
    )

    assert [action.action for action in actions] == [
        "export",
        "self_consumption",
        "export",
    ]
    assert actions[1].power_w == 900
    assert actions[1].battery_discharge_w == 900
    assert actions[1].soc == 0.90


def test_multi_slot_self_consumption_gap_between_exports_is_not_bridged(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    start = datetime(2026, 5, 3, 18, 30, tzinfo=timezone.utc)
    actions = [
        SimpleNamespace(action="export", power_w=9000, timestamp=start),
        SimpleNamespace(
            action="self_consumption",
            power_w=1200,
            timestamp=start + timedelta(minutes=5),
        ),
        SimpleNamespace(
            action="self_consumption",
            power_w=1300,
            timestamp=start + timedelta(minutes=10),
        ),
        SimpleNamespace(
            action="export",
            power_w=7000,
            timestamp=start + timedelta(minutes=15),
        ),
    ]
    schedule = SimpleNamespace(actions=actions)

    coordinator._bridge_short_export_gaps(schedule, [0.45, 0.45, 0.45, 0.45])

    assert [action.action for action in actions] == [
        "export",
        "self_consumption",
        "self_consumption",
        "export",
    ]


def test_flow_power_no_idle_converts_schedule_idle_to_self_consumption(opt_module):
    coordinator = _coordinator(opt_module, "flow_power")
    coordinator._config.disable_idle_enabled = True
    start = datetime(2026, 5, 3, 18, 30, tzinfo=timezone.utc)
    charge_action = opt_module.ScheduleAction(
        timestamp=start + timedelta(minutes=5),
        action="charge",
        power_w=5000,
        soc=0.66,
        battery_charge_w=5000,
        battery_discharge_w=0,
    )
    schedule = opt_module.OptimizationSchedule(
        actions=[
            opt_module.ScheduleAction(
                timestamp=start,
                action="idle",
                power_w=0,
                soc=0.65,
                battery_charge_w=0,
                battery_discharge_w=0,
            ),
            charge_action,
        ],
        predicted_cost=1.23,
        predicted_savings=0.45,
        last_updated=start,
    )

    converted = coordinator._disable_idle_schedule(schedule)

    assert coordinator._should_disable_idle_schedule() is True
    assert converted.actions[0].action == "self_consumption"
    assert converted.actions[0].soc == 0.65
    assert converted.actions[0].power_w == 0
    assert converted.actions[1] is charge_action
    assert converted.predicted_cost == schedule.predicted_cost


def test_flow_power_no_idle_schedule_simulates_home_load(opt_module):
    coordinator = _coordinator(opt_module, "flow_power")
    coordinator._config.disable_idle_enabled = True
    coordinator._config.battery_capacity_wh = 13500
    coordinator._config.max_discharge_w = 5000
    coordinator._config.backup_reserve = 0.2

    start = datetime(2026, 5, 3, 17, 30, tzinfo=timezone.utc)
    schedule = opt_module.OptimizationSchedule(
        actions=[
            opt_module.ScheduleAction(
                timestamp=start,
                action="idle",
                power_w=0,
                soc=0.65,
                battery_charge_w=0,
                battery_discharge_w=0,
            ),
            opt_module.ScheduleAction(
                timestamp=start + timedelta(minutes=5),
                action="idle",
                power_w=0,
                soc=0.65,
                battery_charge_w=0,
                battery_discharge_w=0,
            ),
        ],
        predicted_cost=1.23,
        predicted_savings=0.45,
        last_updated=start,
    )

    converted = coordinator._disable_idle_schedule(
        schedule,
        solar_forecast=[0.0, 0.0],
        load_forecast=[2.0, 2.0],
        initial_soc=0.65,
    )

    assert [action.action for action in converted.actions] == [
        "self_consumption",
        "self_consumption",
    ]
    assert [action.battery_discharge_w for action in converted.actions] == [
        2000.0,
        2000.0,
    ]
    assert [action.power_w for action in converted.actions] == [2000.0, 2000.0]
    assert converted.actions[0].soc < 0.65
    assert converted.actions[1].soc < converted.actions[0].soc


def test_no_idle_uses_self_consumption_at_hardware_floor_before_recovery_charge(
    opt_module,
):
    coordinator = _coordinator(opt_module, "flow_power")
    coordinator._config.disable_idle_enabled = True
    coordinator._config.battery_capacity_wh = 42000
    coordinator._config.max_discharge_w = 10000
    coordinator._config.backup_reserve = 0.35
    coordinator._startup_backup_reserve = 10

    start = datetime(2026, 7, 10, 3, 30, tzinfo=timezone.utc)
    schedule = opt_module.OptimizationSchedule(
        actions=[
            opt_module.ScheduleAction(
                timestamp=start,
                action="idle",
                power_w=0,
                soc=0.10,
                battery_charge_w=0,
                battery_discharge_w=0,
            ),
            opt_module.ScheduleAction(
                timestamp=start + timedelta(minutes=5),
                action="idle",
                power_w=0,
                soc=0.10,
                battery_charge_w=0,
                battery_discharge_w=0,
            ),
            opt_module.ScheduleAction(
                timestamp=start + timedelta(minutes=10),
                action="charge",
                power_w=10000,
                soc=0.13,
                battery_charge_w=10000,
                battery_discharge_w=0,
            ),
        ],
        predicted_cost=1.23,
        predicted_savings=0.45,
        last_updated=start,
    )

    converted = coordinator._disable_idle_schedule(
        schedule,
        solar_forecast=[0.0, 0.0, 0.0],
        load_forecast=[5.0, 5.0, 5.0],
        initial_soc=0.10,
    )

    assert [action.action for action in converted.actions] == [
        "self_consumption",
        "self_consumption",
        "charge",
    ]
    assert [action.soc for action in converted.actions] == [0.10, 0.10, 0.1188]
    assert converted.actions[0].battery_discharge_w == 0
    assert converted.actions[1].battery_discharge_w == 0


def test_no_idle_preserves_charge_by_time_prefill_hold(opt_module):
    coordinator = _coordinator(
        opt_module,
        "octopus",
        charge_by_time=True,
        charge_by_time_target_time="16:01",
        charge_by_time_target_soc=1.0,
    )
    coordinator._config.disable_idle_enabled = True
    coordinator._config.battery_capacity_wh = 47900
    coordinator._config.max_discharge_w = 23000
    coordinator._config.backup_reserve = 0.15

    start = datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    schedule = opt_module.OptimizationSchedule(
        actions=[
            opt_module.ScheduleAction(
                timestamp=start,
                action="self_consumption",
                power_w=0,
                soc=0.80,
                battery_charge_w=0,
                battery_discharge_w=0,
            ),
            opt_module.ScheduleAction(
                timestamp=start + timedelta(minutes=5),
                action="idle",
                power_w=0,
                soc=0.80,
                battery_charge_w=0,
                battery_discharge_w=0,
            ),
        ],
        predicted_cost=1.23,
        predicted_savings=0.45,
        last_updated=start,
    )

    converted = coordinator._disable_idle_schedule(
        schedule,
        solar_forecast=[0.0, 0.0],
        load_forecast=[2.0, 2.0],
        initial_soc=0.80,
    )

    assert coordinator._next_charge_by_time_target_slot() == 90
    assert [action.action for action in converted.actions] == [
        "self_consumption",
        "idle",
    ]
    assert [action.battery_discharge_w for action in converted.actions] == [0.0, 0.0]
    assert [action.soc for action in converted.actions] == [0.80, 0.80]


def test_flow_power_no_idle_schedule_fills_zero_self_consumption_after_export(opt_module):
    coordinator = _coordinator(opt_module, "flow_power")
    coordinator._config.disable_idle_enabled = True
    coordinator._config.battery_capacity_wh = 10000
    coordinator._config.max_discharge_w = 5000
    coordinator._config.backup_reserve = 0.30
    coordinator._startup_backup_reserve = 5

    start = datetime(2026, 5, 3, 18, 45, tzinfo=timezone.utc)
    schedule = opt_module.OptimizationSchedule(
        actions=[
            opt_module.ScheduleAction(
                timestamp=start,
                action="export",
                power_w=5000,
                soc=0.30,
                battery_charge_w=0,
                battery_discharge_w=5000,
            ),
            opt_module.ScheduleAction(
                timestamp=start + timedelta(minutes=5),
                action="self_consumption",
                power_w=0,
                soc=0.05,
                battery_charge_w=0,
                battery_discharge_w=0,
            ),
            opt_module.ScheduleAction(
                timestamp=start + timedelta(minutes=10),
                action="self_consumption",
                power_w=0,
                soc=0.05,
                battery_charge_w=0,
                battery_discharge_w=0,
            ),
        ],
        predicted_cost=1.23,
        predicted_savings=0.45,
        last_updated=start,
    )

    converted = coordinator._disable_idle_schedule(
        schedule,
        solar_forecast=[0.0, 0.0, 0.0],
        load_forecast=[0.0, 2.0, 2.0],
        initial_soc=0.30,
    )

    assert [action.action for action in converted.actions] == [
        "export",
        "self_consumption",
        "self_consumption",
    ]
    assert converted.actions[1].battery_discharge_w == 2000.0
    assert converted.actions[2].battery_discharge_w == 2000.0
    assert converted.actions[1].soc > 0.05
    assert converted.actions[2].soc > 0.05
    assert converted.actions[2].soc < converted.actions[1].soc


def test_flow_power_no_idle_schedule_keeps_export_soc_continuous(opt_module):
    coordinator = _coordinator(opt_module, "flow_power")
    coordinator._config.disable_idle_enabled = True
    coordinator._config.battery_capacity_wh = 10000
    coordinator._config.max_discharge_w = 5000
    coordinator._config.backup_reserve = 0.30
    coordinator._startup_backup_reserve = 5

    start = datetime(2026, 5, 3, 17, 20, tzinfo=timezone.utc)
    schedule = opt_module.OptimizationSchedule(
        actions=[
            opt_module.ScheduleAction(
                timestamp=start,
                action="self_consumption",
                power_w=0,
                soc=0.40,
                battery_charge_w=0,
                battery_discharge_w=0,
            ),
            opt_module.ScheduleAction(
                timestamp=start + timedelta(minutes=5),
                action="self_consumption",
                power_w=0,
                soc=0.40,
                battery_charge_w=0,
                battery_discharge_w=0,
            ),
            opt_module.ScheduleAction(
                timestamp=start + timedelta(minutes=10),
                action="export",
                power_w=5000,
                soc=0.90,
                battery_charge_w=0,
                battery_discharge_w=5000,
            ),
        ],
        predicted_cost=1.23,
        predicted_savings=0.45,
        last_updated=start,
    )

    converted = coordinator._disable_idle_schedule(
        schedule,
        solar_forecast=[0.0, 0.0, 0.0],
        load_forecast=[2.0, 2.0, 0.0],
        initial_soc=0.40,
    )

    assert converted.actions[2].action == "export"
    assert converted.actions[2].soc != 0.90
    assert converted.actions[2].soc < converted.actions[1].soc
    assert converted.actions[2].soc > 0.05


def test_flow_power_no_idle_schedule_uses_hardware_floor_for_home_load(opt_module):
    coordinator = _coordinator(opt_module, "flow_power")
    coordinator._config.disable_idle_enabled = True
    coordinator._config.battery_capacity_wh = 13500
    coordinator._config.max_discharge_w = 5000
    coordinator._config.backup_reserve = 0.20
    coordinator._startup_backup_reserve = 5

    start = datetime(2026, 5, 3, 17, 30, tzinfo=timezone.utc)
    schedule = opt_module.OptimizationSchedule(
        actions=[
            opt_module.ScheduleAction(
                timestamp=start,
                action="idle",
                power_w=0,
                soc=0.205,
                battery_charge_w=0,
                battery_discharge_w=0,
            ),
        ],
        predicted_cost=1.23,
        predicted_savings=0.45,
        last_updated=start,
    )

    converted = coordinator._disable_idle_schedule(
        schedule,
        solar_forecast=[0.0],
        load_forecast=[2.0],
        initial_soc=0.205,
    )

    assert converted.actions[0].action == "self_consumption"
    assert converted.actions[0].battery_discharge_w == 2000.0
    assert converted.actions[0].soc < 0.20


@pytest.mark.parametrize(
    "provider",
    ["flow_power", "globird", "aemo_vpp", "other", "tou_only", "nz"],
)
def test_no_idle_schedule_guard_supports_tou_providers(opt_module, provider):
    coordinator = _coordinator(opt_module, provider)
    coordinator._config.disable_idle_enabled = True

    assert coordinator._should_disable_idle_schedule() is True


@pytest.mark.parametrize("provider", ["amber", "octopus", "epex", "localvolts"])
def test_no_idle_schedule_guard_blocks_unsupported_providers(opt_module, provider):
    coordinator = _coordinator(opt_module, provider)
    coordinator._config.disable_idle_enabled = True

    assert coordinator._should_disable_idle_schedule() is False


def test_no_idle_setting_coerces_unsupported_provider_false(opt_module):
    coordinator = _coordinator(opt_module, "amber")
    updates, run_calls, background_tasks = _prepare_enabled_settings_coordinator(
        coordinator
    )

    result = asyncio.run(coordinator.set_settings({"disable_idle_enabled": True}))

    assert result["success"] is True
    assert result["changes"] == []
    assert coordinator._config.disable_idle_enabled is False
    assert coordinator.disable_idle_enabled is False
    assert updates == []
    assert run_calls == []
    assert background_tasks == []


def test_no_idle_setting_clears_stale_unsupported_provider_value(opt_module):
    coordinator = _coordinator(opt_module, "amber")
    coordinator._config.disable_idle_enabled = True
    updates, run_calls, background_tasks = _prepare_enabled_settings_coordinator(
        coordinator
    )

    result = asyncio.run(coordinator.set_settings({"disable_idle_enabled": True}))

    assert result["success"] is True
    assert result["changes"] == ["disable_idle_enabled: False"]
    assert coordinator._config.disable_idle_enabled is False
    assert updates[-1]["options"]["optimization_disable_idle"] is False
    assert run_calls == []
    assert background_tasks == ["powersync_settings_reoptimize"]


def test_flow_power_no_idle_executor_preserves_solver_exempt_idle(opt_module):
    battery = _FakeBattery(backup_reserve=20)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    coordinator._entry.options["electricity_provider"] = "flow_power"
    coordinator._config.disable_idle_enabled = True
    coordinator._last_executed_action = "export"

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="idle", power_w=0)
        )
    )

    assert battery.self_consumption_calls == 1
    assert battery.backup_reserve_calls == [80]
    assert coordinator._idle_hold_reserve == 80
    assert coordinator._last_executed_action == "idle"


def test_flow_power_no_idle_monitoring_reports_solver_exempt_idle(opt_module, caplog):
    battery = _FakeBattery(backup_reserve=20)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    coordinator._entry.data["monitoring_mode"] = True
    coordinator._entry.options["electricity_provider"] = "flow_power"
    coordinator._config.disable_idle_enabled = True
    coordinator._last_executed_action = "export"

    with caplog.at_level(logging.INFO):
        asyncio.run(
            coordinator._execute_optimizer_action(
                SimpleNamespace(action="idle", power_w=0)
            )
        )

    assert battery.self_consumption_calls == 0
    assert coordinator._last_executed_action == "export"
    assert "Optimizer would execute: idle" in caplog.text
    assert "Optimizer would execute: self_consumption" not in caplog.text


def test_single_slot_export_gap_with_price_change_is_not_bridged(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    start = datetime(2026, 5, 3, 18, 30, tzinfo=timezone.utc)
    actions = [
        SimpleNamespace(action="export", power_w=9000, timestamp=start),
        SimpleNamespace(
            action="self_consumption",
            power_w=1200,
            timestamp=start + timedelta(minutes=5),
        ),
        SimpleNamespace(
            action="export",
            power_w=7000,
            timestamp=start + timedelta(minutes=10),
        ),
    ]
    schedule = SimpleNamespace(actions=actions)

    coordinator._bridge_short_export_gaps(schedule, [0.45, 0.05, 0.45])

    assert [action.action for action in actions] == [
        "export",
        "self_consumption",
        "export",
    ]


def test_dynamic_price_provider_single_slot_export_gap_is_not_bridged(opt_module):
    class AmberPriceCoordinator:
        pass

    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    coordinator.price_coordinator = AmberPriceCoordinator()
    start = datetime(2026, 5, 3, 18, 30, tzinfo=timezone.utc)
    actions = [
        SimpleNamespace(action="export", power_w=9000, timestamp=start),
        SimpleNamespace(
            action="self_consumption",
            power_w=1200,
            timestamp=start + timedelta(minutes=5),
        ),
        SimpleNamespace(
            action="export",
            power_w=7000,
            timestamp=start + timedelta(minutes=10),
        ),
    ]
    schedule = SimpleNamespace(actions=actions)

    coordinator._bridge_short_export_gaps(schedule, [0.45, 0.45, 0.45])

    assert [action.action for action in actions] == [
        "export",
        "self_consumption",
        "export",
    ]


def test_bridged_export_gap_keeps_optimizer_force_active(opt_module):
    now = datetime(2026, 5, 3, 18, 30, tzinfo=timezone.utc)
    opt_module.dt_util.now = lambda *args, **kwargs: now
    opt_module.dt_util.utcnow = lambda *args, **kwargs: now
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    actions = [
        SimpleNamespace(action="export", power_w=9000, timestamp=now),
        SimpleNamespace(
            action="self_consumption",
            power_w=1200,
            timestamp=now + timedelta(minutes=5),
        ),
        SimpleNamespace(
            action="export",
            power_w=7000,
            timestamp=now + timedelta(minutes=10),
        ),
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)
    coordinator._bridge_short_export_gaps(
        coordinator._current_schedule,
        [0.45, 0.45, 0.45],
    )
    coordinator._set_optimizer_force_state("discharge", 30, 5000)
    coordinator._last_executed_action = "export"

    asyncio.run(coordinator._execute_optimizer_action(actions[1]))

    assert battery.restore_normal_calls == 0
    assert coordinator._optimizer_force_state["active"] is True
    assert coordinator._last_executed_action == "export"


def test_export_command_power_respects_grid_export_cap(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    coordinator.battery_system = "sigenergy"
    coordinator._config.max_discharge_w = 15000
    coordinator._config.max_grid_export_w = 5000
    start = datetime(2026, 5, 3, 18, 30, tzinfo=timezone.utc)
    actions = [
        SimpleNamespace(
            action="export",
            power_w=14000,
            timestamp=start + idx * timedelta(minutes=5),
        )
        for idx in range(3)
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))

    assert battery.force_discharge_calls == [(15, 5000, False, None)]


def test_goodwe_export_command_uses_site_export_target_for_ems_limit(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    coordinator.battery_system = "goodwe"
    coordinator._config.max_discharge_w = 15000
    coordinator._config.max_grid_export_w = 5000
    start = datetime(2026, 5, 3, 18, 30, tzinfo=timezone.utc)
    actions = [
        SimpleNamespace(
            action="export",
            power_w=5000,
            battery_discharge_w=7000,
            timestamp=start + idx * timedelta(minutes=5),
        )
        for idx in range(3)
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))

    assert battery.force_discharge_calls == [(15, 5000, False, None)]


@pytest.mark.parametrize(
    "battery_system",
    [
        "goodwe",
        "sigenergy",
        "sungrow",
        "foxess",
        "alphaess",
        "solax",
        "saj_h2",
        "fronius_reserva",
        "neovolt",
    ],
)
def test_target_export_battery_uses_planned_action_power_without_spread(
    opt_module,
    battery_system,
):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    coordinator.battery_system = battery_system
    coordinator._config.max_discharge_w = 5000
    coordinator._config.spread_export_enabled = False
    start = datetime(2026, 5, 3, 18, 30, tzinfo=timezone.utc)
    actions = [
        SimpleNamespace(
            action="export",
            power_w=1000,
            timestamp=start + idx * timedelta(minutes=5),
        )
        for idx in range(3)
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))

    assert battery.force_discharge_calls == [(15, 1000, False, None)]


@pytest.mark.parametrize("battery_system", ["tesla", "esy_sunhome"])
def test_non_target_export_battery_keeps_max_discharge_command(opt_module, battery_system):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    coordinator.battery_system = battery_system
    coordinator._config.max_discharge_w = 5000
    coordinator._config.max_grid_export_w = 1000
    start = datetime(2026, 5, 3, 18, 30, tzinfo=timezone.utc)
    actions = [
        SimpleNamespace(
            action="export",
            power_w=1000,
            timestamp=start + idx * timedelta(minutes=5),
        )
        for idx in range(3)
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))

    assert battery.force_discharge_calls == [(15, 5000, False, None)]


def test_grid_export_cap_resolves_from_sigenergy_config(opt_module):
    coordinator = _coordinator(
        opt_module,
        "amber",
        sigenergy_export_limit_kw=5,
    )

    assert coordinator._resolve_max_grid_export_w() == 5000


def test_grid_export_cap_prefers_explicit_optimizer_config(opt_module):
    coordinator = _coordinator(
        opt_module,
        "amber",
        optimization_max_grid_export_w=0,
        sigenergy_export_limit_kw=5,
    )

    assert coordinator._resolve_max_grid_export_w() == 0


def test_grid_export_cap_resolves_from_energy_data(opt_module):
    coordinator = _coordinator(opt_module, "amber")
    coordinator.energy_coordinator = SimpleNamespace(data={"export_limit_kw": 4.6})

    assert coordinator._resolve_max_grid_export_w() == 4600


def test_curtailed_sigenergy_zero_export_limit_is_not_planning_cap(opt_module):
    coordinator = _coordinator(opt_module, "amber")
    coordinator.energy_coordinator = SimpleNamespace(
        data={"export_limit_kw": 0, "is_curtailed": True}
    )

    assert coordinator._resolve_max_grid_export_w() is None


def test_target_export_sync_uses_physical_discharge_and_user_export_cap(opt_module):
    coordinator = _coordinator(opt_module, "globird")
    coordinator.battery_system = "goodwe"
    coordinator._config.max_discharge_w = 1000
    coordinator.energy_coordinator = SimpleNamespace(data={"rated_power_w": 16384})
    updates = {}

    class _Optimizer:
        def update_config(self, **kwargs):
            updates.update(kwargs)

    coordinator._optimizer = _Optimizer()

    coordinator._sync_optimizer_discharge_limits()

    assert updates["max_discharge_w"] == 16384
    assert updates["max_battery_export_w"] == 1000


def test_target_export_sync_uses_grid_export_cap_as_command_cap(opt_module):
    coordinator = _coordinator(opt_module, "globird")
    coordinator.battery_system = "goodwe"
    coordinator._config.max_discharge_w = 10600
    coordinator._config.max_grid_export_w = 5500
    coordinator.energy_coordinator = SimpleNamespace(data={"rated_power_w": 16384})
    updates = {}

    class _Optimizer:
        def update_config(self, **kwargs):
            updates.update(kwargs)

    coordinator._optimizer = _Optimizer()

    coordinator._sync_optimizer_discharge_limits()

    assert updates["max_discharge_w"] == 16384
    assert updates["max_battery_export_w"] == 5500


def test_spread_export_schedule_flattens_planned_energy_across_allowed_window(opt_module):
    coordinator = _coordinator(opt_module, "octopus")
    coordinator.battery_system = "goodwe"
    coordinator._config.spread_export_enabled = True
    coordinator._config.max_discharge_w = 5000
    start = datetime(2026, 5, 3, 9, 0, tzinfo=timezone.utc)
    actions = [
        opt_module.ScheduleAction(
            timestamp=start + idx * timedelta(minutes=5),
            action="export" if idx < 2 else "self_consumption",
            power_w=5000 if idx < 2 else 0,
            battery_discharge_w=5000 if idx < 2 else 0,
        )
        for idx in range(6)
    ]
    schedule = opt_module.OptimizationSchedule(
        actions=actions,
        predicted_cost=0,
        predicted_savings=0,
        last_updated=start,
    )

    spread = coordinator._spread_export_schedule(schedule, [True] * 6)

    assert {action.action for action in spread.actions} == {"export"}
    assert [action.power_w for action in spread.actions] == [1666.7] * 6
    original_wh = sum(action.battery_discharge_w for action in actions) * (5 / 60)
    spread_wh = sum(action.battery_discharge_w for action in spread.actions) * (5 / 60)
    assert spread_wh == pytest.approx(original_wh, abs=0.1)


def test_spread_export_schedule_caps_target_at_grid_export_limit(opt_module):
    coordinator = _coordinator(opt_module, "octopus")
    coordinator.battery_system = "goodwe"
    coordinator._config.spread_export_enabled = True
    coordinator._config.max_discharge_w = 10000
    coordinator._config.max_grid_export_w = 1000
    start = datetime(2026, 5, 3, 9, 0, tzinfo=timezone.utc)
    actions = [
        opt_module.ScheduleAction(
            timestamp=start + idx * timedelta(minutes=5),
            action="export" if idx < 2 else "self_consumption",
            power_w=5000 if idx < 2 else 0,
            battery_discharge_w=5000 if idx < 2 else 0,
        )
        for idx in range(6)
    ]
    schedule = opt_module.OptimizationSchedule(
        actions=actions,
        predicted_cost=0,
        predicted_savings=0,
        last_updated=start,
    )

    spread = coordinator._spread_export_schedule(schedule, [True] * 6)

    assert {action.action for action in spread.actions} == {"export"}
    assert [action.power_w for action in spread.actions] == [1000.0] * 6


def test_spread_export_schedule_respects_auto_reserve_export_floor(opt_module):
    coordinator = _coordinator(opt_module, "flow_power", profit_max=True)
    coordinator.battery_system = "goodwe"
    coordinator._config.spread_export_enabled = True
    coordinator._config.max_discharge_w = 5000
    start = datetime(2026, 5, 3, 17, 30, tzinfo=timezone.utc)
    actions = [
        opt_module.ScheduleAction(
            timestamp=start + idx * timedelta(minutes=5),
            action="export" if idx < 3 else "self_consumption",
            power_w=5000 if idx < 3 else 0,
            soc=[0.74, 0.65, 0.56, 0.42, 0.25, 0.05][idx],
            battery_discharge_w=5000 if idx < 3 else 0,
        )
        for idx in range(6)
    ]
    schedule = opt_module.OptimizationSchedule(
        actions=actions,
        predicted_cost=0,
        predicted_savings=0,
        last_updated=start,
    )

    spread = coordinator._spread_export_schedule(
        schedule,
        [True] * 6,
        export_reserve_floor=0.56,
    )

    assert [action.action for action in spread.actions] == ["export"] * 6
    assert [action.power_w for action in spread.actions] == [2500.0] * 6
    assert min(
        action.soc for action in spread.actions if action.action == "export"
    ) >= 0.56
    original_wh = sum(action.power_w for action in actions) * (5 / 60)
    spread_wh = sum(action.power_w for action in spread.actions) * (5 / 60)
    assert spread_wh == pytest.approx(original_wh, abs=0.1)


def test_spread_export_uses_full_window_when_lp_soc_already_reached_floor(opt_module):
    coordinator = _coordinator(opt_module, "globird", profit_max=True)
    coordinator.battery_system = "sigenergy"
    coordinator._config.spread_export_enabled = True
    coordinator._config.battery_capacity_wh = 32200
    coordinator._config.max_discharge_w = 5000
    coordinator._config.max_grid_export_w = 5000
    coordinator._optimizer = SimpleNamespace(efficiency=1.0)
    start = datetime(2026, 7, 14, 18, 0, tzinfo=timezone.utc)
    actions = [
        opt_module.ScheduleAction(
            timestamp=start - timedelta(minutes=5),
            action="self_consumption",
            power_w=0,
            soc=0.99,
            battery_discharge_w=0,
        )
    ]
    actions.extend(
        opt_module.ScheduleAction(
            timestamp=start + idx * timedelta(minutes=5),
            action="export" if idx < 18 else "self_consumption",
            power_w=5000 if idx < 18 else 0,
            soc=(0.99 - (0.48 * idx / 17)) if idx < 18 else 0.51,
            battery_discharge_w=5000 if idx < 18 else 0,
        )
        for idx in range(36)
    )
    schedule = opt_module.OptimizationSchedule(
        actions=actions,
        predicted_cost=0,
        predicted_savings=0,
        last_updated=start,
    )

    spread = coordinator._spread_export_schedule(
        schedule,
        [False] + [True] * 36,
        export_reserve_floor=0.51,
    )

    assert [action.action for action in spread.actions[1:]] == ["export"] * 36
    assert [action.power_w for action in spread.actions[1:]] == [2500.0] * 36
    assert min(action.soc for action in spread.actions[1:]) >= 0.51
    original_wh = sum(action.power_w for action in actions[1:]) * (5 / 60)
    spread_wh = sum(action.power_w for action in spread.actions[1:]) * (5 / 60)
    assert spread_wh == pytest.approx(original_wh, abs=0.1)


def test_spread_export_soc_cursor_includes_charge_before_first_export(opt_module):
    coordinator = _coordinator(opt_module, "globird", profit_max=True)
    coordinator.battery_system = "sigenergy"
    coordinator._config.spread_export_enabled = True
    coordinator._config.battery_capacity_wh = 10000
    coordinator._config.max_charge_w = 6000
    coordinator._config.max_discharge_w = 5000
    coordinator._config.max_grid_export_w = 5000
    coordinator._optimizer = SimpleNamespace(efficiency=1.0)
    start = datetime(2026, 7, 14, 17, 55, tzinfo=timezone.utc)
    actions = [
        opt_module.ScheduleAction(
            timestamp=start,
            action="self_consumption",
            power_w=0,
            soc=0.30,
        ),
        opt_module.ScheduleAction(
            timestamp=start + timedelta(minutes=5),
            action="charge",
            power_w=6000,
            soc=0.35,
            battery_charge_w=6000,
        ),
        opt_module.ScheduleAction(
            timestamp=start + timedelta(minutes=10),
            action="export",
            power_w=3000,
            soc=0.325,
            battery_discharge_w=3000,
        ),
    ]
    schedule = opt_module.OptimizationSchedule(
        actions=actions,
        predicted_cost=0,
        predicted_savings=0,
        last_updated=start,
    )

    spread = coordinator._spread_export_schedule(
        schedule,
        [False, True, True],
        export_reserve_floor=0.30,
    )

    assert spread.actions[1].action == "charge"
    assert spread.actions[1].battery_charge_w == 6000
    assert spread.actions[2].action == "export"
    assert spread.actions[2].power_w == 3000
    assert spread.actions[2].soc == pytest.approx(0.325)


def test_spread_export_schedule_defaults_to_configured_reserve_floor(opt_module):
    coordinator = _coordinator(opt_module, "flow_power", profit_max=True)
    coordinator.battery_system = "goodwe"
    coordinator._config.spread_export_enabled = True
    coordinator._config.backup_reserve = 0.30
    coordinator._config.battery_capacity_wh = 10000
    coordinator._config.max_discharge_w = 10000
    coordinator._optimizer = SimpleNamespace(efficiency=0.92)
    start = datetime(2026, 5, 3, 17, 25, tzinfo=timezone.utc)
    actions = [
        opt_module.ScheduleAction(
            timestamp=start,
            action="self_consumption",
            power_w=0,
            soc=0.45,
            battery_discharge_w=0,
        )
    ]
    actions.extend(
        opt_module.ScheduleAction(
            timestamp=start + (idx + 1) * timedelta(minutes=5),
            action="export" if idx < 4 else "self_consumption",
            power_w=10000 if idx < 4 else 0,
            soc=[0.42, 0.34, 0.25, 0.12, 0.05, 0.05][idx],
            battery_discharge_w=10000 if idx < 4 else 0,
        )
        for idx in range(6)
    )
    schedule = opt_module.OptimizationSchedule(
        actions=actions,
        predicted_cost=0,
        predicted_savings=0,
        last_updated=start,
    )

    spread = coordinator._spread_export_schedule(
        schedule,
        [False] + [True] * 6,
    )

    export_actions = [action for action in spread.actions if action.action == "export"]
    assert export_actions
    assert min(action.soc for action in export_actions) >= 0.30 - 1e-6
    assert all(
        action.action != "export" or action.soc >= 0.30
        for action in spread.actions
    )
    assert sum(action.battery_discharge_w for action in spread.actions) < sum(
        action.battery_discharge_w for action in actions
    )


def test_spread_export_schedule_carries_reserve_soc_after_capped_export(opt_module):
    coordinator = _coordinator(opt_module, "flow_power", profit_max=True)
    coordinator.battery_system = "goodwe"
    coordinator._config.spread_export_enabled = True
    coordinator._config.backup_reserve = 0.30
    coordinator._config.battery_capacity_wh = 10000
    coordinator._config.max_discharge_w = 6000
    coordinator._optimizer = SimpleNamespace(efficiency=1.0)
    start = datetime(2026, 5, 3, 17, 30, tzinfo=timezone.utc)
    actions = [
        opt_module.ScheduleAction(
            timestamp=start + idx * timedelta(minutes=5),
            action="export",
            power_w=6000,
            soc=[0.40, 0.35, 0.30, 0.25, 0.20, 0.05][idx],
            battery_discharge_w=6000,
        )
        for idx in range(6)
    ]
    schedule = opt_module.OptimizationSchedule(
        actions=actions,
        predicted_cost=0,
        predicted_savings=0,
        last_updated=start,
    )

    spread = coordinator._spread_export_schedule(schedule, [True] * 6)

    assert [action.action for action in spread.actions] == [
        "export",
        "export",
        "self_consumption",
        "self_consumption",
        "self_consumption",
        "self_consumption",
    ]
    assert min(action.soc for action in spread.actions) >= 0.30 - 1e-6
    assert [action.soc for action in spread.actions[2:]] == [0.30] * 4


def test_export_reserve_floor_bridges_after_contiguous_export_run(opt_module):
    coordinator = _coordinator(opt_module, "globird", profit_max=True)
    coordinator._config.backup_reserve = 0.0
    coordinator._config.battery_capacity_wh = 10000
    coordinator._config.interval_minutes = 5
    coordinator._optimizer = SimpleNamespace(efficiency=1.0)
    start = datetime(2026, 7, 7, 18, 0, tzinfo=timezone.utc)
    actions = [
        opt_module.ScheduleAction(
            timestamp=start + idx * timedelta(minutes=5),
            action="export" if idx < 3 else "self_consumption",
            power_w=5000 if idx < 3 else 0,
            soc=0.9,
            battery_discharge_w=5000 if idx < 3 else 0,
        )
        for idx in range(5)
    ]
    actions.append(
        opt_module.ScheduleAction(
            timestamp=start + timedelta(minutes=25),
            action="charge",
            power_w=5000,
            soc=0.7,
            battery_charge_w=5000,
        )
    )
    schedule = opt_module.OptimizationSchedule(
        actions=actions,
        predicted_cost=0,
        predicted_savings=0,
        last_updated=start,
    )

    floors, metadata = coordinator._post_processed_export_reserve_floor_slots(
        schedule,
        solar_forecast=[0.0] * 6,
        load_forecast=[6.0] * 6,
    )

    assert floors is not None
    assert floors[:3] == pytest.approx([0.1, 0.1, 0.1])
    assert floors[3:] == [0.0, 0.0, 0.0]
    assert metadata["home_load_bridge_kwh"] == pytest.approx(1.0)
    assert metadata["home_load_bridge_start"] == actions[3].timestamp.isoformat()
    assert metadata["home_load_bridge_until"] == actions[5].timestamp.isoformat()


def test_schedule_display_grid_export_uses_post_processed_battery_export(opt_module):
    coordinator = _coordinator(opt_module, "flow_power", profit_max=True)
    coordinator._last_solar_forecast = [0.0, 0.0, 1.5]
    coordinator._last_load_forecast = [0.0, 0.0, 0.5]
    api_response = {
        "timestamps": ["a", "b", "c"],
        "charge_w": [0.0, 0.0, 0.0],
        "battery_consume_w": [0.0, 0.0, 0.0],
        "battery_export_w": [23000.0, 0.0, 0.0],
    }

    _grid_import, grid_export = coordinator._display_grid_arrays_from_schedule(
        api_response,
        raw_grid_import_w=[0.0, 0.0, 0.0],
        raw_grid_export_w=[23000.0, 23000.0, 13075.0],
    )

    assert grid_export == [23000.0, 0.0, 1000.0]


def test_spread_export_schedule_uses_export_power_for_target_batteries(opt_module):
    coordinator = _coordinator(opt_module, "octopus")
    coordinator.battery_system = "goodwe"
    coordinator._config.spread_export_enabled = True
    coordinator._config.max_discharge_w = 5000
    start = datetime(2026, 5, 3, 9, 0, tzinfo=timezone.utc)
    actions = [
        opt_module.ScheduleAction(
            timestamp=start + idx * timedelta(minutes=5),
            action="export" if idx < 2 else "self_consumption",
            power_w=600 if idx < 2 else 1400,
            battery_discharge_w=2000 if idx < 2 else 1400,
        )
        for idx in range(4)
    ]
    schedule = opt_module.OptimizationSchedule(
        actions=actions,
        predicted_cost=0,
        predicted_savings=0,
        last_updated=start,
    )

    spread = coordinator._spread_export_schedule(schedule, [True] * 4)

    assert [action.power_w for action in spread.actions] == [300.0] * 4
    export_wh = sum(action.power_w for action in spread.actions) * (5 / 60)
    assert export_wh == pytest.approx(600 * 2 * (5 / 60), abs=0.1)
    assert [action.battery_discharge_w for action in spread.actions] == [1700.0] * 4


def test_spread_export_schedule_preserves_home_load_without_export_headroom(opt_module):
    coordinator = _coordinator(opt_module, "octopus")
    coordinator.battery_system = "goodwe"
    coordinator._config.spread_export_enabled = True
    coordinator._config.max_discharge_w = 5000
    start = datetime(2026, 5, 3, 9, 0, tzinfo=timezone.utc)
    schedule = opt_module.OptimizationSchedule(
        actions=[
            opt_module.ScheduleAction(
                timestamp=start,
                action="export",
                power_w=1000,
                battery_discharge_w=2000,
            ),
            opt_module.ScheduleAction(
                timestamp=start + timedelta(minutes=5),
                action="self_consumption",
                power_w=5000,
                battery_discharge_w=5000,
            ),
            opt_module.ScheduleAction(
                timestamp=start + timedelta(minutes=10),
                action="export",
                power_w=1000,
                battery_discharge_w=2000,
            ),
        ],
        predicted_cost=0,
        predicted_savings=0,
        last_updated=start,
    )

    spread = coordinator._spread_export_schedule(schedule, [True, True, True])

    assert spread.actions[0].action == "export"
    assert spread.actions[0].power_w == 1000.0
    assert spread.actions[0].battery_discharge_w == 2000.0
    assert spread.actions[1].action == "self_consumption"
    assert spread.actions[1].power_w == 5000.0
    assert spread.actions[1].battery_discharge_w == 5000.0
    assert spread.actions[2].action == "export"
    assert spread.actions[2].power_w == 1000.0
    assert spread.actions[2].battery_discharge_w == 2000.0

    coordinator._bridge_short_export_gaps(spread, [0.45, 0.45, 0.45])

    assert spread.actions[1].action == "self_consumption"
    assert spread.actions[1].power_w == 5000.0
    assert spread.actions[1].battery_discharge_w == 5000.0


def test_spread_export_schedule_reallocates_around_variable_home_load(opt_module):
    coordinator = _coordinator(opt_module, "octopus")
    coordinator.battery_system = "goodwe"
    coordinator._config.spread_export_enabled = True
    coordinator._config.max_discharge_w = 5000
    start = datetime(2026, 5, 3, 9, 0, tzinfo=timezone.utc)
    actions = [
        opt_module.ScheduleAction(
            timestamp=start + idx * timedelta(minutes=5),
            action="export" if idx < 2 else "self_consumption",
            power_w=600 if idx < 2 else 4900,
            battery_discharge_w=2000 if idx < 2 else 4900,
        )
        for idx in range(4)
    ]
    schedule = opt_module.OptimizationSchedule(actions, 0, 0, start)

    spread = coordinator._spread_export_schedule(schedule, [True] * 4)

    assert [action.power_w for action in spread.actions] == [500.0, 500.0, 100.0, 100.0]
    assert [action.battery_discharge_w for action in spread.actions] == [
        1900.0,
        1900.0,
        5000.0,
        5000.0,
    ]
    export_wh = sum(action.power_w for action in spread.actions) * (5 / 60)
    assert export_wh == pytest.approx(600 * 2 * (5 / 60), abs=0.1)


def test_spread_export_schedule_preserves_home_at_export_floor(opt_module):
    coordinator = _coordinator(opt_module, "flow_power", profit_max=True)
    coordinator.battery_system = "goodwe"
    coordinator._config.spread_export_enabled = True
    coordinator._config.battery_capacity_wh = 10000
    coordinator._config.max_discharge_w = 5000
    coordinator._optimizer = SimpleNamespace(efficiency=1.0)
    start = datetime(2026, 5, 3, 17, 30, tzinfo=timezone.utc)
    schedule = opt_module.OptimizationSchedule(
        actions=[
            opt_module.ScheduleAction(
                timestamp=start,
                action="export",
                power_w=1000,
                soc=0.30,
                battery_discharge_w=2000,
            )
        ],
        predicted_cost=0,
        predicted_savings=0,
        last_updated=start,
    )

    spread = coordinator._spread_export_schedule(
        schedule,
        [True],
        export_reserve_floor=0.30,
    )

    assert spread.actions[0].action == "self_consumption"
    assert spread.actions[0].power_w == 1000.0
    assert spread.actions[0].battery_discharge_w == 1000.0
    assert spread.actions[0].soc == pytest.approx(0.2917, abs=0.0001)


def test_spread_import_schedule_flattens_planned_energy_across_same_price_window(opt_module):
    coordinator = _coordinator(opt_module, "octopus")
    coordinator.battery_system = "goodwe"
    coordinator._config.spread_import_enabled = True
    coordinator._config.max_charge_w = 15000
    coordinator._config.battery_capacity_wh = 50000
    start = datetime(2026, 5, 3, 11, 0, tzinfo=timezone.utc)
    actions = [
        opt_module.ScheduleAction(
            timestamp=start + idx * timedelta(minutes=5),
            action="charge" if idx < 18 else "self_consumption",
            power_w=15000 if idx < 18 else 0,
            battery_charge_w=15000 if idx < 18 else 0,
        )
        for idx in range(36)
    ]
    schedule = opt_module.OptimizationSchedule(
        actions=actions,
        predicted_cost=0,
        predicted_savings=0,
        last_updated=start,
    )

    spread = coordinator._spread_import_schedule(
        schedule,
        [0.12] * 36,
        [False] * 36,
        initial_soc=0.35,
    )

    assert {action.action for action in spread.actions} == {"charge"}
    assert [action.power_w for action in spread.actions] == [7500.0] * 36
    original_wh = sum(action.battery_charge_w for action in actions) * (5 / 60)
    spread_wh = sum(action.battery_charge_w for action in spread.actions) * (5 / 60)
    assert spread_wh == pytest.approx(original_wh, abs=0.1)


def test_spread_import_free_window_caps_to_available_battery_room(opt_module):
    coordinator = _coordinator(opt_module, "octopus")
    coordinator.battery_system = "goodwe"
    coordinator._config.spread_import_enabled = True
    coordinator._config.max_charge_w = 15000
    coordinator._config.battery_capacity_wh = 50000
    start = datetime(2026, 5, 3, 11, 0, tzinfo=timezone.utc)
    actions = [
        opt_module.ScheduleAction(
            timestamp=start + idx * timedelta(minutes=5),
            action="charge",
            power_w=15000,
            battery_charge_w=15000,
        )
        for idx in range(36)
    ]
    schedule = opt_module.OptimizationSchedule(
        actions=actions,
        predicted_cost=0,
        predicted_savings=0,
        last_updated=start,
    )

    spread = coordinator._spread_import_schedule(
        schedule,
        [0.0] * 36,
        [False] * 36,
        initial_soc=0.80,
    )

    expected_power_w = round(((1.0 - 0.80) * 50000 / 0.92) / 3, 1)
    assert [action.power_w for action in spread.actions] == [expected_power_w] * 36
    spread_wh = sum(action.battery_charge_w for action in spread.actions) * (5 / 60)
    assert spread_wh == pytest.approx((1.0 - 0.80) * 50000 / 0.92, abs=0.1)


def test_spread_import_schedule_respects_site_import_headroom(opt_module):
    coordinator = _coordinator(opt_module, "octopus")
    coordinator.battery_system = "goodwe"
    coordinator._config.spread_import_enabled = True
    coordinator._config.max_charge_w = 15000
    coordinator._config.max_grid_import_w = 11100
    coordinator._config.battery_capacity_wh = 50000
    start = datetime(2026, 5, 3, 11, 0, tzinfo=timezone.utc)
    actions = [
        opt_module.ScheduleAction(
            timestamp=start + idx * timedelta(minutes=5),
            action="charge",
            power_w=15000,
            battery_charge_w=15000,
        )
        for idx in range(36)
    ]
    schedule = opt_module.OptimizationSchedule(
        actions=actions,
        predicted_cost=0,
        predicted_savings=0,
        last_updated=start,
    )

    spread = coordinator._spread_import_schedule(
        schedule,
        [0.12] * 36,
        [False] * 36,
        initial_soc=0.35,
        solar_forecast=[0.0] * 36,
        load_forecast=[1.0] * 36,
    )

    assert max(action.battery_charge_w for action in spread.actions) == pytest.approx(
        10100.0,
        abs=0.1,
    )
    assert all(
        action.battery_charge_w + 1000 <= 11100.1
        for action in spread.actions
        if action.action == "charge"
    )


def test_spread_import_schedule_uses_solar_headroom_above_site_import_cap(opt_module):
    coordinator = _coordinator(opt_module, "octopus")
    coordinator.battery_system = "goodwe"
    coordinator._config.spread_import_enabled = True
    coordinator._config.max_charge_w = 15000
    coordinator._config.max_grid_import_w = 11100
    coordinator._config.battery_capacity_wh = 50000
    start = datetime(2026, 5, 3, 11, 0, tzinfo=timezone.utc)
    actions = [
        opt_module.ScheduleAction(
            timestamp=start + idx * timedelta(minutes=5),
            action="charge",
            power_w=15000,
            battery_charge_w=15000,
        )
        for idx in range(36)
    ]
    schedule = opt_module.OptimizationSchedule(
        actions=actions,
        predicted_cost=0,
        predicted_savings=0,
        last_updated=start,
    )

    spread = coordinator._spread_import_schedule(
        schedule,
        [0.12] * 36,
        [False] * 36,
        initial_soc=0.35,
        solar_forecast=[5.0] * 36,
        load_forecast=[1.0] * 36,
    )

    assert max(action.battery_charge_w for action in spread.actions) == pytest.approx(
        15000.0,
        abs=0.1,
    )


def test_spread_import_free_only_smooths_free_window(opt_module):
    coordinator = _coordinator(opt_module, "octopus")
    coordinator.battery_system = "sungrow"
    coordinator._config.spread_import_enabled = True
    coordinator._config.max_charge_w = 6000
    coordinator._config.battery_capacity_wh = 20000
    start = datetime(2026, 5, 3, 11, 0, tzinfo=timezone.utc)
    actions = [
        opt_module.ScheduleAction(
            timestamp=start + idx * timedelta(minutes=5),
            action="charge" if idx in (0, 2, 4) else "self_consumption",
            power_w=6000 if idx in (0, 2, 4) else 0,
            battery_charge_w=6000 if idx in (0, 2, 4) else 0,
        )
        for idx in range(6)
    ]
    schedule = opt_module.OptimizationSchedule(
        actions=actions,
        predicted_cost=0,
        predicted_savings=0,
        last_updated=start,
    )

    spread = coordinator._spread_import_schedule(
        schedule,
        [0.0] * 6,
        [False] * 6,
        initial_soc=0.20,
        free_only=True,
    )

    assert [action.action for action in spread.actions] == ["charge"] * 6
    assert [action.power_w for action in spread.actions] == [3000.0] * 6
    original_wh = sum(action.battery_charge_w for action in actions) * (5 / 60)
    spread_wh = sum(action.battery_charge_w for action in spread.actions) * (5 / 60)
    assert spread_wh == pytest.approx(original_wh, abs=0.1)


def test_spread_import_free_only_leaves_paid_window_unchanged(opt_module):
    coordinator = _coordinator(opt_module, "octopus")
    coordinator.battery_system = "sungrow"
    coordinator._config.spread_import_enabled = True
    coordinator._config.max_charge_w = 6000
    coordinator._config.battery_capacity_wh = 20000
    start = datetime(2026, 5, 3, 11, 0, tzinfo=timezone.utc)
    actions = [
        opt_module.ScheduleAction(
            timestamp=start + idx * timedelta(minutes=5),
            action="charge" if idx in (0, 2, 4) else "self_consumption",
            power_w=6000 if idx in (0, 2, 4) else 0,
            battery_charge_w=6000 if idx in (0, 2, 4) else 0,
        )
        for idx in range(6)
    ]
    schedule = opt_module.OptimizationSchedule(
        actions=actions,
        predicted_cost=0,
        predicted_savings=0,
        last_updated=start,
    )

    spread = coordinator._spread_import_schedule(
        schedule,
        [0.12] * 6,
        [False] * 6,
        initial_soc=0.20,
        free_only=True,
    )

    assert [action.action for action in spread.actions] == [
        "charge",
        "self_consumption",
        "charge",
        "self_consumption",
        "charge",
        "self_consumption",
    ]
    assert [action.power_w for action in spread.actions] == [6000, 0, 6000, 0, 6000, 0]


def test_spread_import_schedule_ignores_malformed_price_forecast(opt_module):
    coordinator = _coordinator(opt_module, "octopus")
    coordinator.battery_system = "sungrow"
    coordinator._config.max_charge_w = 6000
    start = datetime(2026, 5, 3, 11, 0, tzinfo=timezone.utc)
    actions = [
        opt_module.ScheduleAction(
            timestamp=start,
            action="charge",
            power_w=6000,
            battery_charge_w=6000,
        )
    ]
    schedule = opt_module.OptimizationSchedule(
        actions=actions,
        predicted_cost=0,
        predicted_savings=0,
        last_updated=start,
    )

    spread = coordinator._spread_import_schedule(
        schedule,
        [None],
        [False],
        initial_soc=0.20,
        free_only=True,
    )

    assert spread is schedule


def test_spread_import_schedule_does_not_cross_price_boundary(opt_module):
    coordinator = _coordinator(opt_module, "octopus")
    coordinator.battery_system = "goodwe"
    coordinator._config.spread_import_enabled = True
    coordinator._config.max_charge_w = 6000
    start = datetime(2026, 5, 3, 11, 0, tzinfo=timezone.utc)
    actions = [
        opt_module.ScheduleAction(
            timestamp=start + idx * timedelta(minutes=5),
            action="charge" if idx < 2 else "self_consumption",
            power_w=6000 if idx < 2 else 0,
            battery_charge_w=6000 if idx < 2 else 0,
        )
        for idx in range(12)
    ]
    schedule = opt_module.OptimizationSchedule(
        actions=actions,
        predicted_cost=0,
        predicted_savings=0,
        last_updated=start,
    )

    spread = coordinator._spread_import_schedule(
        schedule,
        [0.10] * 6 + [0.20] * 6,
        [False] * 12,
        initial_soc=0.20,
    )

    assert [action.power_w for action in spread.actions[:6]] == [2000.0] * 6
    assert [action.action for action in spread.actions[6:]] == ["self_consumption"] * 6


def test_spread_import_schedule_splits_on_blocked_charge_slot(opt_module):
    coordinator = _coordinator(opt_module, "octopus")
    coordinator.battery_system = "goodwe"
    coordinator._config.spread_import_enabled = True
    coordinator._config.max_charge_w = 6000
    start = datetime(2026, 5, 3, 11, 0, tzinfo=timezone.utc)
    actions = [
        opt_module.ScheduleAction(
            timestamp=start + idx * timedelta(minutes=5),
            action="charge" if idx in (0, 1, 4) else "self_consumption",
            power_w=6000 if idx in (0, 1, 4) else 0,
            battery_charge_w=6000 if idx in (0, 1, 4) else 0,
        )
        for idx in range(6)
    ]
    schedule = opt_module.OptimizationSchedule(
        actions=actions,
        predicted_cost=0,
        predicted_savings=0,
        last_updated=start,
    )

    spread = coordinator._spread_import_schedule(
        schedule,
        [0.10] * 6,
        [False, False, False, True, False, False],
        initial_soc=0.20,
    )

    assert [action.power_w for action in spread.actions[:3]] == [4000.0] * 3
    assert spread.actions[3].action == "self_consumption"
    assert [action.power_w for action in spread.actions[4:]] == [3000.0] * 2


def test_spread_import_schedule_preserves_export_actions(opt_module):
    coordinator = _coordinator(opt_module, "octopus")
    coordinator.battery_system = "goodwe"
    coordinator._config.spread_import_enabled = True
    coordinator._config.max_charge_w = 6000
    start = datetime(2026, 5, 3, 11, 0, tzinfo=timezone.utc)
    actions = [
        opt_module.ScheduleAction(
            timestamp=start + idx * timedelta(minutes=5),
            action=(
                "charge"
                if idx in (0, 1, 4)
                else "export"
                if idx == 3
                else "self_consumption"
            ),
            power_w=(
                6000
                if idx in (0, 1, 4)
                else 5000
                if idx == 3
                else 0
            ),
            battery_charge_w=6000 if idx in (0, 1, 4) else 0,
            battery_discharge_w=5000 if idx == 3 else 0,
        )
        for idx in range(6)
    ]
    schedule = opt_module.OptimizationSchedule(
        actions=actions,
        predicted_cost=0,
        predicted_savings=0,
        last_updated=start,
    )

    spread = coordinator._spread_import_schedule(
        schedule,
        [0.10] * 6,
        [False] * 6,
        initial_soc=0.20,
    )

    assert [action.power_w for action in spread.actions[:3]] == [4000.0] * 3
    assert spread.actions[3].action == "export"
    assert spread.actions[3].power_w == 5000
    assert [action.power_w for action in spread.actions[4:]] == [3000.0] * 2


def test_spread_import_schedule_requires_enabled_supported_battery(opt_module):
    coordinator = _coordinator(opt_module, "octopus")
    coordinator.battery_system = "goodwe"
    coordinator._config.spread_import_enabled = False

    assert coordinator._should_spread_import_schedule() is False

    coordinator._config.spread_import_enabled = True
    coordinator.battery_system = "tesla"

    assert coordinator._should_spread_import_schedule() is False


def test_free_import_smoothing_is_not_automatic_when_spread_import_is_disabled(opt_module):
    coordinator = _coordinator(opt_module, "octopus")
    coordinator.battery_system = "sungrow"
    coordinator._config.spread_import_enabled = False
    coordinator._config.allow_grid_charge = True

    assert coordinator._should_spread_import_schedule() is False

    coordinator_source = (
        ROOT / "custom_components" / "power_sync" / "optimization" / "coordinator.py"
    ).read_text()
    assert "_should_smooth_free_import_schedule" not in coordinator_source
    assert "smooth_free_import" not in coordinator_source
    assert "if self._should_spread_import_schedule():" in coordinator_source

    coordinator._config.spread_import_enabled = True
    assert coordinator._should_spread_import_schedule() is True


def test_profit_max_spread_uses_flow_power_export_window(opt_module):
    coordinator = _coordinator(
        opt_module,
        "flow_power",
        profit_max=True,
        flow_power_state="NSW1",
    )
    coordinator.battery_system = "sigenergy"
    coordinator._config.spread_export_enabled = True
    coordinator._config.max_discharge_w = 5000
    start = datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    actions = [
        opt_module.ScheduleAction(
            timestamp=start + idx * timedelta(minutes=5),
            action="self_consumption",
            power_w=0,
        )
        for idx in range(150)
    ]
    actions[108].action = "export"
    actions[108].power_w = 5000
    actions[108].battery_discharge_w = 5000
    actions[109].action = "export"
    actions[109].power_w = 5000
    actions[109].battery_discharge_w = 5000
    schedule = opt_module.OptimizationSchedule(actions, 0, 0, start)

    allowed = coordinator._battery_export_allowed_slots(150, [0.0] * 150)
    spread = coordinator._spread_export_schedule(schedule, allowed)

    export_window = spread.actions[108:132]
    assert all(action.action == "export" for action in export_window)
    assert all(action.power_w == pytest.approx(416.7, abs=0.1) for action in export_window)
    assert spread.actions[107].action == "self_consumption"
    assert spread.actions[132].action == "self_consumption"


def test_flow_power_export_override_replaces_happy_hour_rate(opt_module):
    coordinator = _coordinator(
        opt_module,
        "flow_power",
        flow_power_state="NSW1",
        flow_power_export_rate=50,
    )
    original_now = opt_module.dt_util.now
    try:
        opt_module.dt_util.now = lambda *args, **kwargs: datetime(
            2026, 5, 3, 17, 30, tzinfo=timezone.utc
        )

        assert coordinator._apply_flow_power_export([0.0, 0.0]) == [0.5, 0.5]
    finally:
        opt_module.dt_util.now = original_now


def test_flow_power_zero_export_override_disables_profit_window(opt_module):
    coordinator = _coordinator(
        opt_module,
        "flow_power",
        profit_max=True,
        flow_power_state="NSW1",
        flow_power_export_rate=0,
    )

    assert coordinator._flow_power_export_window_slots(4) == [False] * 4


def test_supported_battery_spread_export_uses_action_power(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    coordinator.battery_system = "goodwe"
    coordinator._config.spread_export_enabled = True
    start = datetime(2026, 5, 3, 18, 30, tzinfo=timezone.utc)
    actions = [
        SimpleNamespace(
            action="export",
            power_w=2100,
            timestamp=start + idx * timedelta(minutes=5),
        )
        for idx in range(4)
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))

    assert battery.force_discharge_calls == [(20, 2100, False, None)]


def test_unsupported_battery_spread_export_keeps_max_discharge(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    coordinator.battery_system = "tesla"
    coordinator._config.spread_export_enabled = True
    start = datetime(2026, 5, 3, 18, 30, tzinfo=timezone.utc)
    actions = [
        SimpleNamespace(action="export", power_w=2100, timestamp=start),
        SimpleNamespace(
            action="self_consumption",
            power_w=0,
            timestamp=start + timedelta(minutes=5),
        ),
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))

    assert battery.force_discharge_calls == [(5, 5000, False, None)]


def test_tesla_export_near_tariff_boundary_extends_tariff_window(opt_module):
    boundary_now = datetime(2026, 5, 3, 8, 25, tzinfo=timezone.utc)
    opt_module.dt_util.now = lambda *args, **kwargs: boundary_now
    opt_module.dt_util.utcnow = lambda *args, **kwargs: boundary_now
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    actions = [
        SimpleNamespace(action="export", power_w=4200, timestamp=boundary_now),
        SimpleNamespace(
            action="self_consumption",
            power_w=0,
            timestamp=boundary_now + timedelta(minutes=5),
        ),
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))

    assert battery.force_discharge_calls == [(5, 5000, False, 10)]


def test_tesla_export_away_from_tariff_boundary_uses_software_duration_only(opt_module):
    stable_now = datetime(2026, 5, 3, 8, 20, tzinfo=timezone.utc)
    opt_module.dt_util.now = lambda *args, **kwargs: stable_now
    opt_module.dt_util.utcnow = lambda *args, **kwargs: stable_now
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    actions = [
        SimpleNamespace(action="export", power_w=4200, timestamp=stable_now),
        SimpleNamespace(
            action="self_consumption",
            power_w=0,
            timestamp=stable_now + timedelta(minutes=5),
        ),
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))

    assert battery.force_discharge_calls == [(5, 5000, False, None)]


def test_export_duration_clips_at_next_non_export_boundary(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    coordinator.battery_system = "foxess"
    start = datetime(2026, 5, 3, 9, 25, tzinfo=timezone.utc)
    actions = [
        SimpleNamespace(
            action="export",
            power_w=4200,
            timestamp=start,
        ),
        SimpleNamespace(
            action="self_consumption",
            power_w=0,
            timestamp=start + timedelta(minutes=5),
        ),
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))

    assert battery.force_discharge_calls == [(5, 4200, False, None)]
    assert coordinator._last_executed_action == "export"


def test_export_price_gate_uses_action_timestamp(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    coordinator.battery_system = "foxess"
    start = datetime(2026, 5, 3, 17, 30, tzinfo=timezone.utc)
    actions = [
        SimpleNamespace(
            action="export",
            power_w=4200,
            timestamp=start,
        ),
        SimpleNamespace(
            action="self_consumption",
            power_w=0,
            timestamp=start + timedelta(minutes=5),
        ),
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)
    coordinator._last_price_timestamps = [
        start - timedelta(minutes=5),
        start,
    ]
    coordinator._last_export_prices = [0.0, 0.15]

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))

    assert battery.force_discharge_calls == [(5, 4200, False, None)]
    assert battery.self_consumption_calls == 0
    assert coordinator._last_executed_action == "export"


def test_profit_max_export_floor_does_not_block_when_auto_apply_disabled(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.73)
    coordinator.battery_system = "goodwe"
    coordinator._config.profit_max_enabled = True
    coordinator._config.backup_reserve = 0.05
    coordinator._config.max_discharge_w = 6000
    coordinator._auto_apply_reserve_enabled = False
    coordinator._last_optimizer_result = SimpleNamespace(
        reserve_recommendation={"home_load_export_floor_percent": 84}
    )
    start = datetime(2026, 6, 4, 8, 25, tzinfo=timezone.utc)
    actions = [
        SimpleNamespace(
            action="export",
            power_w=6000,
            soc=0.706,
            timestamp=start,
        ),
        SimpleNamespace(
            action="self_consumption",
            power_w=0,
            timestamp=start + timedelta(minutes=5),
        ),
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))

    assert battery.force_discharge_calls == [(5, 6000, False, None)]
    assert battery.self_consumption_calls == 0
    assert coordinator._last_executed_action == "export"


def test_active_export_floor_does_not_block_when_auto_apply_disabled(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.38)
    coordinator.battery_system = "foxess"
    coordinator._config.backup_reserve = 0.16
    coordinator._config.max_discharge_w = 23000
    coordinator._auto_apply_reserve_enabled = False
    start = datetime(2026, 7, 8, 9, 0, tzinfo=timezone.utc)
    action = SimpleNamespace(
        action="export",
        power_w=21600,
        soc=0.326,
        timestamp=start,
    )
    coordinator._current_schedule = SimpleNamespace(actions=[action])
    coordinator._set_active_export_reserve_floor_slots(
        [0.35],
        coordinator._current_schedule,
    )

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.force_discharge_calls == [(5, 21600, False, None)]
    assert battery.self_consumption_calls == 0
    assert coordinator._last_executed_action == "export"


def test_foxess_export_at_optimizer_reserve_switches_to_self_consumption(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.20)
    coordinator.battery_system = "foxess"
    coordinator._last_executed_action = "export"

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="export", power_w=4200)
        )
    )

    assert battery.force_discharge_calls == []
    assert battery.self_consumption_calls == 1
    assert coordinator._last_executed_action == "self_consumption"


def test_tesla_export_near_reserve_switches_to_self_consumption(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.20)
    coordinator._last_executed_action = "export"

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="export", power_w=4200)
        )
    )

    assert battery.force_discharge_calls == []
    assert battery.self_consumption_calls == 1
    assert coordinator._last_executed_action == "self_consumption"


def test_tesla_force_extension_reuploads_when_tariff_window_missing(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    start = datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    actions = [
        SimpleNamespace(
            action="export",
            power_w=4200,
            timestamp=start + idx * timedelta(minutes=5),
        )
        for idx in range(4)
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)
    force_state = {
        "active": True,
        "expires_at": start + timedelta(minutes=5),
        "source": "optimizer",
    }
    coordinator.hass.data = {
        "power_sync": {
            "entry-1": {
                "force_discharge_state": force_state,
            }
        }
    }
    coordinator._force_state_getter = lambda: {
        "active": True,
        "type": "discharge",
        "source": "optimizer",
    }

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))

    assert battery.force_discharge_calls == [(20, 5000, True, None)]
    assert force_state["expires_at"] == datetime(2026, 5, 3, 8, 50, tzinfo=timezone.utc)


def test_spread_export_force_extension_reuploads_target_power(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    coordinator.battery_system = "goodwe"
    coordinator._config.spread_export_enabled = True
    start = datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    actions = [
        SimpleNamespace(
            action="export",
            power_w=1800,
            timestamp=start + idx * timedelta(minutes=5),
        )
        for idx in range(4)
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)
    force_state = {
        "active": True,
        "expires_at": start + timedelta(minutes=5),
        "source": "optimizer",
    }
    coordinator.hass.data = {
        "power_sync": {
            "entry-1": {
                "force_discharge_state": force_state,
            }
        }
    }
    coordinator._force_state_getter = lambda: {
        "active": True,
        "type": "discharge",
        "source": "optimizer",
    }

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))

    assert battery.force_discharge_calls == [(20, 1800, True, None)]


def test_target_export_force_refreshes_when_optimizer_power_changes(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    coordinator.battery_system = "goodwe"
    coordinator._config.max_discharge_w = 8000
    start = datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    actions = [
        SimpleNamespace(
            action="export",
            power_w=8000,
            timestamp=start + idx * timedelta(minutes=5),
        )
        for idx in range(3)
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)
    coordinator._set_optimizer_force_state("discharge", 60, 2190)

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))

    assert battery.force_discharge_calls == [(15, 8000, True, None)]
    assert coordinator._optimizer_force_state["power_w"] == 8000


def test_spread_export_force_refresh_applies_higher_target_within_grid_cap(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    coordinator.battery_system = "goodwe"
    coordinator._config.spread_export_enabled = True
    coordinator._config.max_discharge_w = 20000
    coordinator._config.max_grid_export_w = 9000
    start = datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    actions = [
        SimpleNamespace(
            action="export",
            power_w=20000,
            timestamp=start + idx * timedelta(minutes=5),
        )
        for idx in range(3)
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)
    coordinator._set_optimizer_force_state("discharge", 60, 8899)
    coordinator._optimizer_force_state["hardware_expires_at"] = opt_module.dt_util.utcnow()

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))

    assert battery.force_discharge_calls == [(15, 9000, True, None)]
    assert coordinator._optimizer_force_state["power_w"] == 9000


def test_non_target_export_force_ignores_power_change_when_window_is_valid(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    coordinator.battery_system = "tesla"
    coordinator._config.max_discharge_w = 5000
    start = datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    actions = [
        SimpleNamespace(
            action="export",
            power_w=4200,
            timestamp=start + idx * timedelta(minutes=5),
        )
        for idx in range(3)
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)
    coordinator._set_optimizer_force_state("discharge", 60, 2000)

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))

    assert battery.force_discharge_calls == []


def test_tesla_force_extension_near_tariff_boundary_extends_tariff_window(opt_module):
    boundary_now = datetime(2026, 5, 3, 8, 25, tzinfo=timezone.utc)
    opt_module.dt_util.now = lambda *args, **kwargs: boundary_now
    opt_module.dt_util.utcnow = lambda *args, **kwargs: boundary_now
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    actions = [
        SimpleNamespace(action="export", power_w=4200, timestamp=boundary_now),
        SimpleNamespace(
            action="self_consumption",
            power_w=0,
            timestamp=boundary_now + timedelta(minutes=5),
        ),
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)
    force_state = {
        "active": True,
        "expires_at": boundary_now,
        "source": "optimizer",
        "hardware_expires_at": boundary_now + timedelta(minutes=5),
    }
    coordinator.hass.data = {
        "power_sync": {
            "entry-1": {
                "force_discharge_state": force_state,
            }
        }
    }
    coordinator._force_state_getter = lambda: {
        "active": True,
        "type": "discharge",
        "source": "optimizer",
    }

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))

    assert battery.force_discharge_calls == [(5, 5000, True, 10)]
    assert force_state["expires_at"] == boundary_now + timedelta(minutes=5)


def test_tesla_force_extension_skips_reupload_when_tariff_window_covers_expiry(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    start = datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    actions = [
        SimpleNamespace(
            action="export",
            power_w=4200,
            timestamp=start + idx * timedelta(minutes=5),
        )
        for idx in range(4)
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)
    force_state = {
        "active": True,
        "expires_at": start + timedelta(minutes=5),
        "source": "optimizer",
        "hardware_expires_at": start + timedelta(minutes=30),
    }
    coordinator.hass.data = {
        "power_sync": {
            "entry-1": {
                "force_discharge_state": force_state,
            }
        }
    }
    coordinator._force_state_getter = lambda: {
        "active": True,
        "type": "discharge",
        "source": "optimizer",
    }

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))

    assert battery.force_discharge_calls == []
    assert force_state["expires_at"] == datetime(2026, 5, 3, 8, 50, tzinfo=timezone.utc)
