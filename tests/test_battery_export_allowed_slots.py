"""Regression tests for provider-scoped battery export permissions."""

from __future__ import annotations

import ast
import asyncio
import importlib
import logging
import sys
import types
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"

_SENTINEL = object()


def _registered_electricity_providers() -> list[str]:
    tree = ast.parse((COMPONENT_ROOT / "const.py").read_text())
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "ELECTRICITY_PROVIDERS"
            for target in node.targets
        )
    )
    providers = ast.literal_eval(assignment.value)
    return [*providers, "future_tariff"]

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
    "power_sync.coordinator",
    "power_sync.const",
    "power_sync.optimization",
    "power_sync.optimization.battery_optimizer",
    "power_sync.optimization.coordinator",
    "power_sync.optimization.ev_coordinator",
    "power_sync.optimization.executor",
    "power_sync.optimization.load_estimator",
    "power_sync.optimization.schedule_reader",
    "power_sync.optimization.solar_export",
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
    ha_dt.as_local = lambda value: value.astimezone(timezone.utc)
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

    coordinator_module = types.ModuleType("power_sync.coordinator")
    coordinator_module.normalize_custom_power_kw = lambda value: value
    sys.modules["power_sync.coordinator"] = coordinator_module

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
    const_module.CONF_FLOW_POWER_HAPPY_HOUR_END = "flow_power_happy_hour_end"
    const_module.DEFAULT_FLOW_POWER_HAPPY_HOUR_END = "19:30"
    const_module.resolve_flow_power_happy_hour_end = lambda value=None: (
        value if value in {"19:30", "21:30"} else "19:30"
    )
    const_module.CONF_FP_TWAP_OVERRIDE = "fp_twap_override"
    const_module.AGL_BATTERY_REWARDS_START_HOUR = 17
    const_module.AGL_BATTERY_REWARDS_END_HOUR = 21
    const_module.CONF_HARDWARE_BACKUP_RESERVE = "hardware_backup_reserve"
    const_module.CONF_OPTIMIZATION_ENABLED = "optimization_enabled"
    const_module.CONF_OPTIMIZATION_COST_FUNCTION = "optimization_cost_function"
    const_module.CONF_OPTIMIZATION_BACKUP_RESERVE = "optimization_backup_reserve"
    const_module.CONF_OPTIMIZATION_AUTO_APPLY_RESERVE = "optimization_auto_apply_reserve"
    const_module.CONF_OPTIMIZATION_MANUAL_RESERVE = "optimization_manual_reserve"
    const_module.CONF_OPTIMIZATION_EV_INTEGRATION = "optimization_ev_integration"
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
    const_module.CONF_DAILY_SUPPLY_CHARGE = "daily_supply_charge"
    const_module.CONF_MONTHLY_SUPPLY_CHARGE = "monthly_supply_charge"
    const_module.DEFAULT_CHARGE_BY_TIME_TARGET_TIME = "17:15"
    const_module.DEFAULT_CHARGE_BY_TIME_TARGET_SOC = 1.0
    const_module.DEFAULT_PROFIT_MAX_TARGET_TIME = "17:15"
    const_module.DEFAULT_PROFIT_MAX_TARGET_SOC = 1.0
    const_module.DEFAULT_OPTIMIZATION_INTERVAL = 5
    const_module.BATTERY_CAPACITY_DEFAULTS = {"tesla": 13500}
    const_module.BATTERY_POWER_DEFAULTS = {"tesla": 5000}
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
        ev_charge_w: float = 0.0

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
    coordinator.hass = SimpleNamespace(data={})
    coordinator.entry_id = "entry-1"
    coordinator._tariff_schedule = None
    coordinator._last_solar_forecast = None
    coordinator._last_load_forecast = None
    return coordinator


def _sigenergy_profit_max_coordinator(opt_module, *, export_limit_kw=5.0):
    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    coordinator.battery_system = "sigenergy"
    coordinator._entry = SimpleNamespace(
        options={"monitoring_mode": False}, data={}
    )
    coordinator.hass = SimpleNamespace(data={})
    coordinator.entry_id = "entry-1"
    coordinator._config = opt_module.OptimizationConfig(
        interval_minutes=60,
        horizon_hours=4,
        battery_capacity_wh=10000,
        max_charge_w=5000,
        max_grid_import_w=10000,
        allow_grid_charge=True,
        profit_max_enabled=True,
    )
    coordinator._optimizer = SimpleNamespace(
        efficiency=0.92,
        pre_window_slot=None,
    )
    coordinator.energy_coordinator = None
    coordinator._solar_export_hold = SimpleNamespace(
        capability=lambda: {
            "supported": export_limit_kw is not None,
            "reason": "supported" if export_limit_kw is not None else "unknown",
            "adapter": "sigenergy.modbus.charge_limit.v1",
            "export_limit_kw": export_limit_kw,
        }
    )
    return coordinator


def test_profit_max_solar_export_requires_cheaper_reachable_recharge(opt_module):
    coordinator = _sigenergy_profit_max_coordinator(opt_module)

    slots = coordinator._profit_max_solar_export_slots(
        import_prices=[0.40, 0.40, 0.00, 0.40],
        export_prices=[0.50, 0.20, 0.05, 0.20],
        solar=[5.0, 0.0, 0.0, 0.0],
        load=[1.0, 1.0, 1.0, 1.0],
        current_soc=0.5,
        hard_charge_blocks=[False] * 4,
        grid_charge_allowed=[True] * 4,
    )

    assert slots == [True, False, False, False]


def test_profit_max_solar_export_reports_current_slot_funding_for_reported_variant(
    opt_module,
):
    coordinator = _sigenergy_profit_max_coordinator(opt_module)
    coordinator.battery_system = "fronius_reserva"
    coordinator._config.interval_minutes = 5
    coordinator._solar_export_hold = SimpleNamespace(
        capability=lambda: {
            "supported": True,
            "reason": "supported",
            "adapter": "fronius_reserva.entity.block_charging.v1",
            "export_limit_kw": 5.0,
        }
    )

    slots = coordinator._profit_max_solar_export_slots(
        import_prices=[0.1836, 0.1836],
        export_prices=[0.0837, 0.03],
        solar=[1.10, 1.10],
        load=[0.18, 0.18],
        current_soc=0.599,
        hard_charge_blocks=[False, False],
        grid_charge_allowed=[False, False],
    )

    assert slots == [True, False]
    status = coordinator._solar_export_capability_status
    assert status["selected_slots"] == 1
    assert status["current_slot"]["selected"] is True
    assert status["current_slot"]["reason"] == "selected"
    assert status["current_slot"]["solar_surplus_kw"] == pytest.approx(0.92)
    assert status["current_slot"]["eligible_replenishment_kwh"] > 0

    slots = coordinator._profit_max_solar_export_slots(
        import_prices=[0.1836, 0.1836],
        export_prices=[0.0837, 0.03],
        solar=[1.10, 0.18],
        load=[0.18, 0.18],
        current_soc=0.599,
        hard_charge_blocks=[False, False],
        grid_charge_allowed=[False, False],
    )

    assert slots == [False, False]
    status = coordinator._solar_export_capability_status
    assert status["current_slot"]["selected"] is False
    assert (
        status["current_slot"]["reason"]
        == "insufficient_cheaper_replenishment"
    )
    assert status["current_slot"]["eligible_replenishment_kwh"] == 0
    assert status["rejection_counts"] == {
        "insufficient_cheaper_replenishment": 1
    }


def test_profit_max_solar_export_fails_closed_without_capability(opt_module):
    coordinator = _sigenergy_profit_max_coordinator(
        opt_module, export_limit_kw=None
    )

    slots = coordinator._profit_max_solar_export_slots(
        [0.40, 0.05],
        [0.50, 0.05],
        [5.0, 0.0],
        [1.0, 1.0],
        0.5,
        [False, False],
        [True, True],
    )

    assert slots == [False, False]


def test_charge_by_time_requires_repayment_before_deadline(opt_module):
    coordinator = _sigenergy_profit_max_coordinator(opt_module)
    coordinator._config.charge_by_time_enabled = True
    coordinator._optimizer.pre_window_slot = 2

    slots = coordinator._profit_max_solar_export_slots(
        import_prices=[0.40, 0.40, 0.05],
        export_prices=[0.50, 0.20, 0.05],
        solar=[5.0, 0.0, 0.0],
        load=[1.0, 1.0, 1.0],
        current_soc=0.5,
        hard_charge_blocks=[False] * 3,
        grid_charge_allowed=[False, False, True],
    )

    assert slots == [False, False, False]


def test_provider_charge_block_is_never_relabeled_as_profit_max(opt_module):
    coordinator = _sigenergy_profit_max_coordinator(opt_module)

    slots = coordinator._profit_max_solar_export_slots(
        [0.40, 0.00],
        [0.50, 0.00],
        [5.0, 0.0],
        [1.0, 1.0],
        0.5,
        [True, False],
        [False, True],
    )

    assert slots == [False, False]


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


def test_startup_restore_target_prefers_newer_legacy_controls_reserve(opt_module):
    coordinator = _coordinator(
        opt_module,
        "amber",
        hardware_backup_reserve=0.2,
        _user_backup_reserve=45,
        optimization_backup_reserve=30,
    )

    assert coordinator._configured_startup_backup_reserve() == (
        45,
        "persisted user backup reserve",
    )


def test_startup_restore_target_prefers_legacy_controls_reserve_during_migration(
    opt_module,
):
    coordinator = _coordinator(
        opt_module,
        "amber",
        hardware_backup_reserve=0.45,
        _user_backup_reserve=45,
        optimization_backup_reserve=30,
    )
    coordinator._entry.data = {"hardware_backup_reserve": 0.2}

    assert coordinator._configured_startup_backup_reserve() == (
        45,
        "persisted user backup reserve",
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


def test_set_settings_reset_battery_specs_re_detects_without_zeroing_model(
    opt_module,
):
    coordinator = _coordinator(opt_module, "amber")
    coordinator.entry_id = "entry-1"
    coordinator._entry.data = {
        "battery_capacity_wh": 13500,
        "max_charge_w": 5000,
        "max_discharge_w": 5000,
    }
    coordinator._entry.options = dict(coordinator._entry.data)
    coordinator._battery_specs_source = "manual"
    coordinator._enabled = False
    detected = []
    defaults_seen = []
    optimizer_updates = []

    async def _auto_detect_battery_specs():
        detected.append(True)
        defaults_seen.append(
            (
                coordinator._config.battery_capacity_wh,
                coordinator._config.max_charge_w,
                coordinator._config.max_discharge_w,
            )
        )
        coordinator._config.battery_capacity_wh = 16000
        coordinator._config.max_charge_w = 6000
        coordinator._config.max_discharge_w = 5500
        coordinator._battery_specs_source = "auto"

    coordinator._auto_detect_battery_specs = _auto_detect_battery_specs
    coordinator._optimizer = SimpleNamespace(
        update_config=lambda **values: optimizer_updates.append(values)
    )

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

    result = asyncio.run(
        coordinator.set_settings(
            {
                "battery_capacity_wh": 0,
                "max_charge_w": 0,
                "max_discharge_w": 0,
            }
        )
    )

    assert result["success"] is True
    assert detected == [True]
    assert defaults_seen == [(13500, 5000, 5000)]
    assert coordinator._entry.data == {}
    assert coordinator._entry.options == {}
    assert coordinator._battery_specs_source == "auto"
    assert optimizer_updates == [
        {
            "capacity_wh": 16000,
            "max_charge_w": 6000,
            "max_discharge_w": 5500,
        }
    ]


def test_set_settings_ev_participation_allows_options_reload(opt_module):
    coordinator = _coordinator(
        opt_module,
        "amber",
        optimization_ev_integration=False,
    )
    coordinator.entry_id = "entry-1"
    coordinator._ev_integration_enabled = False
    coordinator._enabled = False
    entry_data = {}

    class _ConfigEntries:
        def async_update_entry(self, entry, **kwargs):
            if "options" in kwargs:
                entry.options = kwargs["options"]

    coordinator.hass = SimpleNamespace(
        data={"power_sync": {"entry-1": entry_data}},
        config_entries=_ConfigEntries(),
    )

    result = asyncio.run(coordinator.set_settings({"ev_integration": True}))

    assert result["success"] is True
    assert coordinator._ev_integration_enabled is True
    assert coordinator._entry.options["optimization_ev_integration"] is True
    assert "_skip_reload" not in entry_data


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
    opt_module.dt_util.now = lambda: datetime(
        2026, 7, 13, 8, 30, tzinfo=timezone.utc
    )
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
        coordinator._reference_export_bridge_windows(
            short_export.schedule,
            export_allowed,
            export_allowed,
        ),
        solar,
        load,
    )
    coordinator._set_forecast_bridge_reserve_recommendation(
        full_window_export,
        coordinator._reference_export_bridge_windows(
            full_window_export.schedule,
            export_allowed,
            export_allowed,
        ),
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
        assert (
            recommendation["forecast_bridge_boundary_source"]
            == "bounded_priority_window"
        )


def test_auto_apply_forecast_bridge_uses_current_local_day_only(opt_module):
    """Ticket #243: a future-day bridge must not raise today's scalar floor."""
    coordinator = _coordinator(
        opt_module,
        "flow_power",
        optimization_backup_reserve=0.05,
        optimization_manual_reserve=0.05,
        optimization_auto_apply_reserve=True,
        hardware_backup_reserve=0.05,
    )
    coordinator._auto_apply_reserve_enabled = True
    coordinator._manual_backup_reserve = 0.05
    coordinator._config.backup_reserve = 0.05
    coordinator._config.battery_capacity_wh = 40000
    coordinator._config.interval_minutes = 60
    coordinator._optimizer = SimpleNamespace(efficiency=1.0)

    local_tz = timezone(timedelta(hours=10))
    opt_module.dt_util.as_local = lambda value: value.astimezone(local_tz)
    opt_module.dt_util.now = lambda: datetime(
        2026, 5, 3, 14, 0, tzinfo=timezone.utc
    )
    # UTC dates differ while both values fall on 4 May in the configured
    # local timezone. Comparing in a slot's own offset would skip this window.
    start = datetime(2026, 5, 4, 0, 30, tzinfo=timezone.utc)
    export_slots = {0, 24}
    charge_slots = {9, 44}
    actions = [
        opt_module.ScheduleAction(
            timestamp=start + timedelta(hours=idx),
            action=(
                "export"
                if idx in export_slots
                else ("charge" if idx in charge_slots else "self_consumption")
            ),
            power_w=5000 if idx in export_slots or idx in charge_slots else 0,
            soc=0.50,
            battery_charge_w=5000 if idx in charge_slots else 0,
            battery_discharge_w=5000 if idx in export_slots else 0,
        )
        for idx in range(60)
    ]
    schedule = opt_module.OptimizationSchedule(actions, 0, 0, start)
    load_forecast = [
        2.0 if 1 <= idx <= 8 or 25 <= idx <= 43 else 0.0
        for idx in range(len(actions))
    ]
    result = SimpleNamespace(
        schedule=schedule,
        reserve_recommendation={"suggested_optimizer_reserve_percent": 5},
    )

    windows = coordinator._reference_export_bridge_windows(
        schedule,
        [True] * len(actions),
        [False] * len(actions),
    )
    assert windows == [
        (0, 1, "manual_baseline_export_episode"),
        (24, 25, "manual_baseline_export_episode"),
    ]

    coordinator._set_forecast_bridge_reserve_recommendation(
        result,
        windows,
        None,
        load_forecast,
    )

    recommendation = result.reserve_recommendation
    assert recommendation["forecast_bridge_kwh"] == pytest.approx(16.0)
    assert recommendation["forecast_bridge_reserve_percent"] == 40
    assert recommendation["suggested_optimizer_reserve_percent"] == 45
    assert recommendation["forecast_bridge_export_window_start"] == (
        actions[0].timestamp.isoformat()
    )


@pytest.mark.parametrize(
    "unknown_timestamp",
    [None, datetime(2026, 5, 4, 0, 30)],
)
def test_auto_apply_forecast_bridge_ignores_unknown_window_day(
    opt_module,
    unknown_timestamp,
):
    """An unknown local date must not leak a future bridge or crash metadata."""
    coordinator = _coordinator(
        opt_module,
        "flow_power",
        optimization_backup_reserve=0.05,
        optimization_manual_reserve=0.05,
        optimization_auto_apply_reserve=True,
        hardware_backup_reserve=0.05,
    )
    coordinator._auto_apply_reserve_enabled = True
    coordinator._manual_backup_reserve = 0.05
    coordinator._config.backup_reserve = 0.05
    coordinator._config.battery_capacity_wh = 40000
    coordinator._config.interval_minutes = 60
    coordinator._optimizer = SimpleNamespace(efficiency=1.0)

    actions = [
        opt_module.ScheduleAction(
            timestamp=unknown_timestamp,
            action="export" if idx == 0 else "self_consumption",
            power_w=5000 if idx == 0 else 0,
            soc=0.50,
            battery_discharge_w=5000 if idx == 0 else 0,
        )
        for idx in range(10)
    ]
    result = SimpleNamespace(
        schedule=opt_module.OptimizationSchedule(actions, 0, 0, None),
        reserve_recommendation={"suggested_optimizer_reserve_percent": 5},
    )

    coordinator._set_forecast_bridge_reserve_recommendation(
        result,
        [(0, 1, "manual_baseline_export_episode")],
        None,
        [0.0] + [4.0] * 9,
    )

    recommendation = result.reserve_recommendation
    assert recommendation["suggested_optimizer_reserve_percent"] == 5
    assert recommendation["needs_optimizer_reserve_raise"] is False
    assert "forecast_bridge_kwh" not in recommendation


def test_auto_apply_forecast_bridge_uses_manual_episode_for_flat_export_tariff(
    opt_module,
):
    """Ticket #286: generic all-horizon permission must not erase the bridge."""
    coordinator = _coordinator(
        opt_module,
        "octopus",
        optimization_backup_reserve=0.70,
        optimization_manual_reserve=0.35,
        optimization_auto_apply_reserve=True,
        hardware_backup_reserve=0.08,
    )
    coordinator._auto_apply_reserve_enabled = True
    coordinator._manual_backup_reserve = 0.35
    coordinator._config.backup_reserve = 0.70
    coordinator._config.battery_capacity_wh = 40000
    coordinator._config.interval_minutes = 60
    coordinator._startup_backup_reserve = 8
    coordinator._optimizer = SimpleNamespace(efficiency=1.0)

    start = datetime(2026, 7, 18, 17, 0, tzinfo=timezone.utc)
    opt_module.dt_util.now = lambda: datetime(
        2026, 7, 18, 8, 30, tzinfo=timezone.utc
    )
    actions = []
    for idx in range(12):
        is_export = idx in {2, 3}
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
    result = SimpleNamespace(
        schedule=opt_module.OptimizationSchedule(actions, 0, 0, start),
        reserve_recommendation={"suggested_optimizer_reserve_percent": 70},
    )

    windows = coordinator._reference_export_bridge_windows(
        result.schedule,
        [True] * len(actions),
        [False] * len(actions),
    )
    coordinator._set_forecast_bridge_reserve_recommendation(
        result,
        windows,
        None,
        [0.0] * 4 + [1.0] * 4 + [0.0] * 4,
    )

    assert windows == [(2, 4, "manual_baseline_export_episode")]
    recommendation = result.reserve_recommendation
    assert recommendation["manual_optimizer_reserve_percent"] == 35
    assert recommendation["forecast_bridge_kwh"] == pytest.approx(4.0)
    assert recommendation["forecast_bridge_reserve_percent"] == 10
    assert recommendation["suggested_optimizer_reserve_percent"] == 45
    assert recommendation["needs_optimizer_reserve_raise"] is True
    assert (
        recommendation["forecast_bridge_boundary_source"]
        == "manual_baseline_export_episode"
    )


def test_reference_export_bridge_window_respects_export_denial(opt_module):
    coordinator = _coordinator(opt_module, "flow_power")
    start = datetime(2026, 7, 18, 17, 0, tzinfo=timezone.utc)
    actions = [
        opt_module.ScheduleAction(
            timestamp=start + timedelta(minutes=5 * idx),
            action="export" if idx in {2, 3} else "self_consumption",
            power_w=2000 if idx in {2, 3} else 0,
            soc=0.50,
            battery_discharge_w=2000 if idx in {2, 3} else 0,
        )
        for idx in range(8)
    ]
    schedule = opt_module.OptimizationSchedule(actions, 0, 0, start)

    windows = coordinator._reference_export_bridge_windows(
        schedule,
        [True, True, True, True, False, False, True, True],
        [False, True, True, True, True, True, False, False],
    )

    assert windows == [(2, 4, "bounded_priority_window")]


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


def test_cost_neutral_setting_atomically_disables_profit_max(opt_module):
    coordinator = _coordinator(opt_module, "flow_power", profit_max=True)
    updates, run_calls, background_tasks = _prepare_enabled_settings_coordinator(
        coordinator
    )

    result = asyncio.run(
        coordinator.set_settings({"cost_neutral_enabled": True})
    )

    assert result["success"] is True
    assert result["changes"] == ["cost_neutral_enabled: True"]
    assert coordinator._config.cost_neutral_enabled is True
    assert coordinator._config.profit_max_enabled is False
    assert updates[-1]["options"]["cost_neutral_enabled"] is True
    assert updates[-1]["options"]["profit_max_enabled"] is False
    assert run_calls == []
    assert background_tasks == ["powersync_settings_reoptimize"]


def test_daily_supply_charge_setting_persists_and_reoptimizes(opt_module):
    coordinator = _coordinator(opt_module, "flow_power")
    updates, run_calls, background_tasks = _prepare_enabled_settings_coordinator(
        coordinator
    )

    result = asyncio.run(
        coordinator.set_settings({"daily_supply_charge": 1.23})
    )

    assert result["success"] is True
    assert result["changes"] == ["daily_supply_charge: 1.23"]
    assert updates[-1]["data"]["daily_supply_charge"] == pytest.approx(1.23)
    assert updates[-1]["options"]["daily_supply_charge"] == pytest.approx(1.23)
    assert run_calls == []
    assert background_tasks == ["powersync_settings_reoptimize"]


def test_daily_supply_charge_setting_rejects_invalid_value_before_writes(opt_module):
    coordinator = _coordinator(opt_module, "flow_power")
    updates, _, background_tasks = _prepare_enabled_settings_coordinator(
        coordinator
    )

    result = asyncio.run(
        coordinator.set_settings({
            "daily_supply_charge": -1,
            "profit_max_enabled": True,
        })
    )

    assert result == {
        "success": False,
        "error": "Daily supply charge must be a non-negative number",
        "changes": [],
    }
    assert updates == []
    assert coordinator._config.profit_max_enabled is False
    assert background_tasks == []


def test_zero_daily_supply_charge_keeps_legacy_monthly_fallback(opt_module):
    coordinator = _coordinator(
        opt_module,
        "flow_power",
        daily_supply_charge=0.0,
        monthly_supply_charge=31.0,
    )

    value, source = coordinator._daily_supply_charge_for_cost_neutral(
        datetime(2026, 8, 2, tzinfo=timezone.utc)
    )

    assert value == pytest.approx(1.0)
    assert source == "configured_monthly"


def _cost_neutral_accounting_coordinator(
    opt_module,
    provider: str,
    *,
    supply_charge: float = 1.0,
):
    coordinator = _coordinator(
        opt_module,
        provider,
        daily_supply_charge=supply_charge,
    )
    coordinator._config.cost_neutral_enabled = True
    coordinator._config.interval_minutes = 60
    coordinator._actual_import_cost_today = 0.0
    coordinator._actual_export_earnings_today = 0.0
    coordinator._last_cost_date = None
    coordinator._last_cost_tracking_time = None
    coordinator._last_import_bonus_group_ids = None
    coordinator._last_import_bonus_caps_by_group = {}
    coordinator._last_export_bonus_group_ids = None
    coordinator._last_export_bonus_caps_by_group = {}
    coordinator._optimizer = SimpleNamespace(
        capacity_kwh=10.0,
        efficiency=1.0,
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
        _natural_self_consumption_floor=lambda soc: 0.0,
        _grid_export_limit_kw_for_range=lambda start, end: None,
        _allocate_capped_bonus=lambda flows, *args, **kwargs: [0.0] * len(flows),
    )
    return coordinator


@pytest.mark.parametrize("provider", _registered_electricity_providers())
def test_cost_neutral_builds_tomorrow_budget_for_every_electricity_provider(
    opt_module,
    provider,
):
    coordinator = _cost_neutral_accounting_coordinator(opt_module, provider)
    local_tz = timezone(timedelta(hours=10))
    now = datetime(2026, 8, 1, 22, 0, tzinfo=local_tz)
    timestamps = [now + timedelta(hours=idx) for idx in range(4)]

    plan, status = coordinator._cost_neutral_solve_inputs(
        now=now,
        timestamps=timestamps,
        import_prices=[0.20] * 4,
        export_prices=[0.10] * 4,
        export_bonus_prices=None,
        export_bonus_cap_kwh=None,
        import_bonus_prices=None,
        import_bonus_cap_kwh=None,
        solar=[0.0] * 4,
        load=[1.0] * 4,
        current_soc=0.0,
    )

    assert plan is not None
    assert plan.day_ids == [
        "2026-08-01",
        "2026-08-01",
        "2026-08-02",
        "2026-08-02",
    ]
    assert plan.earnings_caps_by_day == pytest.approx({
        "2026-08-01": 1.2,
        "2026-08-02": 1.4,
    })
    assert set(status["days"]) == {"2026-08-01", "2026-08-02"}
    assert status["current_day"] == "2026-08-01"


def test_cost_neutral_uses_each_target_month_for_monthly_supply_charge(
    opt_module,
):
    coordinator = _cost_neutral_accounting_coordinator(
        opt_module,
        "globird",
        supply_charge=0.0,
    )
    coordinator._entry.options.pop("daily_supply_charge", None)
    coordinator._entry.options["monthly_supply_charge"] = 61.0
    now = datetime(2026, 8, 31, 23, 0, tzinfo=timezone.utc)

    plan, status = coordinator._cost_neutral_solve_inputs(
        now=now,
        timestamps=[now, now + timedelta(hours=1)],
        import_prices=[0.0, 0.0],
        export_prices=[0.0, 0.0],
        export_bonus_prices=None,
        export_bonus_cap_kwh=None,
        import_bonus_prices=None,
        import_bonus_cap_kwh=None,
        solar=[0.0, 0.0],
        load=[0.0, 0.0],
        current_soc=0.5,
    )

    assert plan is not None
    assert status["days"]["2026-08-31"]["supply_charge"]["value"] == pytest.approx(
        61.0 / 31,
        abs=1e-4,
    )
    assert status["days"]["2026-09-01"]["supply_charge"]["value"] == pytest.approx(
        61.0 / 30,
        abs=1e-4,
    )


@pytest.mark.parametrize(
    ("local_day", "expected_slots"),
    [
        (datetime(2026, 10, 4), 23),
        (datetime(2026, 4, 5), 25),
    ],
)
def test_cost_neutral_groups_dst_horizons_by_ha_local_date(
    opt_module,
    local_day,
    expected_slots,
):
    coordinator = _cost_neutral_accounting_coordinator(opt_module, "other")
    local_tz = ZoneInfo("Australia/Sydney")
    local_start = local_day.replace(tzinfo=local_tz)
    local_end = (local_day + timedelta(days=1)).replace(tzinfo=local_tz)
    cursor = local_start.astimezone(timezone.utc)
    end_utc = local_end.astimezone(timezone.utc)
    timestamps = []
    while cursor < end_utc:
        timestamps.append(cursor)
        cursor += timedelta(hours=1)

    plan, _ = coordinator._cost_neutral_solve_inputs(
        now=local_start,
        timestamps=timestamps,
        import_prices=[0.0] * len(timestamps),
        export_prices=[0.0] * len(timestamps),
        export_bonus_prices=None,
        export_bonus_cap_kwh=None,
        import_bonus_prices=None,
        import_bonus_cap_kwh=None,
        solar=[0.0] * len(timestamps),
        load=[0.0] * len(timestamps),
        current_soc=0.5,
    )

    assert plan is not None
    assert len(plan.day_ids) == expected_slots
    assert set(plan.day_ids) == {local_start.date().isoformat()}
    assert plan.timezone == "Australia/Sydney"


def test_cost_neutral_48_hour_horizon_contains_three_local_dates(opt_module):
    coordinator = _cost_neutral_accounting_coordinator(opt_module, "amber")
    local_tz = ZoneInfo("Australia/Sydney")
    now = datetime(2026, 8, 1, 12, 0, tzinfo=local_tz)
    timestamps = [now + timedelta(hours=idx) for idx in range(48)]

    plan, status = coordinator._cost_neutral_solve_inputs(
        now=now,
        timestamps=timestamps,
        import_prices=[0.0] * 48,
        export_prices=[0.0] * 48,
        export_bonus_prices=None,
        export_bonus_cap_kwh=None,
        import_bonus_prices=None,
        import_bonus_cap_kwh=None,
        solar=[0.0] * 48,
        load=[0.0] * 48,
        current_soc=0.5,
    )

    assert plan is not None
    assert list(status["days"]) == [
        "2026-08-01",
        "2026-08-02",
        "2026-08-03",
    ]


def test_cost_neutral_drops_stale_measured_counters_after_midnight(opt_module):
    coordinator = _cost_neutral_accounting_coordinator(opt_module, "flow_power")
    coordinator._actual_import_cost_today = 99.0
    coordinator._actual_export_earnings_today = 25.0
    coordinator._last_cost_date = "2026-08-01"
    coordinator._last_cost_tracking_time = datetime(
        2026,
        8,
        1,
        23,
        55,
        tzinfo=timezone.utc,
    )
    now = datetime(2026, 8, 2, 0, 1, tzinfo=timezone.utc)

    plan, status = coordinator._cost_neutral_solve_inputs(
        now=now,
        timestamps=[now],
        import_prices=[0.0],
        export_prices=[0.0],
        export_bonus_prices=None,
        export_bonus_cap_kwh=None,
        import_bonus_prices=None,
        import_bonus_cap_kwh=None,
        solar=[0.0],
        load=[0.0],
        current_soc=0.5,
    )

    assert plan is not None
    assert plan.earnings_caps_by_day == {"2026-08-02": pytest.approx(1.0)}
    assert status["measured_import_cost"] == 0.0
    assert status["measured_export_earnings"] == 0.0


def test_cost_neutral_keeps_today_covered_and_builds_tomorrow_target(
    opt_module,
):
    coordinator = _cost_neutral_accounting_coordinator(opt_module, "globird")
    coordinator._actual_import_cost_today = 1.0
    coordinator._actual_export_earnings_today = 3.0
    local_tz = timezone(timedelta(hours=10))
    now = datetime(2026, 8, 1, 22, 0, tzinfo=local_tz)
    coordinator._last_cost_date = "2026-08-01"
    coordinator._last_cost_tracking_time = now
    timestamps = [now + timedelta(hours=idx) for idx in range(4)]

    plan, status = coordinator._cost_neutral_solve_inputs(
        now=now,
        timestamps=timestamps,
        import_prices=[0.20] * 4,
        export_prices=[0.10] * 4,
        export_bonus_prices=None,
        export_bonus_cap_kwh=None,
        import_bonus_prices=None,
        import_bonus_cap_kwh=None,
        solar=[0.0] * 4,
        load=[1.0] * 4,
        current_soc=0.0,
    )

    assert plan is not None
    assert plan.earnings_caps_by_day["2026-08-01"] == 0.0
    assert plan.earnings_caps_by_day["2026-08-02"] == pytest.approx(1.4)
    assert status["days"]["2026-08-01"]["reason"] == (
        "already_covered_by_measured_or_natural_export"
    )
    assert status["days"]["2026-08-02"][
        "battery_export_earnings_cap"
    ] == pytest.approx(1.4)


def test_cost_neutral_uses_billable_prices_not_lp_overlays(opt_module):
    coordinator = _coordinator(opt_module, "amber")
    coordinator._last_display_import_prices = [0.20, 0.30]
    coordinator._last_display_export_prices = [0.10, -0.05]

    import_prices, export_prices = coordinator._cost_neutral_settlement_prices(
        [9.0, 8.0, 7.0],
        [6.0, 5.0, 4.0],
    )

    assert import_prices == [0.20, 0.30, 0.30]
    assert export_prices == [0.10, -0.05, -0.05]


def test_cost_neutral_uses_base_rates_before_quota_bonus(opt_module):
    coordinator = _coordinator(opt_module, "covau")
    coordinator._last_display_import_prices = [0.0]
    coordinator._last_display_export_prices = [0.12]
    coordinator._last_settlement_import_prices = [0.30]
    coordinator._last_settlement_export_prices = [0.06]

    import_prices, export_prices = coordinator._cost_neutral_settlement_prices(
        [0.30],
        [0.06],
    )

    assert import_prices == [0.30]
    assert export_prices == [0.06]


@pytest.mark.parametrize(
    ("local_day", "expected_slots"),
    [
        (datetime(2026, 10, 4), 23),
        (datetime(2026, 4, 5), 25),
    ],
)
def test_production_price_timeline_is_dst_safe(
    opt_module,
    monkeypatch,
    local_day,
    expected_slots,
):
    coordinator = _coordinator(opt_module, "globird")
    coordinator._config.interval_minutes = 60
    coordinator._pending_price_timestamps = None
    coordinator._last_price_timestamps = None
    local_tz = ZoneInfo("Australia/Sydney")
    local_start = local_day.replace(tzinfo=local_tz)
    monkeypatch.setattr(opt_module.dt_util, "now", lambda: local_start)

    timestamps = coordinator._price_timestamps(26)
    utc_timestamps = [value.astimezone(timezone.utc) for value in timestamps]

    assert all(
        right - left == timedelta(hours=1)
        for left, right in zip(utc_timestamps, utc_timestamps[1:])
    )
    assert sum(
        value.date() == local_start.date()
        for value in timestamps
    ) == expected_slots


def test_optimizer_settings_reject_both_exclusive_modes(opt_module):
    coordinator = _coordinator(opt_module, "flow_power", profit_max=False)
    _prepare_enabled_settings_coordinator(coordinator)

    result = asyncio.run(coordinator.set_settings({
        "profit_max_enabled": True,
        "cost_neutral_enabled": True,
    }))

    assert result == {
        "success": False,
        "error": "Profit Max and Cost Neutral cannot both be enabled",
        "changes": [],
    }
    assert coordinator._config.profit_max_enabled is False
    assert coordinator._config.cost_neutral_enabled is False


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


def test_globird_zerohero_groups_bonus_caps_by_local_date(
    opt_module,
    monkeypatch,
):
    aest = timezone(timedelta(hours=10))
    start = datetime(2026, 8, 6, 20, 5, tzinfo=aest)
    monkeypatch.setattr(opt_module.dt_util, "now", lambda: start)
    coordinator = _coordinator(
        opt_module,
        "globird",
        globird_plan="zerohero_custom",
        globird_zerohero_start="18:00",
        globird_zerohero_end="21:00",
        globird_zerohero_export_cap_kwh=15.0,
        globird_zerohero_super_export_rate=0.15,
    )
    coordinator._config.horizon_hours = 48
    n = 48 * 12
    coordinator._last_price_timestamps = [
        start + timedelta(minutes=5 * idx) for idx in range(n)
    ]
    coordinator._actual_zerohero_bonus_export_kwh_today = 12.06

    coordinator._apply_zerohero_optimizer_inputs(
        [0.33] * n,
        [0.0] * n,
    )

    assert coordinator._last_zerohero_bonus_cap_kwh == pytest.approx(2.94)
    assert coordinator._last_export_bonus_caps_by_group == {
        "2026-08-06": pytest.approx(2.94),
        "2026-08-07": pytest.approx(15.0),
        "2026-08-08": pytest.approx(15.0),
    }
    assert {
        group
        for group in coordinator._last_export_bonus_group_ids
        if group is not None
    } == {"2026-08-06", "2026-08-07", "2026-08-08"}


def test_globird_zerohero_exhausted_today_keeps_future_bonus_windows(
    opt_module,
    monkeypatch,
):
    aest = timezone(timedelta(hours=10))
    start = datetime(2026, 8, 6, 17, 0, tzinfo=aest)
    monkeypatch.setattr(opt_module.dt_util, "now", lambda: start)
    coordinator = _coordinator(
        opt_module,
        "globird",
        globird_plan="zerohero_custom",
        globird_zerohero_start="18:00",
        globird_zerohero_end="21:00",
        globird_zerohero_export_cap_kwh=15.0,
        globird_zerohero_super_export_rate=0.15,
    )
    n = 30 * 12
    timestamps = [start + timedelta(minutes=5 * idx) for idx in range(n)]
    coordinator._last_price_timestamps = timestamps
    coordinator._actual_zerohero_bonus_export_kwh_today = 15.0
    import_prices = [0.33] * n

    coordinator._apply_zerohero_optimizer_inputs(import_prices, [0.0] * n)

    assert coordinator._last_zerohero_bonus_cap_kwh == pytest.approx(0.0)
    assert coordinator._last_export_bonus_caps_by_group == {
        "2026-08-07": pytest.approx(15.0),
    }
    current_slots = [
        idx
        for idx, timestamp in enumerate(timestamps)
        if timestamp.date() == start.date() and 18 <= timestamp.hour < 21
    ]
    future_slots = [
        idx
        for idx, timestamp in enumerate(timestamps)
        if timestamp.date() == (start + timedelta(days=1)).date()
        and 18 <= timestamp.hour < 21
    ]
    assert current_slots and future_slots
    assert coordinator._last_export_bonus_group_ids[current_slots[0]] is None
    assert coordinator._last_export_bonus_group_ids[future_slots[0]] == "2026-08-07"
    assert all(import_prices[idx] == pytest.approx(5.33) for idx in current_slots)
    assert _true_indexes(
        coordinator._battery_export_allowed_slots(n, [0.0] * n)
    ) == future_slots
    assert _true_indexes(
        coordinator._priority_export_slots_for_run(n, [0.0] * n)
    ) == future_slots


def test_globird_zerohero_same_day_keeps_remaining_bonus_cap(
    opt_module,
    monkeypatch,
):
    aest = timezone(timedelta(hours=10))
    start = datetime(2026, 8, 6, 17, 0, tzinfo=aest)
    monkeypatch.setattr(opt_module.dt_util, "now", lambda: start)
    coordinator = _coordinator(
        opt_module,
        "globird",
        globird_plan="zerohero_custom",
        globird_zerohero_start="18:00",
        globird_zerohero_end="21:00",
        globird_zerohero_export_cap_kwh=15.0,
        globird_zerohero_super_export_rate=0.15,
    )
    n = 24 * 12
    coordinator._last_price_timestamps = [
        start + timedelta(minutes=5 * idx) for idx in range(n)
    ]
    coordinator._actual_zerohero_bonus_export_kwh_today = 6.0

    coordinator._apply_zerohero_optimizer_inputs(
        [0.33] * n,
        [0.0] * n,
    )

    assert coordinator._last_zerohero_bonus_cap_kwh == pytest.approx(9.0)
    assert coordinator._last_export_bonus_caps_by_group == {
        "2026-08-06": pytest.approx(9.0),
    }


def test_globird_zerohero_overnight_groups_slots_by_timestamp_local_date(
    opt_module,
    monkeypatch,
):
    """Overnight windows retain each slot's local calendar date as its group."""
    aest = timezone(timedelta(hours=10))
    start = datetime(2026, 8, 6, 22, 55, tzinfo=aest)
    monkeypatch.setattr(opt_module.dt_util, "now", lambda: start)
    coordinator = _coordinator(
        opt_module,
        "globird",
        globird_plan="zerohero_custom",
        globird_zerohero_start="23:00",
        globird_zerohero_end="02:00",
        globird_zerohero_export_cap_kwh=15.0,
        globird_zerohero_super_export_rate=0.15,
    )
    n = 8 * 12
    timestamps = [start + timedelta(minutes=5 * idx) for idx in range(n)]
    coordinator._last_price_timestamps = timestamps
    coordinator._actual_zerohero_bonus_export_kwh_today = 6.0

    coordinator._apply_zerohero_optimizer_inputs([0.33] * n, [0.0] * n)

    assert coordinator._last_export_bonus_caps_by_group == {
        "2026-08-06": pytest.approx(9.0),
        "2026-08-07": pytest.approx(15.0),
    }


def test_globird_zerocharge_limits_grid_charge_to_configured_window(
    opt_module,
    monkeypatch,
):
    monkeypatch.setattr(
        opt_module.dt_util,
        "now",
        lambda: datetime(
            2026,
            7,
            7,
            13,
            50,
            tzinfo=timezone(timedelta(hours=10)),
        ),
    )
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

    coordinator._actual_zerocharge_import_kwh_today = 1550.0
    assert coordinator._grid_charge_allowed_slots(
        import_prices=[0.0] * 6,
        solar_forecast=[0.0] * 6,
        load_forecast=[0.0] * 6,
        current_soc=0.50,
    ) == [False] * 6


def test_globird_zerocharge_monthly_exhaustion_blocks_tomorrows_allowance(
    opt_module,
    monkeypatch,
):
    coordinator = _coordinator(
        opt_module,
        "globird",
        globird_plan="zerohero_custom",
        globird_zerohero_start="18:00",
        globird_zerohero_end="21:00",
        globird_zerohero_export_cap_kwh=15.0,
        globird_zerohero_super_export_rate=0.15,
        globird_zerocharge_start="11:00",
        globird_zerocharge_end="14:00",
        globird_zerocharge_import_cap_kwh=50.0,
    )
    aest = timezone(timedelta(hours=10))
    start = datetime(2026, 8, 1, 17, 15, tzinfo=aest)
    monkeypatch.setattr(opt_module.dt_util, "now", lambda: start)
    n = 24 * 12
    timestamps = [start + timedelta(minutes=5 * idx) for idx in range(n)]
    coordinator._last_price_timestamps = timestamps
    coordinator._actual_zerocharge_import_kwh_today = 1550.0

    allowed = coordinator._grid_charge_allowed_slots(
        import_prices=[0.33] * n,
        solar_forecast=[0.0] * n,
        load_forecast=[0.8] * n,
        current_soc=1.0,
    )

    tomorrow = (start + timedelta(days=1)).date()
    tomorrow_zerocharge = [
        idx
        for idx, timestamp in enumerate(timestamps)
        if timestamp.date() == tomorrow and 11 <= timestamp.hour < 14
    ]
    assert len(tomorrow_zerocharge) == 36
    assert all(not allowed[idx] for idx in tomorrow_zerocharge)


def test_globird_zerocharge_uses_one_month_pool_for_multiple_local_days(
    opt_module,
    monkeypatch,
):
    coordinator = _coordinator(
        opt_module,
        "globird",
        globird_plan="zerohero_custom",
        globird_zerohero_start="18:00",
        globird_zerohero_end="21:00",
        globird_zerohero_export_cap_kwh=15.0,
        globird_zerohero_super_export_rate=0.15,
        globird_zerocharge_start="11:00",
        globird_zerocharge_end="14:00",
        globird_zerocharge_import_cap_kwh=50.0,
    )
    aest = timezone(timedelta(hours=10))
    start = datetime(2026, 8, 1, 17, 15, tzinfo=aest)
    monkeypatch.setattr(opt_module.dt_util, "now", lambda: start)
    n = 24 * 12
    timestamps = [start + timedelta(minutes=5 * idx) for idx in range(n)]
    coordinator._last_price_timestamps = timestamps
    coordinator._actual_zerocharge_import_kwh_today = 50.0
    import_prices = [0.33] * n
    export_prices = [0.0] * n

    coordinator._apply_zerohero_optimizer_inputs(import_prices, export_prices)

    month_key = "2026-08"
    assert coordinator._last_zerocharge_bonus_cap_kwh == pytest.approx(1500.0)
    assert coordinator._last_import_bonus_caps_by_group == {
        month_key: pytest.approx(1500.0),
    }
    active_groups = {
        group
        for group in coordinator._last_import_bonus_group_ids
        if group is not None
    }
    assert active_groups == {month_key}


def test_globird_zerocharge_keeps_month_to_date_pool_across_local_days(
    opt_module,
    monkeypatch,
):
    coordinator = _coordinator(
        opt_module,
        "globird",
        globird_plan="zerohero_custom",
        globird_zerocharge_start="11:00",
        globird_zerocharge_end="14:00",
        globird_zerocharge_import_cap_kwh=50.0,
    )
    aest = timezone(timedelta(hours=10))
    start = datetime(2026, 8, 1, 10, 55, tzinfo=aest)
    monkeypatch.setattr(opt_module.dt_util, "now", lambda: start)
    n = 30 * 12
    timestamps = [start + timedelta(minutes=5 * idx) for idx in range(n)]
    coordinator._last_price_timestamps = timestamps
    coordinator._actual_zerocharge_import_kwh_today = 12.5

    coordinator._apply_zerohero_optimizer_inputs(
        [0.33] * n,
        [0.0] * n,
    )

    assert coordinator._last_import_bonus_caps_by_group == {
        "2026-08": pytest.approx(1537.5),
    }


def test_globird_overnight_zerocharge_stays_in_same_month_at_midnight(opt_module):
    coordinator = _coordinator(
        opt_module,
        "globird",
        globird_plan="zerohero_custom",
        globird_zerocharge_start="23:00",
        globird_zerocharge_end="02:00",
        globird_zerocharge_import_cap_kwh=50.0,
    )
    aest = timezone(timedelta(hours=10))
    start = datetime(2026, 8, 1, 22, 55, tzinfo=aest)
    n = 4 * 12
    timestamps = [start + timedelta(minutes=5 * idx) for idx in range(n)]
    coordinator._last_price_timestamps = timestamps

    coordinator._apply_zerohero_optimizer_inputs(
        [0.33] * n,
        [0.0] * n,
    )

    active_groups = {
        group
        for group in coordinator._last_import_bonus_group_ids
        if group is not None
    }
    assert active_groups == {"2026-08"}
    assert coordinator._last_import_bonus_caps_by_group == {
        "2026-08": pytest.approx(1550.0),
    }


def test_globird_zerocharge_splits_august_and_september_pools(opt_module):
    coordinator = _coordinator(
        opt_module,
        "globird",
        globird_plan="zerohero_custom",
        globird_zerocharge_start="11:00",
        globird_zerocharge_end="14:00",
        globird_zerocharge_import_cap_kwh=50.0,
    )
    aest = timezone(timedelta(hours=10))
    start = datetime(2026, 8, 31, 10, 55, tzinfo=aest)
    timestamps = [start + timedelta(minutes=5 * idx) for idx in range(48 * 12)]
    coordinator._last_price_timestamps = timestamps

    coordinator._apply_zerohero_optimizer_inputs(
        [0.33] * len(timestamps),
        [0.0] * len(timestamps),
    )

    assert coordinator._last_import_bonus_caps_by_group == {
        "2026-08": pytest.approx(1550.0),
        "2026-09": pytest.approx(1500.0),
    }


def test_globird_zerocharge_stale_forecast_does_not_reset_current_month_state(
    opt_module,
    monkeypatch,
):
    aest = timezone(timedelta(hours=10))
    now = datetime(2026, 9, 1, 0, 5, tzinfo=aest)
    monkeypatch.setattr(opt_module.dt_util, "now", lambda: now)
    coordinator = _coordinator(
        opt_module,
        "globird",
        globird_plan="zerohero_jul_2026",
    )
    coordinator._set_zerocharge_period_state(
        period_key="2026-09",
        import_kwh=100.0,
        credit_value=5.0,
    )
    stale_start = datetime(2026, 8, 31, 12, 0, tzinfo=aest)
    coordinator._last_price_timestamps = [
        stale_start + timedelta(minutes=5 * idx) for idx in range(48 * 12)
    ]

    coordinator._apply_zerohero_optimizer_inputs(
        [0.33] * len(coordinator._last_price_timestamps),
        [0.0] * len(coordinator._last_price_timestamps),
    )

    assert coordinator._actual_zerocharge_period_key == "2026-09"
    assert coordinator._actual_zerocharge_import_kwh_month == pytest.approx(100.0)
    assert coordinator._last_import_bonus_caps_by_group == {
        "2026-08": pytest.approx(1550.0),
        "2026-09": pytest.approx(1400.0),
    }


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
    published = []
    coordinator.get_api_data = lambda: {
        "charge_by_time_enabled": coordinator.charge_by_time_enabled,
        "config": {
            "charge_by_time_target_time": (
                coordinator._config.charge_by_time_target_time
            ),
            "charge_by_time_target_soc": int(
                round(coordinator._config.charge_by_time_target_soc * 100)
            ),
        },
    }
    coordinator.async_set_updated_data = published.append

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
    assert published == [coordinator.get_api_data()]
    assert background_tasks == ["powersync_settings_reoptimize"]


def test_charge_by_time_switch_setter_publishes_when_optimizer_disabled(opt_module):
    coordinator = _coordinator(
        opt_module,
        "flow_power",
        charge_by_time=False,
    )
    _prepare_enabled_settings_coordinator(coordinator)
    coordinator._enabled = False
    published = []
    coordinator.get_api_data = lambda: {
        "charge_by_time_enabled": coordinator.charge_by_time_enabled,
    }
    coordinator.async_set_updated_data = published.append

    assert coordinator.set_charge_by_time_enabled(True) is True
    assert published == [{"charge_by_time_enabled": True}]

    assert coordinator.set_charge_by_time_enabled(True) is False
    assert published == [{"charge_by_time_enabled": True}]


def test_combined_charge_by_time_settings_publish_one_final_snapshot(opt_module):
    coordinator = _coordinator(
        opt_module,
        "flow_power",
        charge_by_time=False,
        charge_by_time_target_time="17:15",
        charge_by_time_target_soc=1.0,
    )
    _updates, _run_calls, background_tasks = _prepare_enabled_settings_coordinator(
        coordinator
    )
    published = []
    coordinator.get_api_data = lambda: {
        "charge_by_time_enabled": coordinator.charge_by_time_enabled,
        "config": {
            "charge_by_time_target_time": (
                coordinator._config.charge_by_time_target_time
            ),
            "charge_by_time_target_soc": int(
                round(coordinator._config.charge_by_time_target_soc * 100)
            ),
        },
    }
    coordinator.async_set_updated_data = published.append

    result = asyncio.run(
        coordinator.set_settings(
            {
                "charge_by_time_enabled": True,
                "charge_by_time_target_time": "16:00",
                "charge_by_time_target_soc": 80,
            }
        )
    )

    assert result["success"] is True
    assert published == [
        {
            "charge_by_time_enabled": True,
            "config": {
                "charge_by_time_target_time": "16:00",
                "charge_by_time_target_soc": 80,
            },
        }
    ]
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


def test_covau_forecast_partitions_caps_by_adelaide_tariff_day(opt_module):
    now = datetime(2026, 5, 3, 0, 30, tzinfo=timezone.utc)  # 10:00 ACST
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
        timezone_token="Australia/Adelaide",
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
            opt_module.tariff_datetime(
                value,
                "Australia/Adelaide",
            ).date().isoformat()
            for value in active
        } == {"2026-05-03", "2026-05-04"}

    priority = coordinator._priority_export_slots_for_run(
        len(timestamps),
        prices[1],
    )
    priority_times = [timestamps[idx] for idx, value in enumerate(priority) if value]
    assert priority_times
    assert {
        opt_module.tariff_datetime(
            value,
            "Australia/Adelaide",
        ).date().isoformat()
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
    first = datetime(2026, 5, 3, 1, 35, tzinfo=timezone.utc)  # 11:05 ACST
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
        timezone_token="Australia/Adelaide",
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

    export_time = datetime(2026, 5, 3, 8, 35, tzinfo=timezone.utc)  # 18:05 ACST
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


def test_globird_status_reports_calendar_month_pool(opt_module, monkeypatch):
    now = datetime(2026, 8, 3, 13, 0, tzinfo=timezone(timedelta(hours=10)))
    monkeypatch.setattr(opt_module.dt_util, "now", lambda: now)
    coordinator = _coordinator(
        opt_module,
        "globird",
        globird_plan="zerohero_jul_2026",
    )
    coordinator._actual_zerocharge_import_kwh_today = 50.0

    status = coordinator._zerohero_cost_breakdown()

    assert status["zerocharge_period_key"] == "2026-08"
    assert status["zerocharge_monthly_cap_kwh"] == pytest.approx(1550.0)
    assert status["zerocharge_import_kwh_used"] == pytest.approx(50.0)
    assert status["zerocharge_import_kwh_remaining"] == pytest.approx(1500.0)


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


def test_tesla_tariff_auto_activates_grandfathered_zerohero_priority(opt_module):
    coordinator = _coordinator(
        opt_module,
        "globird",
        globird_plan="not_zerohero",
    )
    coordinator._tariff_schedule = {
        "plan_name": "Zerohero",
        "buy_rates": {"ON_PEAK": 0.53, "SUPER_OFF_PEAK": 0.0},
        "sell_rates": {"ON_PEAK": 0.10, "SUPER_OFF_PEAK": 0.0},
        "tou_periods": {
            "ON_PEAK": [
                {"fromHour": 18, "fromMinute": 0, "toHour": 21, "toMinute": 0}
            ],
            "SUPER_OFF_PEAK": [
                {"fromHour": 11, "fromMinute": 0, "toHour": 14, "toMinute": 0}
            ],
        },
    }
    coordinator._last_price_timestamps = [
        datetime(2026, 8, 4, 17, 30, tzinfo=timezone.utc)
        + timedelta(minutes=5 * idx)
        for idx in range(48)
    ]
    import_prices = [0.53] * 48
    export_prices = [0.0] * 48
    for idx in range(6, 42):
        export_prices[idx] = 0.10

    config = coordinator._zerohero_config()
    coordinator._apply_zerohero_optimizer_inputs(import_prices, export_prices)
    priority_slots = coordinator._priority_export_slots_for_run(48, export_prices)

    assert config is not None
    assert config.plan == "zerohero_pre_jul_2026"
    assert config.zerocharge_start == "11:00"
    assert _true_indexes(priority_slots) == list(range(6, 42))


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


def test_agl_reward_window_is_priority_export_only_with_a_usable_rate(opt_module):
    coordinator = _coordinator(opt_module, "agl")
    n = 51
    coordinator._last_price_timestamps = [
        datetime(2026, 7, 31, 16, 55, tzinfo=timezone.utc)
        + timedelta(minutes=5 * idx)
        for idx in range(n)
    ]
    export_prices = [0.26] * n
    export_prices[12] = 0.0

    slots = coordinator._priority_export_slots_for_run(n, export_prices)

    expected = list(range(1, 49))
    expected.remove(12)
    assert _true_indexes(slots) == expected


def test_flow_power_happy_hour_is_priority_export_without_profit_max(opt_module):
    coordinator = _coordinator(
        opt_module,
        "flow_power",
        profit_max=False,
        flow_power_state="NSW1",
    )

    slots = coordinator._priority_export_slots_for_run(288, [0.45] * 288)

    assert _true_indexes(slots) == list(range(108, 132))


def test_flow_power_extended_happy_hour_uses_selected_end(opt_module):
    """The four-hour plan extends priority export through the 21:00 slot."""
    coordinator = _coordinator(
        opt_module,
        "flow_power",
        profit_max=True,
        flow_power_state="NSW1",
        flow_power_happy_hour_end="21:30",
    )

    slots = coordinator._flow_power_export_window_slots(288)

    assert _true_indexes(slots) == list(range(108, 156))


def test_solar_only_charge_does_not_use_median_import_as_acquisition_cost(
    opt_module,
):
    coordinator = _coordinator(opt_module, "flow_power")
    coordinator._grid_charge_tracking_known = True
    coordinator._actual_charge_kwh_today = 31.0
    coordinator._actual_discharge_kwh_today = 0.5
    coordinator._actual_grid_charge_kwh_today = 0.0
    coordinator._actual_grid_charge_cost_today = 0.0

    acquisition_cost = coordinator._acquisition_cost_for_run(
        import_prices=[0.43, 0.532, 0.43],
        current_soc=0.94,
        capacity_wh=32_000,
    )

    assert acquisition_cost == 0.0


def test_solar_top_up_blends_unknown_carry_over_with_free_solar(opt_module):
    coordinator = _coordinator(opt_module, "flow_power")
    coordinator._grid_charge_tracking_known = True
    coordinator._actual_charge_kwh_today = 9.4
    coordinator._actual_discharge_kwh_today = 0.0
    coordinator._actual_grid_charge_kwh_today = 0.0
    coordinator._actual_grid_charge_cost_today = 0.0

    acquisition_cost = coordinator._acquisition_cost_for_run(
        import_prices=[0.43, 0.532, 0.41],
        current_soc=0.94,
        capacity_wh=32_000,
    )

    current_stored_energy_kwh = 0.94 * 32.0
    expected_unknown_fraction = (
        current_stored_energy_kwh - 9.4
    ) / current_stored_energy_kwh
    assert acquisition_cost == pytest.approx(0.43 * expected_unknown_fraction)


def test_solar_filled_battery_does_not_price_entire_overnight_reserve(
    opt_module,
):
    """Regression for Discord #338 after the v2.12.999 provenance fix."""
    coordinator = _coordinator(opt_module, "flow_power")
    coordinator._grid_charge_tracking_known = True
    coordinator._actual_charge_kwh_today = 25.6
    coordinator._actual_discharge_kwh_today = 0.0
    coordinator._actual_grid_charge_kwh_today = 0.0
    coordinator._actual_grid_charge_cost_today = 0.0

    acquisition_cost = coordinator._acquisition_cost_for_run(
        import_prices=[0.43, 0.532, 0.41],
        current_soc=1.0,
        capacity_wh=32_000,
    )

    # The 20% overnight remainder stays conservatively valued, while the 80%
    # measured solar charge is free. Happy Hour's 35c rate is therefore not
    # vetoed by applying the 43c proxy to the full battery.
    assert acquisition_cost == pytest.approx(0.43 * 0.20)
    assert acquisition_cost < 0.35


def test_mixed_solar_and_grid_charge_blends_current_inventory_provenance(
    opt_module,
):
    """Ticket #338: a small measured grid top-up must not price all storage."""
    coordinator = _coordinator(opt_module, "flow_power")
    coordinator._grid_charge_tracking_known = True
    coordinator._actual_charge_kwh_today = 19.17
    coordinator._actual_discharge_kwh_today = 2.61
    coordinator._actual_grid_charge_kwh_today = 0.0284
    coordinator._actual_grid_charge_cost_today = 0.0119
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "energy_summary": {
                "charge_today_kwh": 19.17,
                "discharge_today_kwh": 2.61,
                "grid_import_today_kwh": 0.03,
            }
        }
    )

    acquisition_cost = coordinator._acquisition_cost_for_run(
        import_prices=[0.434, 0.532, 0.434],
        current_soc=1.0,
        capacity_wh=24_200,
    )

    expected = (0.0119 + (24.2 - 16.5316 - 0.0284) * 0.434) / 24.2
    assert acquisition_cost == pytest.approx(expected)
    assert acquisition_cost < 0.35


def test_unknown_charge_provenance_keeps_median_import_acquisition_cost(
    opt_module,
):
    coordinator = _coordinator(opt_module, "flow_power")
    coordinator._grid_charge_tracking_known = False
    coordinator._actual_charge_kwh_today = 31.0
    coordinator._actual_discharge_kwh_today = 0.5
    coordinator._actual_grid_charge_kwh_today = 0.0
    coordinator._actual_grid_charge_cost_today = 0.0

    acquisition_cost = coordinator._acquisition_cost_for_run(
        import_prices=[0.43, 0.532, 0.41],
        current_soc=0.94,
        capacity_wh=32_000,
    )

    assert acquisition_cost == pytest.approx(0.43)


def test_unknown_charge_provenance_blends_main_energy_summary(opt_module):
    """Use full-day counters when the private tracking state was lost."""
    coordinator = _coordinator(opt_module, "flow_power")
    coordinator._grid_charge_tracking_known = False
    coordinator._actual_grid_charge_kwh_today = 0.0
    coordinator._actual_grid_charge_cost_today = 0.0
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "energy_summary": {
                "pv_today_kwh": 37.07,
                "grid_import_today_kwh": 0.03,
                "charge_today_kwh": 17.86,
                "discharge_today_kwh": 2.18,
            }
        }
    )

    acquisition_cost = coordinator._acquisition_cost_for_run(
        import_prices=[0.43, 0.532, 0.43],
        current_soc=1.0,
        capacity_wh=24_200,
    )

    stored_energy_kwh = 24.2
    proven_solar_kwh = 17.86 - 2.18 - 0.03
    expected_unknown_fraction = (
        stored_energy_kwh - proven_solar_kwh
    ) / stored_energy_kwh
    assert acquisition_cost == pytest.approx(0.43 * expected_unknown_fraction)
    assert acquisition_cost < 0.35


def test_incomplete_private_tracking_uses_main_energy_summary(opt_module):
    """A newly enabled optimizer can have known-but-partial private totals."""
    coordinator = _coordinator(opt_module, "flow_power")
    coordinator._grid_charge_tracking_known = True
    coordinator._actual_charge_kwh_today = 0.0
    coordinator._actual_discharge_kwh_today = 0.0
    coordinator._actual_grid_charge_kwh_today = 0.0
    coordinator._actual_grid_charge_cost_today = 0.0
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "energy_summary": {
                "pv_today_kwh": 37.07,
                "grid_import_today_kwh": 0.03,
                "charge_today_kwh": 17.86,
                "discharge_today_kwh": 2.18,
            }
        }
    )

    acquisition_cost = coordinator._acquisition_cost_for_run(
        import_prices=[0.43, 0.532, 0.43],
        current_soc=1.0,
        capacity_wh=24_200,
    )

    assert acquisition_cost < 0.35


@pytest.mark.parametrize(
    "energy_summary",
    [
        {"charge_today_kwh": 10.0, "discharge_today_kwh": 2.0},
        {
            "charge_today_kwh": 10.0,
            "discharge_today_kwh": 2.0,
            "grid_import_today_kwh": -0.1,
        },
        {
            "charge_today_kwh": 10.0,
            "discharge_today_kwh": 2.0,
            "grid_import_today_kwh": "unknown",
        },
        {
            "charge_today_kwh": True,
            "discharge_today_kwh": False,
            "grid_import_today_kwh": False,
        },
    ],
)
def test_unknown_charge_provenance_keeps_conservative_fallback_for_bad_summary(
    opt_module,
    energy_summary,
):
    coordinator = _coordinator(opt_module, "flow_power")
    coordinator._grid_charge_tracking_known = False
    coordinator.energy_coordinator = SimpleNamespace(
        data={"energy_summary": energy_summary}
    )

    acquisition_cost = coordinator._acquisition_cost_for_run(
        import_prices=[0.43, 0.532, 0.43],
        current_soc=1.0,
        capacity_wh=24_200,
    )

    assert acquisition_cost == pytest.approx(0.43)


def test_unknown_charge_provenance_caps_proven_solar_at_current_inventory(
    opt_module,
):
    coordinator = _coordinator(opt_module, "flow_power")
    coordinator._grid_charge_tracking_known = False
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "energy_summary": {
                "charge_today_kwh": 100.0,
                "discharge_today_kwh": 0.0,
                "grid_import_today_kwh": 0.0,
            }
        }
    )

    acquisition_cost = coordinator._acquisition_cost_for_run(
        import_prices=[0.43, 0.532, 0.43],
        current_soc=0.5,
        capacity_wh=10_000,
    )

    assert acquisition_cost == pytest.approx(0.0)


def test_all_grid_charge_keeps_measured_acquisition_cost(opt_module):
    coordinator = _coordinator(opt_module, "flow_power")
    coordinator._grid_charge_tracking_known = True
    # All of the currently stored inventory is known to be grid-origin.
    coordinator._actual_charge_kwh_today = 30.08
    coordinator._actual_discharge_kwh_today = 0.0
    coordinator._actual_grid_charge_kwh_today = 30.08
    coordinator._actual_grid_charge_cost_today = 12.032

    acquisition_cost = coordinator._acquisition_cost_for_run(
        import_prices=[0.43, 0.532, 0.41],
        current_soc=0.94,
        capacity_wh=32_000,
    )

    assert acquisition_cost == pytest.approx(0.40)


@pytest.mark.parametrize(
    ("grid_fields", "expected_known"),
    [({}, False), ({"grid_charge_kwh": 0.0, "grid_charge_cost": 0.0}, True)],
)
def test_cost_restore_distinguishes_legacy_missing_grid_charge_provenance(
    opt_module,
    grid_fields,
    expected_known,
):
    coordinator = _coordinator(opt_module, "flow_power")
    payload = {
        "date": "2026-05-03",
        "charge_kwh": 31.0,
        "discharge_kwh": 0.5,
        **grid_fields,
    }

    async def _load():
        return payload

    coordinator._cost_store = SimpleNamespace(async_load=_load)
    coordinator._ensure_custom_tariff_quota_ledger = lambda *args, **kwargs: None
    coordinator._covau_snapshot = lambda: None

    asyncio.run(coordinator._restore_cost_data())

    assert coordinator._grid_charge_tracking_known is expected_known


def test_zerocharge_month_state_restores_across_midnight_from_explicit_period(
    opt_module,
    monkeypatch,
):
    now = datetime(2026, 8, 3, 0, 15, tzinfo=timezone(timedelta(hours=10)))
    monkeypatch.setattr(opt_module.dt_util, "now", lambda: now)
    coordinator = _coordinator(opt_module, "globird", globird_plan="zerohero_jul_2026")
    payload = {
        "date": "2026-08-02",
        "zerohero": {
            "zerocharge_period_key": "2026-08",
            "zerocharge_import_kwh_month": 50.0,
            "zerocharge_credit_value_month": 2.5,
            "baseline_zerocharge_period_key": "2026-08",
            "baseline_zerocharge_import_kwh_month": 40.0,
            "baseline_zerocharge_credit_value_month": 2.0,
        },
    }

    async def _load():
        return payload

    coordinator._cost_store = SimpleNamespace(async_load=_load)
    coordinator._ensure_custom_tariff_quota_ledger = lambda *args, **kwargs: None
    coordinator._covau_snapshot = lambda: None

    asyncio.run(coordinator._restore_cost_data())

    assert coordinator._actual_zerocharge_period_key == "2026-08"
    assert coordinator._actual_zerocharge_import_kwh_month == pytest.approx(50.0)
    assert coordinator._actual_zerocharge_credit_value_month == pytest.approx(2.5)
    assert coordinator._baseline_zerocharge_period_key == "2026-08"
    assert coordinator._baseline_zerocharge_import_kwh_month == pytest.approx(40.0)
    assert coordinator._baseline_zerocharge_credit_value_month == pytest.approx(2.0)
    # Legacy/public aliases remain synchronized.
    assert coordinator._actual_zerocharge_import_kwh_today == pytest.approx(50.0)


def test_zerocharge_month_restore_tolerates_malformed_values(
    opt_module,
    monkeypatch,
):
    now = datetime(2026, 8, 3, 0, 15, tzinfo=timezone(timedelta(hours=10)))
    monkeypatch.setattr(opt_module.dt_util, "now", lambda: now)
    coordinator = _coordinator(opt_module, "globird", globird_plan="zerohero_jul_2026")
    payload = {
        "date": "2026-08-02",
        "zerohero": {
            "zerocharge_period_key": "2026-08",
            "zerocharge_import_kwh_month": "invalid",
            "zerocharge_import_kwh": 50.0,
            "zerocharge_credit_value_month": None,
            "zerocharge_credit_value": 2.5,
            "baseline_zerocharge_period_key": "2026-08",
            "baseline_zerocharge_import_kwh_month": float("nan"),
            "baseline_zerocharge_credit_value_month": -1.0,
        },
    }

    async def _load():
        return payload

    coordinator._cost_store = SimpleNamespace(async_load=_load)
    coordinator._ensure_custom_tariff_quota_ledger = lambda *args, **kwargs: None
    coordinator._covau_snapshot = lambda: None

    asyncio.run(coordinator._restore_cost_data())

    assert coordinator._actual_zerocharge_import_kwh_month == pytest.approx(50.0)
    assert coordinator._actual_zerocharge_credit_value_month == pytest.approx(2.5)
    assert coordinator._baseline_zerocharge_import_kwh_month == 0.0
    assert coordinator._baseline_zerocharge_credit_value_month == 0.0


def test_zerocharge_month_restore_tolerates_malformed_section_shape(
    opt_module,
    monkeypatch,
):
    now = datetime(2026, 8, 3, 0, 15, tzinfo=timezone(timedelta(hours=10)))
    monkeypatch.setattr(opt_module.dt_util, "now", lambda: now)
    coordinator = _coordinator(opt_module, "globird", globird_plan="zerohero_jul_2026")

    async def _load():
        return {"date": "2026-08-02", "zerohero": "invalid"}

    coordinator._cost_store = SimpleNamespace(async_load=_load)
    coordinator._ensure_custom_tariff_quota_ledger = lambda *args, **kwargs: None
    coordinator._covau_snapshot = lambda: None

    asyncio.run(coordinator._restore_cost_data())

    assert coordinator._actual_zerocharge_import_kwh_month == 0.0
    assert coordinator._baseline_zerocharge_import_kwh_month == 0.0


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
        force_charge_result: bool | None = None,
        force_discharge_result: bool | None = None,
        restore_normal_result: bool | None = None,
        self_consumption_result: bool | None = None,
    ) -> None:
        self.hardware_mode = hardware_mode
        self.backup_reserve = backup_reserve
        self.self_consumption_calls = 0
        self.restore_normal_calls = 0
        self.restore_normal_force_flags = []
        self.restore_normal_result = restore_normal_result
        self.self_consumption_result = self_consumption_result
        self.backup_reserve_calls = []
        self.force_charge_calls = []
        self.force_charge_result = force_charge_result
        self.force_discharge_calls = []
        self.force_discharge_result = force_discharge_result

    async def get_tesla_operation_mode(self):
        return self.hardware_mode

    async def get_backup_reserve(self):
        return self.backup_reserve

    async def set_self_consumption_mode(self):
        self.self_consumption_calls += 1
        return self.self_consumption_result

    async def restore_normal(self, *, force_restore=False):
        self.restore_normal_calls += 1
        self.restore_normal_force_flags.append(force_restore)
        return self.restore_normal_result

    async def set_backup_reserve(self, percent):
        self.backup_reserve_calls.append(percent)

    async def force_charge(self, duration_minutes=60, power_w=5000, _extend_hardware=False):
        self.force_charge_calls.append((duration_minutes, power_w, _extend_hardware))
        return self.force_charge_result

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
        return self.force_discharge_result


class _FakeEnergyCoordinator:
    def __init__(self) -> None:
        self.restore_work_mode_from_idle_calls = 0
        self.no_discharge_calls = 0
        self.restore_no_discharge_calls = 0
        self.restore_no_discharge_result = True
        self.discharge_blocked_after_restore = False

    async def restore_work_mode_from_idle(self):
        self.restore_work_mode_from_idle_calls += 1

    async def set_no_discharge_mode(self):
        self.no_discharge_calls += 1
        return True

    async def restore_no_discharge_mode(self):
        self.restore_no_discharge_calls += 1
        return self.restore_no_discharge_result

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
    coordinator._last_executed_planned_action = None
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


class _FakeSolarExportHold:
    def __init__(self, apply_result=True, clear_result=True):
        self.apply_result = apply_result
        self.clear_result = clear_result
        self.active = False
        self.applied = []
        self.cleared = []
        self.status = {}

    async def apply(self, owner, generation):
        self.applied.append((owner, generation))
        self.active = self.apply_result
        return self.apply_result

    async def clear(self, reason):
        self.cleared.append(reason)
        if self.clear_result:
            self.active = False
        return self.clear_result


def test_solar_export_action_records_effective_only_after_hold_confirmation(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.50)
    coordinator.battery_system = "sigenergy"
    hold = _FakeSolarExportHold()
    coordinator._solar_export_hold = hold
    action = SimpleNamespace(
        action="solar_export",
        power_w=4000,
        timestamp=datetime(2026, 7, 30, 7, 30, tzinfo=timezone.utc),
    )

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert hold.applied == [("entry-1", action.timestamp.isoformat())]
    assert coordinator._last_executed_planned_action == "solar_export"
    assert coordinator._last_executed_action == "solar_export"
    assert battery.force_charge_calls == []
    assert battery.force_discharge_calls == []


def test_solar_export_apply_failure_executes_normal_control_fallback(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.50)
    coordinator.battery_system = "sungrow"
    hold = _FakeSolarExportHold(apply_result=False)
    coordinator._solar_export_hold = hold
    action = SimpleNamespace(
        action="solar_export",
        power_w=4000,
        timestamp=datetime(2026, 7, 30, 7, 30, tzinfo=timezone.utc),
    )

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.self_consumption_calls == 1
    assert coordinator._last_executed_planned_action == "solar_export"
    assert coordinator._last_executed_action == "self_consumption"


def test_transition_clears_solar_export_hold_before_next_action(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.50)
    coordinator.battery_system = "sigenergy"
    hold = _FakeSolarExportHold()
    hold.active = True
    coordinator._solar_export_hold = hold
    action = SimpleNamespace(
        action="self_consumption",
        power_w=0,
        timestamp=datetime(2026, 7, 30, 7, 35, tzinfo=timezone.utc),
    )

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert hold.cleared == ["transition_to_self_consumption"]
    assert coordinator._last_executed_action == "self_consumption"


def test_tesla_self_consumption_restores_stale_grid_charge(opt_module):
    """A confirmed mode/reserve cannot hide continued force-charge flow."""
    battery = _FakeBattery(
        hardware_mode="self_consumption",
        backup_reserve=10,
        restore_normal_result=True,
    )
    coordinator = _execution_coordinator(opt_module, battery, soc=0.225)
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "battery_power": -3.34,
            "grid_power": 3.96,
            "battery_level": 22.5,
            "grid_services_active": False,
        }
    )
    coordinator._get_energy_data = lambda: coordinator.energy_coordinator.data
    action = SimpleNamespace(
        action="self_consumption",
        power_w=0,
        timestamp=datetime(2026, 8, 22, 7, 20, tzinfo=timezone.utc),
    )

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.restore_normal_calls == 1
    assert battery.restore_normal_force_flags == [True]
    assert battery.self_consumption_calls == 0
    assert battery.backup_reserve_calls == [20]
    assert coordinator._last_executed_action == "self_consumption"


def test_tesla_stale_grid_charge_rechecks_after_restore_cooldown(opt_module):
    """Continued physical charging triggers another restore after cooldown."""
    battery = _FakeBattery(
        hardware_mode="self_consumption",
        backup_reserve=20,
        restore_normal_result=True,
    )
    coordinator = _execution_coordinator(opt_module, battery, soc=0.235)
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "battery_power": -3.34,
            "grid_power": 3.96,
            "battery_level": 23.5,
            "grid_services_active": False,
        }
    )
    coordinator._get_energy_data = lambda: coordinator.energy_coordinator.data
    now = datetime(2026, 8, 22, 7, 20, tzinfo=timezone.utc)
    current_time = {"now": now}
    opt_module.dt_util.utcnow = lambda *args, **kwargs: current_time["now"]
    action = SimpleNamespace(action="self_consumption", power_w=0)

    asyncio.run(coordinator._execute_optimizer_action(action))
    asyncio.run(coordinator._execute_optimizer_action(action))
    assert battery.restore_normal_calls == 1

    current_time["now"] += opt_module.TESLA_STALE_GRID_CHARGE_RESTORE_COOLDOWN
    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.restore_normal_calls == 2


def test_tesla_stale_grid_charge_retries_failed_restore(opt_module):
    """A failed restore remains immediately retryable on the next cycle."""
    battery = _FakeBattery(
        hardware_mode="self_consumption",
        backup_reserve=20,
        restore_normal_result=False,
    )
    coordinator = _execution_coordinator(opt_module, battery, soc=0.235)
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "battery_power": -3.34,
            "grid_power": 3.96,
            "battery_level": 23.5,
            "grid_services_active": False,
        }
    )
    coordinator._get_energy_data = lambda: coordinator.energy_coordinator.data
    action = SimpleNamespace(action="self_consumption", power_w=0)

    asyncio.run(coordinator._execute_optimizer_action(action))
    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.restore_normal_calls == 2
    assert not hasattr(coordinator, "_last_tesla_stale_grid_charge_restore_at")


@pytest.mark.parametrize(
    ("battery_kw", "grid_kw", "soc_pct", "grid_services_active"),
    [
        (-3.34, 0.20, 23.5, False),  # Predominantly solar-funded charge.
        (-3.34, 3.96, 21.5, False),  # Legitimate reserve recovery.
        (-3.34, 3.96, 23.5, True),  # Native grid-service dispatch owns it.
    ],
)
def test_tesla_self_consumption_keeps_expected_charge_sources(
    opt_module,
    battery_kw,
    grid_kw,
    soc_pct,
    grid_services_active,
):
    battery = _FakeBattery(
        hardware_mode="self_consumption",
        backup_reserve=20,
    )
    coordinator = _execution_coordinator(opt_module, battery, soc=soc_pct / 100)
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "battery_power": battery_kw,
            "grid_power": grid_kw,
            "battery_level": soc_pct,
            "grid_services_active": grid_services_active,
        }
    )
    coordinator._get_energy_data = lambda: coordinator.energy_coordinator.data

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )

    assert battery.restore_normal_calls == 0
    assert battery.self_consumption_calls == 0
    assert battery.backup_reserve_calls == []


def test_failed_hold_cleanup_suppresses_next_force_action_and_uses_normal_control(
    opt_module,
):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.50)
    coordinator.battery_system = "neovolt"
    hold = _FakeSolarExportHold(clear_result=False)
    hold.active = True
    coordinator._solar_export_hold = hold
    action = SimpleNamespace(
        action="charge",
        power_w=3000,
        timestamp=datetime(2026, 7, 30, 7, 35, tzinfo=timezone.utc),
    )

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert hold.cleared == ["transition_to_charge"]
    assert battery.force_charge_calls == []
    assert battery.self_consumption_calls == 1
    assert coordinator._last_executed_action == "self_consumption"


def test_optimizer_waits_for_startup_hardware_restore_task(opt_module):
    async def run_wait():
        coordinator = object.__new__(opt_module.OptimizationCoordinator)
        coordinator._enabled = True
        restore_started = asyncio.Event()
        allow_restore = asyncio.Event()

        async def restore_hardware():
            restore_started.set()
            await allow_restore.wait()

        restore_task = asyncio.create_task(restore_hardware())
        coordinator._deferred_restore_task = restore_task
        wait_task = asyncio.create_task(
            coordinator._wait_for_deferred_enable_restore()
        )
        await restore_started.wait()
        await asyncio.sleep(0)
        was_waiting = not wait_task.done()
        allow_restore.set()
        result = await wait_task
        return was_waiting, result

    was_waiting, result = asyncio.run(run_wait())
    assert was_waiting is True
    assert result is True


def test_rejected_optimizer_export_does_not_record_active_force_state(opt_module):
    battery = _FakeBattery(force_discharge_result=False)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    coordinator.battery_system = "sigenergy"
    action = SimpleNamespace(
        action="export",
        power_w=5000,
        timestamp=datetime(2026, 7, 30, 7, 30, tzinfo=timezone.utc),
    )
    coordinator._current_schedule = SimpleNamespace(actions=[action])

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.force_discharge_calls == [(5, 5000, False, None)]
    assert coordinator._optimizer_force_state["active"] is False
    assert coordinator._last_executed_action == "self_consumption"


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


def test_api_no_idle_never_publishes_or_executes_residual_idle(opt_module):
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
    assert data["effective_current_action"] == "self_consumption"
    assert data["current_action"] == "self_consumption"
    assert data["current_power_w"] == -300
    assert data["next_action"] == "charge"
    assert data["next_action_power_w"] == 5000
    assert data["next_actions"][0]["action"] == "self_consumption"
    assert data["next_actions"][0]["planned_action"] == "idle"

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

    # Startup, expired-schedule, and failed-solve states must not fall back to
    # displaying IDLE while No Idle is enabled.
    coordinator._current_schedule = None
    coordinator._get_current_action = lambda: None

    data = coordinator.get_api_data()

    assert data["planned_current_action"] == "self_consumption"
    assert data["effective_current_action"] == "self_consumption"
    assert data["current_action"] == "self_consumption"
    assert data["next_action"] == "self_consumption"


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


@pytest.mark.parametrize(
    ("last_executed_action", "last_executed_planned_action"),
    [
        ("charge", "charge"),
        (None, None),
    ],
)
def test_api_external_self_consumption_overrides_planned_charge_status(
    opt_module,
    last_executed_action,
    last_executed_planned_action,
):
    """A persisted user override must win before and after reload markers."""
    coordinator = _coordinator(opt_module, "amber")
    now = datetime(2026, 8, 14, 8, 55, tzinfo=timezone.utc)
    actions = [
        _api_action(now, "charge", 5000, 0.25),
        _api_action(now + timedelta(minutes=5), "charge", 5000, 0.29),
        _api_action(now + timedelta(minutes=10), "self_consumption", 0, 0.33),
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
    coordinator._last_executed_action = last_executed_action
    coordinator._last_executed_planned_action = last_executed_planned_action
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
    coordinator._get_actual_battery_power_w = lambda: -838
    coordinator._get_current_action = lambda: actions[0]
    coordinator._get_active_force_state = lambda: {
        "active": True,
        "type": "self_consumption",
        "source": "user",
        "scope": "external",
    }
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
    assert data["planned_current_power_w"] == 5000
    assert data["effective_current_action"] == "self_consumption"
    assert data["current_action"] == "self_consumption"
    assert data["current_power_w"] == -838
    assert data["actual_battery_power_w"] == -838


@pytest.mark.parametrize("planned_action", ["charge", "export"])
@pytest.mark.parametrize(
    (
        "last_executed_action",
        "last_executed_planned_action",
        "in_demand_window",
        "expected_action",
    ),
    [
        (None, None, False, "idle"),
        (None, None, True, "self_consumption"),
        ("self_consumption", "self_consumption", False, "self_consumption"),
    ],
)
def test_api_unacknowledged_force_plan_is_not_effective_hardware_status(
    opt_module,
    planned_action,
    last_executed_action,
    last_executed_planned_action,
    in_demand_window,
    expected_action,
):
    coordinator = _coordinator(opt_module, "flow_power")
    now = datetime(2026, 8, 14, 17, 30, tzinfo=timezone.utc)
    actions = [
        _api_action(now, planned_action, 5000, 0.80),
        _api_action(now + timedelta(minutes=5), "self_consumption", 0, 0.78),
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
    coordinator._last_executed_action = last_executed_action
    coordinator._last_executed_planned_action = last_executed_planned_action
    coordinator._optimizer_force_state = {"active": False, "type": None}
    coordinator._force_state_getter = None
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
    coordinator._get_actual_battery_power_w = lambda: 630
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
    coordinator._is_in_demand_window_at = lambda timestamp: in_demand_window

    data = coordinator.get_api_data()

    assert data["planned_current_action"] == planned_action
    assert data["planned_current_power_w"] == 5000
    assert data["effective_current_action"] == expected_action
    assert data["current_action"] == expected_action
    assert data["current_power_w"] == 630


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


def test_zero_forecast_from_confirmed_provider_does_not_warn(opt_module):
    coordinator = _coordinator(opt_module, "octopus")
    coordinator._solar_forecaster = SimpleNamespace(last_forecast_source="solcast")

    coordinator._record_solar_forecast_availability([0.0, 0.0])

    assert coordinator._has_solar_forecast is True
    assert coordinator._get_warnings() == []


def test_zero_forecast_without_provider_keeps_unavailable_warning(opt_module):
    coordinator = _coordinator(opt_module, "octopus")
    coordinator._solar_forecaster = SimpleNamespace(last_forecast_source=None)

    coordinator._record_solar_forecast_availability([0.0, 0.0])

    assert coordinator._has_solar_forecast is False
    warnings = coordinator._get_warnings()
    assert [warning["type"] for warning in warnings] == ["no_solar_forecast"]


def test_coordinator_refresh_executes_cached_charge_at_action_boundary(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.25)
    coordinator._enabled = True
    coordinator._optimization_lock = SimpleNamespace(locked=lambda: False)
    coordinator.get_api_data = lambda: {"ok": True}
    coordinator._track_actual_cost = lambda: None
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


def test_failed_force_charge_keeps_previous_action_marker_for_retry(opt_module):
    battery = _FakeBattery(force_charge_result=False)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.25)
    coordinator._enabled = True
    coordinator._optimization_lock = SimpleNamespace(locked=lambda: False)
    coordinator.get_api_data = lambda: {"ok": True}
    coordinator._track_actual_cost = lambda: None
    coordinator._last_executed_action = "self_consumption"
    boundary = datetime(2026, 5, 3, 11, 0, tzinfo=timezone.utc)
    coordinator._current_schedule = SimpleNamespace(
        actions=[
            SimpleNamespace(
                action="self_consumption",
                power_w=0,
                timestamp=boundary - timedelta(minutes=5),
            ),
            SimpleNamespace(action="charge", power_w=5000, timestamp=boundary),
            SimpleNamespace(
                action="self_consumption",
                power_w=0,
                timestamp=boundary + timedelta(minutes=5),
            ),
        ]
    )
    original_now = opt_module.dt_util.now
    opt_module.dt_util.now = lambda *args, **kwargs: boundary

    try:
        result = asyncio.run(coordinator._async_update_data())
    finally:
        opt_module.dt_util.now = original_now

    assert result == {"ok": True}
    assert battery.force_charge_calls == [(5, 5000, False)]
    assert coordinator._last_executed_action == "self_consumption"


def test_cached_force_is_refreshed_once_when_same_action_crosses_boundary(opt_module):
    """A retained force action must not let its previous timer expire mid-slot."""
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.97)
    coordinator._enabled = True
    coordinator._optimization_lock = asyncio.Lock()
    coordinator._execute_lock = asyncio.Lock()
    coordinator._last_executed_action = "charge"
    boundary = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)
    actions = [
        SimpleNamespace(action="charge", power_w=15000, timestamp=boundary),
        SimpleNamespace(
            action="charge",
            power_w=15000,
            timestamp=boundary + timedelta(minutes=5),
        ),
        SimpleNamespace(
            action="self_consumption",
            power_w=0,
            timestamp=boundary + timedelta(minutes=10),
        ),
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)
    coordinator._boundary_execution = {
        "slot_start": boundary - timedelta(minutes=5),
        "slot_end": boundary,
        "action": "charge",
        "was_forced": True,
    }
    coordinator._optimizer_force_state = {
        "active": True,
        "type": "charge",
        "expires_at": boundary + timedelta(minutes=3),
        "hardware_expires_at": boundary + timedelta(minutes=3),
        "power_w": 15000,
        "started_at": boundary - timedelta(minutes=2),
        "source": "optimizer",
        "scope": "optimizer",
    }
    original_now = opt_module.dt_util.now
    original_utcnow = opt_module.dt_util.utcnow
    opt_module.dt_util.now = lambda *args, **kwargs: boundary
    opt_module.dt_util.utcnow = lambda *args, **kwargs: boundary

    try:
        asyncio.run(coordinator._execute_cached_current_action_if_changed())
        asyncio.run(coordinator._execute_cached_current_action_if_changed())
    finally:
        opt_module.dt_util.now = original_now
        opt_module.dt_util.utcnow = original_utcnow

    assert battery.force_charge_calls == [(10, 15000, True)]
    assert coordinator._boundary_execution == {
        "slot_start": boundary,
        "slot_end": boundary + timedelta(minutes=5),
        "action": "charge",
        "was_forced": True,
    }


def test_cached_no_idle_releases_stale_goodwe_charge_by_time_hold(opt_module):
    """A stale planned IDLE must not deduplicate the effective self-use action."""

    class SuccessfulGoodWeBattery(_FakeBattery):
        async def set_self_consumption_mode(self):
            self.self_consumption_calls += 1
            return True

    battery = SuccessfulGoodWeBattery(backup_reserve=15)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.92)
    coordinator.battery_system = "goodwe"
    coordinator.energy_coordinator = SimpleNamespace(data={})
    coordinator._config.disable_idle_enabled = True
    coordinator._enabled = True
    coordinator._optimization_lock = asyncio.Lock()
    coordinator._execute_lock = asyncio.Lock()
    coordinator._last_executed_planned_action = "idle"
    coordinator._last_executed_action = "idle"
    coordinator._idle_hold_reserve = None
    boundary = datetime(2026, 7, 25, 15, 45, tzinfo=timezone.utc)
    action = SimpleNamespace(action="idle", power_w=0, timestamp=boundary)
    coordinator._get_current_action = lambda: action

    asyncio.run(coordinator._execute_cached_current_action_if_changed())

    assert battery.self_consumption_calls == 1
    assert battery.backup_reserve_calls == []
    assert coordinator._last_executed_planned_action == "idle"
    assert coordinator._last_executed_action == "self_consumption"
    assert coordinator._boundary_execution == {
        "slot_start": boundary,
        "slot_end": boundary + timedelta(minutes=5),
        "action": "self_consumption",
        "was_forced": False,
    }


def test_cached_no_idle_retries_failed_goodwe_charge_by_time_hold_release(opt_module):
    """A failed GoodWe restore must keep the stale IDLE marker retryable."""

    class FailingGoodWeBattery(_FakeBattery):
        async def set_self_consumption_mode(self):
            self.self_consumption_calls += 1
            return False

    battery = FailingGoodWeBattery(backup_reserve=15)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.92)
    coordinator.battery_system = "goodwe"
    coordinator.energy_coordinator = SimpleNamespace(data={})
    coordinator._config.disable_idle_enabled = True
    coordinator._enabled = True
    coordinator._optimization_lock = asyncio.Lock()
    coordinator._execute_lock = asyncio.Lock()
    coordinator._last_executed_planned_action = "idle"
    coordinator._last_executed_action = "idle"
    coordinator._idle_hold_reserve = None
    action = SimpleNamespace(
        action="idle",
        power_w=0,
        timestamp=datetime(2026, 7, 25, 15, 45, tzinfo=timezone.utc),
    )
    coordinator._get_current_action = lambda: action

    asyncio.run(coordinator._execute_cached_current_action_if_changed())

    assert battery.self_consumption_calls == 1
    assert battery.backup_reserve_calls == []
    assert coordinator._last_executed_planned_action == "idle"
    assert coordinator._last_executed_action == "idle"
    assert getattr(coordinator, "_boundary_execution", None) is None


def test_periodic_slow_solve_defers_new_force_until_next_boundary(opt_module):
    """A late periodic result must not reverse the cached boundary action."""
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.97)
    coordinator.battery_system = "foxess"
    coordinator._enabled = True
    coordinator._optimization_lock = asyncio.Lock()
    coordinator._execute_lock = asyncio.Lock()
    coordinator._last_executed_action = "charge"
    boundary = datetime(2026, 7, 15, 13, 10, tzinfo=timezone.utc)
    now = [boundary]
    original_now = opt_module.dt_util.now
    opt_module.dt_util.now = lambda *args, **kwargs: now[0]

    cached_actions = [
        SimpleNamespace(
            action="self_consumption",
            power_w=0,
            timestamp=boundary,
        ),
        SimpleNamespace(
            action="self_consumption",
            power_w=0,
            timestamp=boundary + timedelta(minutes=5),
        ),
    ]
    coordinator._current_schedule = SimpleNamespace(actions=cached_actions)

    try:
        asyncio.run(coordinator._execute_cached_current_action_if_changed())
        assert coordinator._boundary_execution == {
            "slot_start": boundary,
            "slot_end": boundary + timedelta(minutes=5),
            "action": "self_consumption",
            "was_forced": False,
        }

        fresh_actions = [
            SimpleNamespace(action="charge", power_w=20000, timestamp=boundary),
            SimpleNamespace(
                action="charge",
                power_w=20000,
                timestamp=boundary + timedelta(minutes=5),
            ),
            SimpleNamespace(
                action="self_consumption",
                power_w=0,
                timestamp=boundary + timedelta(minutes=10),
            ),
        ]
        coordinator._current_schedule = SimpleNamespace(actions=fresh_actions)
        now[0] = boundary + timedelta(minutes=3, seconds=16)

        asyncio.run(
            coordinator._execute_optimizer_action(
                fresh_actions[0],
                execution_trigger="poll",
            )
        )

        assert battery.force_charge_calls == []
        assert coordinator._last_executed_action == "self_consumption"

        now[0] = boundary + timedelta(minutes=5)
        asyncio.run(coordinator._execute_cached_current_action_if_changed())

        assert battery.force_charge_calls == [(5, 20000, False)]
        assert coordinator._last_executed_action == "charge"
    finally:
        opt_module.dt_util.now = original_now


def test_periodic_boundary_solve_can_start_fresh_export_immediately(opt_module):
    """A fast boundary solve must not lose the new slot to the cached plan."""
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.97)
    coordinator.battery_system = "foxess"
    coordinator._enabled = True
    coordinator._optimization_lock = asyncio.Lock()
    coordinator._execute_lock = asyncio.Lock()
    coordinator._last_executed_action = "charge"
    boundary = datetime(2026, 7, 21, 18, 5, tzinfo=timezone.utc)
    now = [boundary]
    original_now = opt_module.dt_util.now
    opt_module.dt_util.now = lambda *args, **kwargs: now[0]

    cached_actions = [
        SimpleNamespace(
            action="self_consumption",
            power_w=0,
            timestamp=boundary,
        ),
        SimpleNamespace(
            action="self_consumption",
            power_w=0,
            timestamp=boundary + timedelta(minutes=5),
        ),
    ]
    coordinator._current_schedule = SimpleNamespace(actions=cached_actions)

    try:
        asyncio.run(coordinator._execute_cached_current_action_if_changed())

        fresh_actions = [
            SimpleNamespace(action="export", power_w=8000, timestamp=boundary),
            SimpleNamespace(
                action="self_consumption",
                power_w=0,
                timestamp=boundary + timedelta(minutes=5),
            ),
        ]
        coordinator._current_schedule = SimpleNamespace(actions=fresh_actions)
        now[0] = boundary + timedelta(seconds=2)

        asyncio.run(
            coordinator._execute_optimizer_action(
                fresh_actions[0],
                execution_trigger="poll",
            )
        )
    finally:
        opt_module.dt_util.now = original_now

    assert battery.force_discharge_calls == [(5, 5000.0, False, None)]
    assert coordinator._last_executed_action == "export"


def test_price_solve_can_override_non_force_boundary_mid_slot(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.50)
    coordinator.battery_system = "foxess"
    boundary = datetime(2026, 7, 15, 13, 10, tzinfo=timezone.utc)
    now = boundary + timedelta(minutes=3)
    original_now = opt_module.dt_util.now
    opt_module.dt_util.now = lambda *args, **kwargs: now
    action = SimpleNamespace(action="charge", power_w=5000, timestamp=boundary)
    coordinator._current_schedule = SimpleNamespace(
        actions=[
            action,
            SimpleNamespace(
                action="self_consumption",
                power_w=0,
                timestamp=boundary + timedelta(minutes=5),
            ),
        ]
    )
    coordinator._boundary_execution = {
        "slot_start": boundary,
        "slot_end": boundary + timedelta(minutes=5),
        "action": "self_consumption",
        "was_forced": False,
    }

    try:
        asyncio.run(
            coordinator._execute_optimizer_action(
                action,
                execution_trigger="price",
            )
        )
    finally:
        opt_module.dt_util.now = original_now

    assert battery.force_charge_calls == [(2, 5000, False)]


def test_force_duration_uses_wall_clock_time_remaining_in_action_block(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.50)
    coordinator.battery_system = "foxess"
    start = datetime(2026, 7, 15, 13, 10, tzinfo=timezone.utc)
    actions = [
        SimpleNamespace(action="charge", power_w=5000, timestamp=start),
        SimpleNamespace(
            action="charge",
            power_w=5000,
            timestamp=start + timedelta(minutes=5),
        ),
        SimpleNamespace(
            action="self_consumption",
            power_w=0,
            timestamp=start + timedelta(minutes=10),
        ),
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)
    original_now = opt_module.dt_util.now
    opt_module.dt_util.now = lambda *args, **kwargs: start + timedelta(
        minutes=3,
        seconds=16,
    )

    try:
        duration = coordinator._force_duration_for_action_window(
            actions[0],
            {"charge"},
            allow_boundary_overrun=False,
        )
    finally:
        opt_module.dt_util.now = original_now

    assert duration == 7


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


def test_no_idle_uses_self_consumption_after_smart_schedule_preserve_clears(
    opt_module,
):
    battery = _FakeBattery(hardware_mode="autonomous")
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    energy = _FakeEnergyCoordinator()
    coordinator.energy_coordinator = energy
    coordinator._config.disable_idle_enabled = True
    coordinator.hass.data = {
        "power_sync": {
            coordinator.entry_id: {
                "scheduled_ev_preserve_state": {
                    "active": False,
                    "source": "smart_schedule",
                    "reason": "no active Smart Schedule charging",
                }
            }
        }
    }

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="idle", power_w=0)
        )
    )

    assert battery.self_consumption_calls == 1
    assert energy.no_discharge_calls == 0
    assert coordinator._last_executed_action == "self_consumption"


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


def test_sigenergy_spread_import_keeps_scheduled_free_charge_target(opt_module):
    """Spread Import must not be widened back to free-window headroom."""
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.25)
    coordinator.battery_system = "sigenergy"
    coordinator._config.spread_import_enabled = True
    coordinator._config.max_grid_import_w = 10000
    coordinator._config.max_charge_w = 10000
    coordinator._last_import_prices = [0.0]
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "solar_power": 0.0,
            "load_power": 0.0,
            "grid_power": 0.0,
            "battery_power": 0.0,
        }
    )

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="charge", power_w=5327)
        )
    )

    assert battery.force_charge_calls == [(10, 5327, False)]
    assert coordinator._optimizer_force_state["power_w"] == 5327


def test_sigenergy_spread_import_clamps_to_lower_live_headroom(opt_module):
    """Spread Import still fails safely when live site headroom falls."""
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.25)
    coordinator.battery_system = "sigenergy"
    coordinator._config.spread_import_enabled = True
    coordinator._config.max_grid_import_w = 10000
    coordinator._config.max_charge_w = 10000
    coordinator._last_import_prices = [0.0]
    coordinator._live_site_import_charge_limit_w = lambda: 4000.0

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="charge", power_w=5327)
        )
    )

    assert battery.force_charge_calls == [(10, 4000, False)]
    assert coordinator._optimizer_force_state["power_w"] == 4000


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


def test_tesla_targetless_partial_export_stops_before_crossing_reserve(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.6643)
    coordinator.battery_system = "tesla"
    coordinator._config.backup_reserve = 0.66
    coordinator._config.battery_capacity_wh = 27000
    coordinator._config.max_discharge_w = 10000
    coordinator._config.interval_minutes = 5
    coordinator._optimizer = SimpleNamespace(efficiency=0.92)
    coordinator._last_executed_action = "export"
    action = SimpleNamespace(
        action="export",
        power_w=1281.7,
        soc=0.66,
        timestamp=datetime(2026, 5, 3, 18, 30, tzinfo=timezone.utc),
    )
    coordinator._current_schedule = SimpleNamespace(actions=[action])

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.force_discharge_calls == []
    assert battery.self_consumption_calls == 1
    assert coordinator._last_executed_action == "self_consumption"


def test_tesla_targetless_full_interval_may_finish_at_reserve(opt_module):
    battery = _FakeBattery()
    interval_drop = 10000 * (5 / 60) / 0.92 / 27000
    coordinator = _execution_coordinator(
        opt_module,
        battery,
        soc=0.66 + interval_drop,
    )
    coordinator.battery_system = "tesla"
    coordinator._config.backup_reserve = 0.66
    coordinator._config.battery_capacity_wh = 27000
    coordinator._config.max_discharge_w = 10000
    coordinator._config.interval_minutes = 5
    coordinator._optimizer = SimpleNamespace(efficiency=0.92)
    action = SimpleNamespace(
        action="export",
        power_w=10000,
        soc=0.66,
        timestamp=datetime(2026, 5, 3, 18, 30, tzinfo=timezone.utc),
    )
    coordinator._current_schedule = SimpleNamespace(actions=[action])

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.force_discharge_calls == [(5, 10000, False, None)]
    assert coordinator._last_executed_action == "export"


def test_tesla_targetless_export_window_is_shortened_to_safe_duration(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.832)
    coordinator.battery_system = "tesla"
    coordinator._config.backup_reserve = 0.66
    coordinator._config.battery_capacity_wh = 27000
    coordinator._config.max_discharge_w = 10000
    coordinator._config.interval_minutes = 5
    coordinator._optimizer = SimpleNamespace(efficiency=0.92)
    start = datetime(2026, 5, 3, 18, 30, tzinfo=timezone.utc)
    actions = [
        SimpleNamespace(
            action="export",
            power_w=10000 if idx < 5 else 1281.7,
            soc=0.832 - min(idx + 1, 5) * (10000 * (5 / 60) / 0.92 / 27000),
            timestamp=start + idx * timedelta(minutes=5),
        )
        for idx in range(6)
    ]
    actions[-1].soc = 0.66
    coordinator._current_schedule = SimpleNamespace(actions=actions)

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))

    assert battery.force_discharge_calls
    assert battery.force_discharge_calls[0][0] == 25
    assert battery.force_discharge_calls[0][1] == 10000
    assert coordinator._last_executed_action == "export"


def test_active_tesla_partial_export_restores_before_crossing_reserve(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.6643)
    coordinator.battery_system = "tesla"
    coordinator._config.backup_reserve = 0.66
    coordinator._config.battery_capacity_wh = 27000
    coordinator._config.max_discharge_w = 10000
    coordinator._config.interval_minutes = 5
    coordinator._optimizer = SimpleNamespace(efficiency=0.92)
    coordinator._last_executed_action = "export"
    force_state_clears = []
    coordinator._force_state_getter = lambda: {
        "active": True,
        "type": "discharge",
        "source": "optimizer",
    }
    coordinator._force_state_clearer = lambda: force_state_clears.append(True)
    action = SimpleNamespace(
        action="export",
        power_w=1281.7,
        soc=0.66,
        timestamp=datetime(2026, 5, 3, 18, 30, tzinfo=timezone.utc),
    )
    coordinator._current_schedule = SimpleNamespace(actions=[action])

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.force_discharge_calls == []
    assert battery.restore_normal_calls == 1
    assert force_state_clears == [True]
    assert coordinator._last_executed_action == "self_consumption"


def test_active_tesla_targetless_extension_uses_safe_window(opt_module):
    now = datetime(2026, 5, 3, 18, 30, tzinfo=timezone.utc)
    opt_module.dt_util.now = lambda *args, **kwargs: now
    opt_module.dt_util.utcnow = lambda *args, **kwargs: now
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.832)
    coordinator.battery_system = "tesla"
    coordinator._config.backup_reserve = 0.66
    coordinator._config.battery_capacity_wh = 27000
    coordinator._config.max_discharge_w = 10000
    coordinator._config.interval_minutes = 5
    coordinator._optimizer = SimpleNamespace(efficiency=0.92)
    coordinator._last_executed_action = "export"
    actions = [
        SimpleNamespace(
            action="export",
            power_w=10000 if idx < 5 else 1281.7,
            soc=0.832 - min(idx + 1, 5) * (10000 * (5 / 60) / 0.92 / 27000),
            timestamp=now + idx * timedelta(minutes=5),
        )
        for idx in range(6)
    ]
    actions[-1].soc = 0.66
    coordinator._current_schedule = SimpleNamespace(actions=actions)
    force_state = {
        "active": True,
        "expires_at": now + timedelta(minutes=30),
        "hardware_expires_at": now + timedelta(minutes=30),
        "source": "optimizer",
        "cancel_expiry_timer": lambda: None,
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

    assert battery.force_discharge_calls
    assert battery.force_discharge_calls[0][:3] == (25, 10000, True)
    assert force_state["expires_at"] == now + timedelta(minutes=25)
    assert coordinator._last_executed_action == "export"


@pytest.mark.parametrize(
    ("capacity_wh", "efficiency"),
    [
        (0, 0.92),
        (27000, "invalid"),
    ],
)
def test_tesla_targetless_export_fails_closed_without_safety_inputs(
    opt_module,
    capacity_wh,
    efficiency,
):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    coordinator.battery_system = "tesla"
    coordinator._config.backup_reserve = 0.20
    coordinator._config.battery_capacity_wh = capacity_wh
    coordinator._config.max_discharge_w = 10000
    coordinator._config.interval_minutes = 5
    coordinator._optimizer = SimpleNamespace(efficiency=efficiency)
    coordinator._last_executed_action = "export"
    action = SimpleNamespace(
        action="export",
        power_w=10000,
        soc=0.75,
        timestamp=datetime(2026, 5, 3, 18, 30, tzinfo=timezone.utc),
    )
    coordinator._current_schedule = SimpleNamespace(actions=[action])

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.force_discharge_calls == []
    assert battery.self_consumption_calls == 1
    assert coordinator._last_executed_action == "self_consumption"


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


def test_self_consumption_initial_tesla_reserve_uses_soc_clamp(
    opt_module,
):
    battery = _FakeBattery(hardware_mode="self_consumption", backup_reserve=20)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.19)

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )

    assert battery.backup_reserve_calls == [19]


@pytest.mark.parametrize("current_reserve", [20, 25])
def test_self_consumption_initial_tesla_reserve_clamp_avoids_grid_charging(
    opt_module,
    current_reserve,
):
    battery = _FakeBattery(
        hardware_mode="self_consumption",
        backup_reserve=current_reserve,
    )
    coordinator = _execution_coordinator(opt_module, battery, soc=0.11)
    coordinator._startup_backup_reserve = 25

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )

    assert battery.backup_reserve_calls == [11]


def test_self_consumption_raises_lower_tesla_reserve_toward_startup_floor(
    opt_module,
):
    battery = _FakeBattery(hardware_mode="self_consumption", backup_reserve=18)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.19)

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )

    assert battery.backup_reserve_calls == [19]


def test_self_consumption_does_not_ratchet_tesla_reserve_down_after_raise(
    opt_module,
):
    battery = _FakeBattery(hardware_mode="self_consumption", backup_reserve=18)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.19)
    soc_values = [0.19, 0.18]

    async def _battery_state():
        return soc_values.pop(0), 13500

    coordinator._get_battery_state = _battery_state

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )
    battery.backup_reserve = 19

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )

    assert battery.backup_reserve_calls == [19]
    assert coordinator._last_optimizer_self_consumption_reserve_target == 19


def test_self_consumption_transition_uses_initial_tesla_soc_clamp(
    opt_module,
):
    battery = _FakeBattery(hardware_mode="autonomous", backup_reserve=20)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.19)
    coordinator._last_executed_action = "idle"

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )

    assert battery.self_consumption_calls == 1
    assert battery.backup_reserve_calls == [19]


def test_self_consumption_mode_failure_clears_reserve_provenance(
    opt_module,
):
    battery = _FakeBattery(
        hardware_mode="autonomous",
        backup_reserve=18,
        self_consumption_result=False,
    )
    coordinator = _execution_coordinator(opt_module, battery, soc=0.19)

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )

    assert battery.self_consumption_calls == 1
    assert battery.backup_reserve_calls == [19]
    assert coordinator._last_executed_action == "self_consumption"
    assert coordinator._last_optimizer_self_consumption_reserve_target is None


def test_self_consumption_reserve_write_failure_does_not_advance_provenance(
    opt_module,
):
    class FailingReserveBattery(_FakeBattery):
        async def set_backup_reserve(self, percent):
            self.backup_reserve_calls.append(percent)
            return False

    battery = FailingReserveBattery(
        hardware_mode="self_consumption",
        backup_reserve=18,
    )
    coordinator = _execution_coordinator(opt_module, battery, soc=0.19)

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )

    assert battery.backup_reserve_calls == [19]
    assert (
        getattr(
            coordinator,
            "_last_optimizer_self_consumption_reserve_target",
            None,
        )
        is None
    )


def test_self_consumption_reserve_provenance_clears_on_disable_reset(opt_module):
    async def _run():
        battery = _FakeBattery(
            hardware_mode="self_consumption",
            backup_reserve=18,
        )
        coordinator = _execution_coordinator(opt_module, battery, soc=0.19)
        soc_values = [0.19, 0.18]

        async def _battery_state():
            return soc_values.pop(0), 13500

        coordinator._get_battery_state = _battery_state
        coordinator._enabled = True

        await coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
        assert battery.backup_reserve_calls == [19]
        assert coordinator._last_optimizer_self_consumption_reserve_target == 19

        coordinator._polling_task = None
        coordinator._initial_opt_task = None
        coordinator._deferred_restore_task = None
        coordinator._settings_reoptimize_task = None
        coordinator._price_reoptimize_task = None
        coordinator._price_listener_unsub = None
        coordinator._octopus_gate_listener_unsub = None
        coordinator._ev_coordinator = None
        coordinator._cost_store = SimpleNamespace(
            async_save=lambda *_args: asyncio.sleep(0)
        )
        coordinator._cost_data_to_save = lambda: {}
        coordinator._executor.stop = lambda **_kwargs: asyncio.sleep(0)

        await coordinator.disable()

        assert coordinator._last_executed_action is None
        assert coordinator._last_optimizer_self_consumption_reserve_target is None

        coordinator._enabled = True
        battery.backup_reserve = 19
        await coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )

        assert battery.backup_reserve_calls == [19, 18]

    asyncio.run(_run())


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


def test_self_consumption_reapplies_pending_sungrow_export_restore(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.50)
    coordinator.battery_system = "sungrow"
    coordinator.energy_coordinator = _FakeEnergyCoordinator()
    coordinator.energy_coordinator.pending_optimizer_export_restore = True

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )
    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )

    assert battery.self_consumption_calls == 2
    assert coordinator._last_executed_action == "self_consumption"


def test_self_consumption_does_not_reapply_without_pending_sungrow_export_restore(
    opt_module,
):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.50)
    coordinator.battery_system = "sungrow"
    coordinator.energy_coordinator = _FakeEnergyCoordinator()
    coordinator.energy_coordinator.pending_optimizer_export_restore = False

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )

    assert battery.self_consumption_calls == 0
    assert coordinator._last_executed_action == "self_consumption"


def test_pending_sungrow_export_restore_retries_after_false_mode_apply(opt_module):
    class RejectingBattery(_FakeBattery):
        async def set_self_consumption_mode(self):
            self.self_consumption_calls += 1
            return False

    battery = RejectingBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.50)
    coordinator.battery_system = "sungrow"
    coordinator.energy_coordinator = _FakeEnergyCoordinator()
    coordinator.energy_coordinator.pending_optimizer_export_restore = True

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )
    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )

    assert battery.self_consumption_calls == 2
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


def test_sungrow_idle_uses_discharge_cap_without_backup_reserve_write(opt_module):
    class SungrowEnergyCoordinator(_FakeEnergyCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.backup_mode_calls = 0

        async def set_backup_mode(self):
            self.backup_mode_calls += 1
            return True

    battery = _FakeBattery(backup_reserve=10)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.984)
    coordinator.battery_system = "sungrow"
    coordinator._startup_backup_reserve = 10
    energy_coordinator = SungrowEnergyCoordinator()
    coordinator.energy_coordinator = energy_coordinator

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="idle", power_w=0)
        )
    )

    assert energy_coordinator.backup_mode_calls == 1
    assert battery.backup_reserve_calls == []
    assert coordinator._pre_idle_backup_reserve is None
    assert coordinator._idle_hold_reserve is None
    assert coordinator._last_executed_action == "idle"


def test_fronius_idle_uses_power_limits_without_backup_reserve_write(opt_module):
    class FroniusEnergyCoordinator(_FakeEnergyCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.backup_mode_calls = 0

        async def set_backup_mode(self):
            self.backup_mode_calls += 1
            return True

    battery = _FakeBattery(backup_reserve=4)
    coordinator = _execution_coordinator(opt_module, battery, soc=1.0)
    coordinator.battery_system = "fronius_reserva"
    coordinator._startup_backup_reserve = 4
    energy_coordinator = FroniusEnergyCoordinator()
    coordinator.energy_coordinator = energy_coordinator

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="idle", power_w=0)
        )
    )

    assert energy_coordinator.backup_mode_calls == 1
    assert battery.backup_reserve_calls == []
    assert coordinator._pre_idle_backup_reserve is None
    assert coordinator._idle_hold_reserve is None
    assert coordinator._last_executed_action == "idle"


@pytest.mark.parametrize("failure_mode", ["unavailable", "rejected"])
def test_fronius_idle_never_falls_back_to_backup_reserve_write(
    opt_module,
    failure_mode,
):
    class RejectingFroniusEnergyCoordinator(_FakeEnergyCoordinator):
        async def set_backup_mode(self):
            return False

    battery = _FakeBattery(backup_reserve=4)
    coordinator = _execution_coordinator(opt_module, battery, soc=1.0)
    coordinator.battery_system = "fronius_reserva"
    coordinator.energy_coordinator = (
        _FakeEnergyCoordinator()
        if failure_mode == "unavailable"
        else RejectingFroniusEnergyCoordinator()
    )

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="idle", power_w=0)
        )
    )

    assert battery.backup_reserve_calls == []
    assert coordinator._last_executed_action == "self_consumption"


def test_fronius_exit_idle_restores_auto_without_reserve_write(opt_module):
    battery = _FakeBattery(backup_reserve=4)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.75)
    coordinator.battery_system = "fronius_reserva"
    coordinator.energy_coordinator = _FakeEnergyCoordinator()
    coordinator._last_executed_action = "idle"

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )

    assert coordinator.energy_coordinator.restore_work_mode_from_idle_calls == 1
    assert battery.backup_reserve_calls == []
    assert coordinator._pre_idle_backup_reserve is None
    assert coordinator._idle_hold_reserve is None
    assert coordinator._last_executed_action == "self_consumption"


@pytest.mark.parametrize("result", [False, True])
def test_saj_idle_propagates_result_without_backup_reserve_write(
    opt_module,
    result,
):
    class SajEnergyCoordinator(_FakeEnergyCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.backup_mode_calls = 0

        async def set_backup_mode(self):
            self.backup_mode_calls += 1
            return result

    battery = _FakeBattery(backup_reserve=20)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.216)
    coordinator.battery_system = "saj_h2"
    coordinator._config.backup_reserve = 0.56
    energy_coordinator = SajEnergyCoordinator()
    coordinator.energy_coordinator = energy_coordinator

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="idle", power_w=0)
        )
    )

    assert energy_coordinator.backup_mode_calls == 1
    assert battery.backup_reserve_calls == []
    assert coordinator._pre_idle_backup_reserve is None
    assert coordinator._idle_hold_reserve is None
    assert coordinator._last_executed_action == (
        "idle" if result else "self_consumption"
    )


def test_fronius_rejected_auto_restore_keeps_idle_for_retry(opt_module):
    class RejectingFroniusEnergyCoordinator(_FakeEnergyCoordinator):
        async def restore_work_mode_from_idle(self):
            self.restore_work_mode_from_idle_calls += 1
            return False

    battery = _FakeBattery(backup_reserve=4)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.75)
    coordinator.battery_system = "fronius_reserva"
    coordinator.energy_coordinator = RejectingFroniusEnergyCoordinator()
    coordinator._last_executed_action = "idle"

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )

    assert coordinator.energy_coordinator.restore_work_mode_from_idle_calls == 1
    assert battery.self_consumption_calls == 0
    assert battery.backup_reserve_calls == []
    assert coordinator._last_executed_action == "idle"


@pytest.mark.parametrize("failure_mode", ["unavailable", "rejected"])
def test_sungrow_idle_never_falls_back_to_backup_reserve_write(
    opt_module,
    failure_mode,
):
    class RejectingSungrowEnergyCoordinator(_FakeEnergyCoordinator):
        async def set_backup_mode(self):
            return False

    battery = _FakeBattery(backup_reserve=10)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.984)
    coordinator.battery_system = "sungrow"
    coordinator.energy_coordinator = (
        _FakeEnergyCoordinator()
        if failure_mode == "unavailable"
        else RejectingSungrowEnergyCoordinator()
    )

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="idle", power_w=0)
        )
    )

    assert battery.backup_reserve_calls == []
    assert coordinator._last_executed_action == "self_consumption"


def test_sungrow_idle_does_not_discard_pending_reserve_restore_state(opt_module):
    class SungrowEnergyCoordinator(_FakeEnergyCoordinator):
        async def set_backup_mode(self):
            return True

    battery = _FakeBattery(backup_reserve=10)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.984)
    coordinator.battery_system = "sungrow"
    coordinator.energy_coordinator = SungrowEnergyCoordinator()
    coordinator._pre_idle_backup_reserve = 10
    coordinator._idle_hold_reserve = 98

    assert asyncio.run(coordinator._set_idle_hold_mode(battery)) is True

    assert battery.backup_reserve_calls == []
    assert coordinator._pre_idle_backup_reserve == 10
    assert coordinator._idle_hold_reserve == 98


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


def test_goodwe_no_idle_overrides_residual_hold_without_entering_conserve(
    opt_module,
):
    class GoodWeEnergyCoordinator:
        def __init__(self):
            self.backup_mode_calls = 0
            self.restore_work_mode_from_idle_calls = 0

        async def set_backup_mode(self):
            self.backup_mode_calls += 1
            return True

        async def restore_work_mode_from_idle(self):
            self.restore_work_mode_from_idle_calls += 1

    battery = _FakeBattery(backup_reserve=20)
    energy_coordinator = GoodWeEnergyCoordinator()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.92)
    coordinator.battery_system = "goodwe"
    coordinator.energy_coordinator = energy_coordinator
    coordinator._config.disable_idle_enabled = True
    coordinator._startup_backup_reserve = 15

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="idle", power_w=0)
        )
    )

    assert energy_coordinator.backup_mode_calls == 0
    assert battery.self_consumption_calls == 0
    assert battery.backup_reserve_calls == []
    assert coordinator._pre_idle_backup_reserve is None
    assert coordinator._idle_hold_reserve is None
    assert coordinator._last_executed_action == "self_consumption"


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


def test_charge_at_grid_soc_cap_restores_without_force_command(opt_module):
    """A stale/free-slot charge action cannot bypass the live SOC cap."""
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.90)
    coordinator.battery_system = "fronius_reserva"
    coordinator._config.grid_charge_soc_cap = 0.89
    coordinator._get_energy_data = lambda: {"battery_level": 90}
    action = SimpleNamespace(action="charge", power_w=5000)

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.force_charge_calls == []
    assert battery.restore_normal_calls == 1
    assert coordinator._optimizer_force_state["active"] is False
    assert coordinator._last_executed_action == "self_consumption"


@pytest.mark.parametrize("requested_w", [384.3, 645.1, 1158.8])
def test_saj_partial_charge_below_grid_cap_does_not_send_full_tou_command(
    opt_module,
    requested_w,
):
    """Ticket #319: a fractional top-up cannot become SAJ's fixed 5 kW mode."""
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.979)
    coordinator.battery_system = "saj_h2"
    coordinator._config.battery_capacity_wh = 20_000
    coordinator._config.max_charge_w = 5_000
    coordinator._config.grid_charge_soc_cap = 0.98
    coordinator._get_energy_data = lambda: {"battery_level": 97.9}
    start = datetime(2026, 8, 3, 3, 45, tzinfo=timezone.utc)
    action = SimpleNamespace(action="charge", power_w=requested_w, timestamp=start)
    coordinator._current_schedule = SimpleNamespace(actions=[action])

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.force_charge_calls == []
    assert battery.restore_normal_calls == 1
    assert coordinator._optimizer_force_state["active"] is False
    assert coordinator._last_executed_action == "self_consumption"


def test_saj_full_fixed_rate_charge_executes_when_slot_fits_below_cap(
    opt_module,
):
    """A complete SAJ fixed-rate slot remains valid when it fits the SOC cap."""
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.95)
    coordinator.battery_system = "saj_h2"
    coordinator._config.battery_capacity_wh = 20_000
    coordinator._config.max_charge_w = 5_000
    coordinator._config.grid_charge_soc_cap = 0.98
    coordinator._get_energy_data = lambda: {"battery_level": 95.0}
    start = datetime(2026, 8, 3, 3, 30, tzinfo=timezone.utc)
    action = SimpleNamespace(action="charge", power_w=5_000, timestamp=start)
    coordinator._current_schedule = SimpleNamespace(actions=[action])

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.force_charge_calls == [(5, 5_000, False)]
    assert battery.restore_normal_calls == 0
    assert coordinator._optimizer_force_state["active"] is True
    assert coordinator._last_executed_action == "charge"


def test_unknown_soc_with_lower_grid_cap_blocks_force_charge(opt_module):
    """A lower cap fails safe when live SOC telemetry is unavailable."""
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.50)
    coordinator._config.grid_charge_soc_cap = 0.89
    coordinator._get_energy_data = lambda: {}
    action = SimpleNamespace(action="charge", power_w=5000)

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.force_charge_calls == []
    assert battery.restore_normal_calls == 1
    assert coordinator._last_executed_action == "self_consumption"


def test_grid_cap_state_read_error_blocks_force_charge(opt_module):
    """Unexpected live-state errors cannot make a configured cap fail open."""
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.50)
    coordinator._config.grid_charge_soc_cap = 0.89

    def _raise_state_error():
        raise RuntimeError("state unavailable")

    coordinator._get_energy_data = _raise_state_error
    action = SimpleNamespace(action="charge", power_w=5000)

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.force_charge_calls == []
    assert battery.restore_normal_calls == 1
    assert coordinator._last_executed_action == "self_consumption"


def test_stale_charge_retries_failed_soc_cap_restore(opt_module):
    """A failed stale-action restore retries before recording cap enforcement."""
    battery = _FakeBattery(restore_normal_result=False)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.90)
    coordinator._config.grid_charge_soc_cap = 0.89
    coordinator._get_energy_data = lambda: {"battery_level": 90}
    action = SimpleNamespace(action="charge", power_w=5000)

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.restore_normal_calls == 1
    assert coordinator._last_executed_planned_action is None

    battery.restore_normal_result = True
    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.restore_normal_calls == 2
    assert coordinator._last_executed_planned_action == "charge"
    assert coordinator._last_executed_action == "self_consumption"


def test_repeated_charge_at_grid_soc_cap_restores_once_then_can_resume(opt_module):
    """An at-cap stale slot is idempotent but can charge again below the cap."""
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.90)
    coordinator.battery_system = "fronius_reserva"
    coordinator._config.grid_charge_soc_cap = 0.89
    coordinator._get_energy_data = lambda: {"battery_level": 90}
    action = SimpleNamespace(action="charge", power_w=5000)

    asyncio.run(coordinator._execute_optimizer_action(action))
    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.restore_normal_calls == 1
    assert battery.force_charge_calls == []

    coordinator._get_energy_data = lambda: {"battery_level": 88}
    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.force_charge_calls == [(10, 5000, False)]


def test_user_force_charge_at_grid_soc_cap_is_untouched(opt_module):
    """The optimizer cap cannot override a user-owned force command."""
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.90)
    coordinator._config.grid_charge_soc_cap = 0.89
    coordinator._force_state_getter = lambda: {
        "active": True,
        "type": "charge",
        "source": "user",
    }
    action = SimpleNamespace(action="charge", power_w=5000)

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.restore_normal_calls == 0
    assert battery.force_charge_calls == []


def test_full_grid_soc_cap_keeps_charge_execution(opt_module):
    """The default 100% cap preserves free-slot force charging."""
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=1.0)
    coordinator._config.grid_charge_soc_cap = 1.0
    action = SimpleNamespace(action="charge", power_w=5000)

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.restore_normal_calls == 0
    assert battery.force_charge_calls == [(10, 5000, False)]


@pytest.mark.parametrize("cap", [None, "invalid"])
def test_invalid_grid_soc_cap_defaults_to_full(opt_module, cap):
    """Missing or malformed persisted caps cannot block optimizer charging."""
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=1.0)
    coordinator._config.grid_charge_soc_cap = cap

    reached, soc, normalized_cap = asyncio.run(
        coordinator._grid_charge_soc_cap_reached()
    )

    assert reached is False
    assert soc is None
    assert normalized_cap == 1.0


def test_active_optimizer_charge_stops_when_live_soc_reaches_cap(opt_module):
    """The SOC cap overrides the optimizer's anti-thrash charge commitment."""
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.88)
    coordinator.battery_system = "fronius_reserva"
    coordinator._config.grid_charge_soc_cap = 0.89
    coordinator._get_energy_data = lambda: {"battery_level": 88}
    start = datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    action = SimpleNamespace(action="charge", power_w=5000, timestamp=start)
    coordinator._current_schedule = SimpleNamespace(actions=[action])

    asyncio.run(coordinator._execute_optimizer_action(action))

    coordinator._get_energy_data = lambda: {"battery_level": 90}
    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.force_charge_calls == [(5, 5000, False)]
    assert battery.restore_normal_calls == 1
    assert coordinator._optimizer_force_state["active"] is False
    assert coordinator._last_executed_action == "self_consumption"


def test_active_optimizer_charge_cap_executes_new_export_same_invocation(opt_module):
    """A capped charge yields directly to a fresh optimizer export action."""
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.63)
    coordinator.battery_system = "fronius_reserva"
    coordinator._config.grid_charge_soc_cap = 0.59
    coordinator._get_energy_data = lambda: {"battery_level": 63}
    coordinator._set_optimizer_force_state("charge", 10, 5000)
    action = SimpleNamespace(
        action="export",
        power_w=4200,
        timestamp=datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc),
    )
    coordinator._current_schedule = SimpleNamespace(actions=[action])

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.restore_normal_calls == 1
    assert battery.force_charge_calls == []
    assert battery.force_discharge_calls == [(5, 4200, False, None)]
    assert coordinator._optimizer_force_state["active"] is True
    assert coordinator._optimizer_force_state["type"] == "discharge"
    assert coordinator._last_executed_action == "export"


def test_active_optimizer_charge_cap_restore_is_reentrant_safe(opt_module):
    """A restore callback cannot duplicate the transition's export command."""

    class _ReentrantRestoreBattery(_FakeBattery):
        def __init__(self):
            super().__init__()
            self.coordinator = None
            self.reentrant_action = None

        async def restore_normal(self):
            self.restore_normal_calls += 1
            if self.coordinator is not None and self.reentrant_action is not None:
                await self.coordinator._execute_optimizer_action(self.reentrant_action)
            return self.restore_normal_result

    battery = _ReentrantRestoreBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.63)
    coordinator.battery_system = "fronius_reserva"
    coordinator._config.grid_charge_soc_cap = 0.59
    coordinator._get_energy_data = lambda: {"battery_level": 63}
    coordinator._set_optimizer_force_state("charge", 10, 5000)
    action = SimpleNamespace(
        action="export",
        power_w=4200,
        timestamp=datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc),
    )
    coordinator._current_schedule = SimpleNamespace(actions=[action])
    battery.coordinator = coordinator
    battery.reentrant_action = action

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.restore_normal_calls == 1
    assert battery.force_discharge_calls == [(5, 4200, False, None)]
    assert coordinator._optimizer_force_state["type"] == "discharge"
    assert coordinator._last_executed_action == "export"


def test_active_optimizer_charge_cap_restore_failure_keeps_export_pending(opt_module):
    """A failed cap restore rolls back charge state and does not export."""
    battery = _FakeBattery(restore_normal_result=False)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.63)
    coordinator.battery_system = "fronius_reserva"
    coordinator._config.grid_charge_soc_cap = 0.59
    coordinator._get_energy_data = lambda: {"battery_level": 63}
    coordinator._set_optimizer_force_state("charge", 10, 5000)
    action = SimpleNamespace(
        action="export",
        power_w=4200,
        timestamp=datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc),
    )
    coordinator._current_schedule = SimpleNamespace(actions=[action])

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.restore_normal_calls == 1
    assert battery.force_discharge_calls == []
    assert coordinator._optimizer_force_state["active"] is True
    assert coordinator._optimizer_force_state["type"] == "charge"
    assert coordinator._last_executed_action == "self_consumption"


def test_active_optimizer_charge_retries_failed_soc_cap_restore(opt_module):
    """A failed cap restore retains force state until a retry succeeds."""
    battery = _FakeBattery(restore_normal_result=False)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.88)
    coordinator.battery_system = "fronius_reserva"
    coordinator._config.grid_charge_soc_cap = 0.89
    coordinator._get_energy_data = lambda: {"battery_level": 88}
    start = datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    action = SimpleNamespace(action="charge", power_w=5000, timestamp=start)
    coordinator._current_schedule = SimpleNamespace(actions=[action])

    asyncio.run(coordinator._execute_optimizer_action(action))

    coordinator._get_energy_data = lambda: {"battery_level": 90}
    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.restore_normal_calls == 1
    assert coordinator._optimizer_force_state["active"] is True

    battery.restore_normal_result = True
    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.restore_normal_calls == 2
    assert coordinator._optimizer_force_state["active"] is False
    assert coordinator._last_executed_action == "self_consumption"


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


def test_optimizer_owned_priority_export_holds_after_one_solve_drops_export(opt_module):
    """A fresh LP omission cannot flap an active priority export after five minutes."""
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.90)
    coordinator.battery_system = "fronius_reserva"
    coordinator._optimizer = SimpleNamespace(efficiency=0.92)
    coordinator._config.battery_capacity_wh = 38600
    coordinator._config.max_discharge_w = 8000
    start = datetime(2026, 8, 6, 7, 55, tzinfo=timezone.utc)
    current_time = {"now": start}
    opt_module.dt_util.utcnow = lambda *args, **kwargs: current_time["now"]
    opt_module.dt_util.now = lambda *args, **kwargs: current_time["now"]
    initial_action = SimpleNamespace(
        action="export",
        power_w=8000,
        timestamp=start,
        soc=0.882,
    )
    coordinator._last_price_timestamps = [start]
    coordinator._last_battery_export_allowed_slots = [True]
    coordinator._last_priority_export_slots = [True]
    coordinator._current_schedule = SimpleNamespace(
        actions=[
            initial_action,
            SimpleNamespace(
                action="export",
                power_w=8000,
                timestamp=start + timedelta(minutes=5),
                soc=0.864,
            ),
        ]
    )

    asyncio.run(coordinator._execute_optimizer_action(initial_action))
    current_time["now"] = start + timedelta(minutes=5)
    dropped_action = SimpleNamespace(
        action="self_consumption",
        power_w=0,
        timestamp=start + timedelta(minutes=5),
        soc=0.89,
    )
    coordinator._last_price_timestamps = [dropped_action.timestamp]
    coordinator._last_battery_export_allowed_slots = [True]
    coordinator._last_priority_export_slots = [True]
    coordinator._current_schedule = SimpleNamespace(actions=[dropped_action])

    asyncio.run(coordinator._execute_optimizer_action(dropped_action))

    assert battery.force_discharge_calls == [(10, 8000, False, None)]
    assert battery.restore_normal_calls == 0
    assert coordinator._optimizer_force_state["active"] is True
    assert coordinator._last_executed_action == "export"


def test_optimizer_owned_priority_export_releases_when_fresh_gate_closes(opt_module):
    """The commitment cannot carry export past a fresh hard eligibility gate."""
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.90)
    coordinator.battery_system = "fronius_reserva"
    coordinator._optimizer = SimpleNamespace(efficiency=0.92)
    coordinator._config.battery_capacity_wh = 38600
    coordinator._config.max_discharge_w = 8000
    start = datetime(2026, 8, 6, 7, 55, tzinfo=timezone.utc)
    current_time = {"now": start}
    opt_module.dt_util.utcnow = lambda *args, **kwargs: current_time["now"]
    opt_module.dt_util.now = lambda *args, **kwargs: current_time["now"]
    initial_action = SimpleNamespace(
        action="export",
        power_w=8000,
        timestamp=start,
        soc=0.882,
    )
    coordinator._last_price_timestamps = [start]
    coordinator._last_battery_export_allowed_slots = [True]
    coordinator._last_priority_export_slots = [True]
    coordinator._current_schedule = SimpleNamespace(
        actions=[
            initial_action,
            SimpleNamespace(
                action="export",
                power_w=8000,
                timestamp=start + timedelta(minutes=5),
                soc=0.864,
            ),
        ]
    )

    asyncio.run(coordinator._execute_optimizer_action(initial_action))
    current_time["now"] = start + timedelta(minutes=5)
    stopped_action = SimpleNamespace(
        action="self_consumption",
        power_w=0,
        timestamp=start + timedelta(minutes=5),
        soc=0.89,
    )
    coordinator._last_price_timestamps = [stopped_action.timestamp]
    coordinator._last_battery_export_allowed_slots = [False]
    coordinator._last_priority_export_slots = [False]
    coordinator._current_schedule = SimpleNamespace(actions=[stopped_action])

    asyncio.run(coordinator._execute_optimizer_action(stopped_action))

    assert battery.restore_normal_calls == 1
    assert coordinator._optimizer_force_state["active"] is False
    assert coordinator._last_executed_action == "self_consumption"


def test_optimizer_owned_priority_export_commitment_respects_reserve(opt_module):
    """The anti-flap hold releases when its remaining energy would cross reserve."""
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.215)
    coordinator.battery_system = "fronius_reserva"
    coordinator._optimizer = SimpleNamespace(efficiency=0.92)
    coordinator._config.battery_capacity_wh = 13500
    coordinator._config.max_discharge_w = 5000
    coordinator._config.backup_reserve = 0.20
    start = datetime(2026, 8, 6, 7, 55, tzinfo=timezone.utc)
    current_time = {"now": start}
    opt_module.dt_util.utcnow = lambda *args, **kwargs: current_time["now"]
    opt_module.dt_util.now = lambda *args, **kwargs: current_time["now"]
    coordinator._set_optimizer_force_state("discharge", 20, 5000)
    current_time["now"] = start + timedelta(minutes=5)
    stopped_action = SimpleNamespace(
        action="self_consumption",
        power_w=0,
        timestamp=start + timedelta(minutes=5),
        soc=0.215,
    )
    coordinator._last_price_timestamps = [stopped_action.timestamp]
    coordinator._last_battery_export_allowed_slots = [True]
    coordinator._last_priority_export_slots = [True]
    coordinator._current_schedule = SimpleNamespace(actions=[stopped_action])

    asyncio.run(coordinator._execute_optimizer_action(stopped_action))

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


def test_optimizer_owned_saj_charge_does_not_rewrite_active_tou_slot(opt_module):
    """An active SAJ TOU command must not be treated as an external force."""
    now = datetime(2026, 7, 28, 1, 5, tzinfo=timezone.utc)
    opt_module.dt_util.utcnow = lambda *args, **kwargs: now
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.64)
    coordinator.battery_system = "saj_h2"
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "app_mode": 1,
            "battery_power": -2.5,
        }
    )
    actions = [
        SimpleNamespace(
            action="charge",
            power_w=5000,
            timestamp=now + idx * timedelta(minutes=5),
        )
        for idx in range(6)
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)
    coordinator._set_optimizer_force_state("charge", 75, 5000)
    shared_force_state = {
        "active": True,
        "type": "charge",
        "source": "optimizer",
        "expires_at": now + timedelta(minutes=75),
        "hardware_expires_at": now + timedelta(minutes=75),
        "power_w": 5000,
    }
    coordinator.hass.data = {
        "power_sync": {
            "entry-1": {
                "force_charge_state": shared_force_state,
            }
        }
    }
    coordinator._force_state_getter = lambda: shared_force_state

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))

    assert battery.force_charge_calls == []
    assert coordinator._get_active_force_state()["scope"] == "optimizer"


def test_optimizer_owned_saj_charge_reissues_when_app_mode_drops(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.64)
    coordinator.battery_system = "saj_h2"
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "app_mode": 0,
            "battery_power": -2.5,
        }
    )

    assert coordinator._force_charge_hardware_needs_refresh(5000) is True


def test_optimizer_owned_saj_failed_refresh_retains_retry_state(opt_module):
    now = datetime(2026, 7, 28, 1, 5, tzinfo=timezone.utc)
    opt_module.dt_util.utcnow = lambda *args, **kwargs: now
    battery = _FakeBattery(force_charge_result=False)
    coordinator = _execution_coordinator(opt_module, battery, soc=0.64)
    coordinator.battery_system = "saj_h2"
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "app_mode": 0,
            "battery_power": -2.5,
        }
    )
    actions = [
        SimpleNamespace(
            action="charge",
            power_w=5000,
            timestamp=now + idx * timedelta(minutes=5),
        )
        for idx in range(6)
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)
    coordinator._set_optimizer_force_state("charge", 12, 5000)
    prior_hardware_expiry = coordinator._optimizer_force_state[
        "hardware_expires_at"
    ]

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))

    assert battery.force_charge_calls == [(30, 5000.0, True)]
    assert (
        coordinator._optimizer_force_state["hardware_expires_at"]
        == prior_hardware_expiry
    )


@pytest.mark.parametrize("app_mode", [None, "unknown"])
def test_optimizer_owned_saj_charge_does_not_guess_from_power_without_app_mode(
    opt_module,
    app_mode,
):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.64)
    coordinator.battery_system = "saj_h2"
    coordinator.energy_coordinator = SimpleNamespace(
        data={
            "app_mode": app_mode,
            "battery_power": -2.5,
        }
    )

    assert coordinator._force_charge_hardware_needs_refresh(5000) is False


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


def test_bridge_short_export_gaps_authoritative_floor_ignores_active_auto_reserve(
    opt_module,
):
    coordinator = _execution_coordinator(opt_module, _FakeBattery(), soc=0.60)
    coordinator._config.backup_reserve = 0.70
    coordinator._config.battery_capacity_wh = 10000
    coordinator._config.max_discharge_w = 5000
    coordinator._optimizer = SimpleNamespace(efficiency=1.0)
    start = datetime(2026, 5, 3, 18, 30, tzinfo=timezone.utc)

    def _schedule():
        return SimpleNamespace(
            actions=[
                SimpleNamespace(
                    action="export",
                    power_w=1000,
                    battery_charge_w=0,
                    battery_discharge_w=1000,
                    soc=0.60,
                    timestamp=start,
                ),
                SimpleNamespace(
                    action="self_consumption",
                    power_w=500,
                    battery_charge_w=0,
                    battery_discharge_w=500,
                    soc=0.59,
                    timestamp=start + timedelta(minutes=5),
                ),
                SimpleNamespace(
                    action="export",
                    power_w=1000,
                    battery_charge_w=0,
                    battery_discharge_w=1000,
                    soc=0.58,
                    timestamp=start + timedelta(minutes=10),
                ),
            ]
        )

    active_floor_schedule = _schedule()
    coordinator._bridge_short_export_gaps(
        active_floor_schedule,
        [0.45, 0.45, 0.45],
    )
    assert active_floor_schedule.actions[1].action == "self_consumption"

    reference_floor_schedule = _schedule()
    coordinator._bridge_short_export_gaps(
        reference_floor_schedule,
        [0.45, 0.45, 0.45],
        authoritative_reserve_floor=0.35,
    )
    assert reference_floor_schedule.actions[1].action == "export"


def test_bridge_short_export_gaps_rejects_two_floor_authorities(opt_module):
    coordinator = _coordinator(opt_module, "globird")
    schedule = SimpleNamespace(actions=[])

    with pytest.raises(ValueError, match="mutually exclusive"):
        coordinator._bridge_short_export_gaps(
            schedule,
            [0.45],
            export_reserve_floor=0.20,
            authoritative_reserve_floor=0.30,
        )


def test_future_export_protection_floor_blocks_post_solve_gap_bridge(opt_module):
    coordinator = _execution_coordinator(opt_module, _FakeBattery(), soc=0.35)
    coordinator._config.backup_reserve = 0.20
    coordinator._config.battery_capacity_wh = 10000
    coordinator._config.max_discharge_w = 5000
    coordinator._optimizer = SimpleNamespace(efficiency=1.0)
    start = datetime(2026, 5, 3, 18, 30, tzinfo=timezone.utc)
    actions = [
        SimpleNamespace(
            action="export",
            power_w=1000,
            battery_charge_w=0,
            battery_discharge_w=1000,
            soc=0.35,
            timestamp=start,
        ),
        SimpleNamespace(
            action="self_consumption",
            power_w=500,
            battery_charge_w=0,
            battery_discharge_w=500,
            soc=0.34,
            timestamp=start + timedelta(minutes=5),
        ),
        SimpleNamespace(
            action="export",
            power_w=1000,
            battery_charge_w=0,
            battery_discharge_w=1000,
            soc=0.33,
            timestamp=start + timedelta(minutes=10),
        ),
    ]
    merged_floor = coordinator._merge_export_protection_floors(
        0.20,
        [0.0, 0.34, 0.0],
        len(actions),
    )

    coordinator._bridge_short_export_gaps(
        SimpleNamespace(actions=actions),
        [0.45, 0.45, 0.45],
        authoritative_reserve_floor=merged_floor,
    )

    assert merged_floor == [0.20, 0.34, 0.20]
    assert actions[1].action == "self_consumption"
    assert actions[1].battery_discharge_w == 500


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


def test_no_idle_overrides_charge_by_time_prefill_hold(opt_module):
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
        "self_consumption",
    ]
    assert all(
        action.battery_discharge_w == 2000.0 for action in converted.actions
    )
    assert converted.actions[0].soc < 0.80
    assert converted.actions[1].soc < converted.actions[0].soc


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
    _registered_electricity_providers(),
)
def test_no_idle_schedule_guard_supports_every_provider(opt_module, provider):
    coordinator = _coordinator(opt_module, provider)
    coordinator._config.disable_idle_enabled = True

    assert coordinator._should_disable_idle_schedule() is True


def test_no_idle_setting_accepts_formerly_unsupported_provider(opt_module):
    coordinator = _coordinator(opt_module, "amber")
    updates, run_calls, background_tasks = _prepare_enabled_settings_coordinator(
        coordinator
    )

    result = asyncio.run(coordinator.set_settings({"disable_idle_enabled": True}))

    assert result["success"] is True
    assert result["changes"] == ["disable_idle_enabled: True"]
    assert coordinator._config.disable_idle_enabled is True
    assert coordinator.disable_idle_enabled is True
    assert updates[-1]["options"]["optimization_disable_idle"] is True
    assert run_calls == []
    assert background_tasks == ["powersync_settings_reoptimize"]


def test_no_idle_setting_noop_preserves_formerly_unsupported_provider_value(opt_module):
    coordinator = _coordinator(opt_module, "amber")
    coordinator._config.disable_idle_enabled = True
    updates, run_calls, background_tasks = _prepare_enabled_settings_coordinator(
        coordinator
    )

    result = asyncio.run(coordinator.set_settings({"disable_idle_enabled": True}))

    assert result["success"] is True
    assert result["changes"] == []
    assert coordinator._config.disable_idle_enabled is True
    assert coordinator.disable_idle_enabled is True
    assert updates == []
    assert run_calls == []
    assert background_tasks == []


def test_no_idle_setting_reconciles_stale_runtime_without_stuck_reload_flag(
    opt_module,
):
    """A saved true value must repair a stale false runtime value in place.

    Resubmitting the unchanged persisted value does not fire Home Assistant's
    config-entry update listener, so the reconciliation must also avoid leaving
    ``_skip_reload`` set for the next unrelated structural change.
    """
    coordinator = _coordinator(
        opt_module,
        "flow_power",
        optimization_disable_idle=True,
    )
    coordinator._entry.data["optimization_disable_idle"] = True
    coordinator._config.disable_idle_enabled = False
    updates, run_calls, background_tasks = _prepare_enabled_settings_coordinator(
        coordinator
    )
    entry_data = coordinator.hass.data["power_sync"]["entry-1"]

    result = asyncio.run(
        coordinator.set_settings({"disable_idle_enabled": True})
    )

    assert result["success"] is True
    assert result["changes"] == ["disable_idle_enabled: True"]
    assert coordinator.disable_idle_enabled is True
    assert updates == []
    assert entry_data.get("_skip_reload") is not True
    assert run_calls == []
    assert background_tasks == ["powersync_settings_reoptimize"]


def test_flow_power_no_idle_executor_overrides_residual_idle(opt_module):
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
    assert battery.backup_reserve_calls == [20]
    assert coordinator._idle_hold_reserve is None
    assert coordinator._last_executed_action == "self_consumption"


def test_sigenergy_no_idle_overrides_residual_idle_without_discharge_hold(opt_module):
    """No Idle must not turn a residual action into a Sigenergy hold."""
    battery = _FakeBattery(backup_reserve=20)
    energy_coordinator = _FakeEnergyCoordinator()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.37)
    coordinator._entry.options["electricity_provider"] = "flow_power"
    coordinator._config.disable_idle_enabled = True
    coordinator.battery_system = "sigenergy"
    coordinator.energy_coordinator = energy_coordinator
    coordinator._last_executed_action = "self_consumption"

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="idle", power_w=0)
        )
    )

    assert energy_coordinator.no_discharge_calls == 0
    assert battery.backup_reserve_calls == []
    assert coordinator._last_executed_action == "self_consumption"

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )

    assert energy_coordinator.restore_no_discharge_calls == 0
    assert energy_coordinator.restore_work_mode_from_idle_calls == 0
    assert coordinator._last_executed_action == "self_consumption"


def test_sigenergy_stale_no_idle_hold_retries_failed_restore(opt_module):
    """An upgrade must still retry release of a previously retained hold."""
    battery = _FakeBattery(backup_reserve=20)
    energy_coordinator = _FakeEnergyCoordinator()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.37)
    coordinator._entry.options["electricity_provider"] = "flow_power"
    coordinator._config.disable_idle_enabled = True
    coordinator.battery_system = "sigenergy"
    coordinator.energy_coordinator = energy_coordinator

    coordinator._idle_no_discharge_active = True
    coordinator._last_executed_action = "idle"
    energy_coordinator.restore_no_discharge_result = False

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )

    assert energy_coordinator.restore_no_discharge_calls == 1
    assert coordinator._idle_no_discharge_active is True
    assert coordinator._last_executed_action == "idle"
    assert battery.self_consumption_calls == 0

    energy_coordinator.restore_no_discharge_result = True
    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="self_consumption", power_w=0)
        )
    )

    assert energy_coordinator.restore_no_discharge_calls == 2
    assert coordinator._idle_no_discharge_active is False
    assert coordinator._last_executed_action == "self_consumption"


def test_sigenergy_failed_disable_cleanup_retries_before_enable(opt_module):
    """A failed disable cleanup must be retried before a new optimizer run."""

    async def _run():
        battery = _FakeBattery(backup_reserve=20)
        energy_coordinator = _FakeEnergyCoordinator()
        coordinator = _execution_coordinator(opt_module, battery, soc=0.37)
        coordinator.battery_system = "sigenergy"
        coordinator.energy_coordinator = energy_coordinator
        coordinator._idle_no_discharge_active = True
        coordinator._last_executed_action = "idle"
        coordinator._enabled = True
        coordinator._polling_task = None
        coordinator._initial_opt_task = None
        coordinator._deferred_restore_task = None
        coordinator._settings_reoptimize_task = None
        coordinator._price_reoptimize_task = None
        coordinator._price_listener_unsub = None
        coordinator._octopus_gate_listener_unsub = None
        coordinator._ev_coordinator = None
        coordinator._ev_configs = []
        coordinator._cost_store = SimpleNamespace(
            async_save=lambda *_args: asyncio.sleep(0)
        )
        coordinator._cost_data_to_save = lambda: {}

        async def _stop(*, restore_normal=True):
            return None

        async def _start(*, use_periodic_timer=False):
            return True

        coordinator._executor = SimpleNamespace(
            battery_controller=battery,
            stop=_stop,
            start=_start,
            set_config=lambda _config: None,
        )
        coordinator._optimizer = object()
        coordinator._setup_price_listener = lambda: asyncio.sleep(0)

        background_tasks = []

        def _background_task(coro, name):
            coro.close()
            background_tasks.append(name)
            return None

        coordinator.hass.async_create_background_task = _background_task

        energy_coordinator.restore_no_discharge_result = False
        await coordinator.disable()

        assert coordinator._enabled is False
        assert coordinator._idle_no_discharge_active is True
        assert energy_coordinator.restore_no_discharge_calls == 1

        energy_coordinator.restore_no_discharge_result = True
        assert await coordinator.enable() is True
        assert coordinator._idle_no_discharge_active is False
        assert energy_coordinator.restore_no_discharge_calls == 2
        assert background_tasks == [
            "powersync_enable_restore",
            "powersync_initial_optimization",
            "powersync_schedule_polling",
        ]

    asyncio.run(_run())


def test_flow_power_no_idle_monitoring_reports_effective_self_consumption(
    opt_module,
    caplog,
):
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
    assert "Optimizer would execute: idle" not in caplog.text
    assert "Optimizer would execute: self_consumption" in caplog.text


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


def test_bridged_sungrow_gap_preserves_committed_force_power(opt_module):
    """A bridged gap must not refresh a committed 10 kW export at LP gap power."""
    now = datetime(2026, 5, 3, 18, 30, tzinfo=timezone.utc)
    opt_module.dt_util.now = lambda *args, **kwargs: now
    opt_module.dt_util.utcnow = lambda *args, **kwargs: now
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    coordinator.battery_system = "sungrow"
    coordinator._config.max_discharge_w = 10000
    coordinator._config.battery_capacity_wh = 30000
    coordinator._optimizer = SimpleNamespace(efficiency=0.92)
    actions = [
        SimpleNamespace(
            action="export",
            power_w=329,
            battery_charge_w=0,
            battery_discharge_w=329,
            timestamp=now,
        ),
        SimpleNamespace(
            action="self_consumption",
            power_w=329,
            battery_charge_w=0,
            battery_discharge_w=329,
            timestamp=now + timedelta(minutes=5),
        ),
        SimpleNamespace(
            action="export",
            power_w=10000,
            battery_charge_w=0,
            battery_discharge_w=10000,
            timestamp=now + timedelta(minutes=10),
        ),
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)
    coordinator._bridge_short_export_gaps(
        coordinator._current_schedule,
        [0.45, 0.45, 0.45],
    )
    coordinator._last_export_prices = [0.45, 0.45, 0.45]
    coordinator._last_price_timestamps = [
        action.timestamp for action in actions
    ]
    coordinator._last_battery_export_allowed_slots = [True, True, True]
    coordinator._last_priority_export_slots = [True, True, True]
    coordinator._set_optimizer_force_state("discharge", 20, 10000)
    coordinator._last_executed_action = "export"

    asyncio.run(coordinator._execute_optimizer_action(actions[1]))

    assert battery.force_discharge_calls == []
    assert coordinator._optimizer_force_state["active"] is True
    assert coordinator._optimizer_force_state["power_w"] == 10000


def test_reconciled_bridged_sungrow_gap_preserves_committed_force_power(
    opt_module,
):
    """Reconciliation restamping must retain the bridge commitment marker."""
    now = datetime(2026, 5, 3, 18, 30, tzinfo=timezone.utc)
    opt_module.dt_util.now = lambda *args, **kwargs: now
    opt_module.dt_util.utcnow = lambda *args, **kwargs: now
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    coordinator.battery_system = "sungrow"
    coordinator._config.max_discharge_w = 10000
    coordinator._config.battery_capacity_wh = 30000
    source_actions = [
        SimpleNamespace(
            action="export",
            power_w=329,
            battery_charge_w=0,
            battery_discharge_w=329,
            timestamp=now,
        ),
        SimpleNamespace(
            action="self_consumption",
            power_w=329,
            battery_charge_w=0,
            battery_discharge_w=329,
            timestamp=now + timedelta(minutes=5),
        ),
        SimpleNamespace(
            action="export",
            power_w=10000,
            battery_charge_w=0,
            battery_discharge_w=10000,
            timestamp=now + timedelta(minutes=10),
        ),
    ]
    source_schedule = SimpleNamespace(actions=source_actions)
    coordinator._bridge_short_export_gaps(
        source_schedule,
        [0.45, 0.45, 0.45],
    )

    # Model reconcile_result_with_schedule's ScheduleAction restamping: the
    # rebuilt instances cannot carry dynamic attributes from the source.
    class _Reconciler:
        efficiency = 0.92

        def reconcile_result_with_schedule(self, result, schedule, **kwargs):
            return SimpleNamespace(
                schedule=SimpleNamespace(
                    actions=[
                        opt_module.ScheduleAction(
                            timestamp=action.timestamp,
                            action=action.action,
                            power_w=round(action.power_w, 1),
                            soc=getattr(action, "soc", None),
                            battery_charge_w=action.battery_charge_w,
                            battery_discharge_w=action.battery_discharge_w,
                        )
                        for action in schedule.actions
                    ]
                )
            )

    coordinator._optimizer = _Reconciler()
    reconciled_result = coordinator._optimizer.reconcile_result_with_schedule(
        SimpleNamespace(),
        source_schedule,
    )
    reconciled_schedule = reconciled_result.schedule
    coordinator._restore_bridged_export_gap_provenance(
        source_schedule,
        reconciled_schedule,
    )
    assert getattr(
        reconciled_schedule.actions[1],
        "_optimizer_bridged_export_gap",
        False,
    ) is True

    coordinator._current_schedule = reconciled_schedule
    coordinator._last_export_prices = [0.45, 0.45, 0.45]
    coordinator._last_price_timestamps = [
        action.timestamp for action in reconciled_schedule.actions
    ]
    coordinator._last_battery_export_allowed_slots = [True, True, True]
    coordinator._last_priority_export_slots = [True, True, True]
    coordinator._set_optimizer_force_state("discharge", 20, 10000)

    asyncio.run(
        coordinator._execute_optimizer_action(reconciled_schedule.actions[1])
    )

    assert battery.force_discharge_calls == []
    assert coordinator._optimizer_force_state["power_w"] == 10000


def test_bridged_sungrow_gap_releases_expired_commitment(opt_module):
    """Once the 20-minute hold expires, a bridged LP target may be applied."""
    now = datetime(2026, 5, 3, 18, 55, tzinfo=timezone.utc)
    opt_module.dt_util.now = lambda *args, **kwargs: now
    opt_module.dt_util.utcnow = lambda *args, **kwargs: now
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    coordinator.battery_system = "sungrow"
    coordinator._config.max_discharge_w = 10000
    actions = [
        SimpleNamespace(
            action="export",
            power_w=329,
            battery_charge_w=0,
            battery_discharge_w=329,
            timestamp=now,
        ),
        SimpleNamespace(
            action="self_consumption",
            power_w=329,
            battery_charge_w=0,
            battery_discharge_w=329,
            timestamp=now + timedelta(minutes=5),
        ),
        SimpleNamespace(
            action="export",
            power_w=10000,
            battery_charge_w=0,
            battery_discharge_w=10000,
            timestamp=now + timedelta(minutes=10),
        ),
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)
    coordinator._bridge_short_export_gaps(
        coordinator._current_schedule,
        [0.45, 0.45, 0.45],
    )
    coordinator._last_export_prices = [0.45, 0.45, 0.45]
    coordinator._last_price_timestamps = [
        action.timestamp for action in actions
    ]
    coordinator._last_battery_export_allowed_slots = [True, True, True]
    coordinator._last_priority_export_slots = [True, True, True]
    coordinator._set_optimizer_force_state("discharge", 20, 10000)
    coordinator._optimizer_force_state["started_at"] = now - timedelta(minutes=21)
    coordinator._optimizer_force_state["expires_at"] = now + timedelta(minutes=5)
    coordinator._optimizer_force_state["hardware_expires_at"] = now + timedelta(minutes=5)

    asyncio.run(coordinator._execute_optimizer_action(actions[1]))

    assert battery.restore_normal_calls == 1
    assert battery.force_discharge_calls == [(10, 329, False, None)]
    assert coordinator._optimizer_force_state["power_w"] == 329


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
    assert [action.power_w for action in spread.actions] == [1666.6] * 6
    original_wh = sum(action.battery_discharge_w for action in actions) * (5 / 60)
    spread_wh = sum(action.battery_discharge_w for action in spread.actions) * (5 / 60)
    assert spread_wh == pytest.approx(original_wh, abs=0.1)


def test_spread_export_schedule_stops_at_export_price_boundary(opt_module):
    coordinator = _coordinator(opt_module, "agl")
    coordinator.battery_system = "goodwe"
    coordinator._config.spread_export_enabled = True
    coordinator._config.max_discharge_w = 5000
    start = datetime(2026, 7, 25, 17, 0, tzinfo=timezone.utc)
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

    spread = coordinator._spread_export_schedule(
        schedule,
        [True] * 6,
        export_prices=[0.27, 0.27, 0.27, 0.02, 0.02, 0.02],
    )

    assert [action.action for action in spread.actions] == [
        "export",
        "export",
        "export",
        "self_consumption",
        "self_consumption",
        "self_consumption",
    ]
    assert [action.power_w for action in spread.actions] == [
        3333.3,
        3333.3,
        3333.3,
        0,
        0,
        0,
    ]


def test_spread_export_schedule_preserves_covau_bonus_price_window(opt_module):
    coordinator = _coordinator(opt_module, "covau")
    coordinator.battery_system = "saj_h2"
    coordinator._config.spread_export_enabled = True
    coordinator._config.max_discharge_w = 5000
    coordinator._last_zerohero_bonus_prices = [
        0.0,
        0.0,
        0.0,
        0.10,
        0.10,
        0.10,
        0.0,
        0.0,
        0.0,
        0.50,
    ]
    start = datetime(2026, 7, 26, 17, 45, tzinfo=timezone.utc)
    actions = [
        opt_module.ScheduleAction(
            timestamp=start + idx * timedelta(minutes=5),
            action="export" if idx in (3, 4) else "self_consumption",
            power_w=5000 if idx in (3, 4) else 0,
            battery_discharge_w=5000 if idx in (3, 4) else 0,
        )
        for idx in range(9)
    ]
    schedule = opt_module.OptimizationSchedule(
        actions=actions,
        predicted_cost=0,
        predicted_savings=0,
        last_updated=start,
    )

    spread_prices = coordinator._spread_export_prices_for_run([0.05] * 9)
    spread = coordinator._spread_export_schedule(
        schedule,
        [True] * 9,
        export_prices=spread_prices,
    )

    assert spread_prices == pytest.approx(
        [0.05] * 3 + [0.15] * 3 + [0.05] * 3
    )
    assert [action.action for action in spread.actions] == [
        "self_consumption",
        "self_consumption",
        "self_consumption",
        "export",
        "export",
        "export",
        "self_consumption",
        "self_consumption",
        "self_consumption",
    ]
    assert [action.power_w for action in spread.actions] == [
        0,
        0,
        0,
        3333.3,
        3333.3,
        3333.3,
        0,
        0,
        0,
    ]


def test_spread_export_schedule_falls_back_for_non_finite_prices(opt_module):
    coordinator = _coordinator(opt_module, "agl")
    coordinator.battery_system = "goodwe"
    coordinator._config.spread_export_enabled = True
    coordinator._config.max_discharge_w = 5000
    start = datetime(2026, 7, 25, 17, 0, tzinfo=timezone.utc)
    actions = [
        opt_module.ScheduleAction(
            timestamp=start + idx * timedelta(minutes=5),
            action="export" if idx == 0 else "self_consumption",
            power_w=3000 if idx == 0 else 0,
            battery_discharge_w=3000 if idx == 0 else 0,
        )
        for idx in range(3)
    ]
    schedule = opt_module.OptimizationSchedule(
        actions=actions,
        predicted_cost=0,
        predicted_savings=0,
        last_updated=start,
    )

    spread = coordinator._spread_export_schedule(
        schedule,
        [True] * 3,
        export_prices=[0.27, float("nan"), 0.02],
    )

    assert [action.action for action in spread.actions] == ["export"] * 3
    assert [action.power_w for action in spread.actions] == [1000.0] * 3


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


@pytest.mark.parametrize(
    "auto_apply,profit_max,disable_idle",
    [
        (False, False, False),
        (False, False, True),
        (False, True, False),
        (False, True, True),
        (True, False, False),
        (True, False, True),
        (True, True, False),
        (True, True, True),
    ],
)
def test_spread_export_reserve_and_home_load_use_full_window_in_all_modes(
    opt_module,
    auto_apply,
    profit_max,
    disable_idle,
):
    coordinator = _coordinator(opt_module, "globird", profit_max=profit_max)
    coordinator.battery_system = "sungrow"
    coordinator._auto_apply_reserve_enabled = auto_apply
    coordinator._config.auto_apply_reserve_enabled = auto_apply
    coordinator._config.profit_max_enabled = profit_max
    coordinator._config.disable_idle_enabled = disable_idle
    coordinator._config.spread_export_enabled = True
    coordinator._config.battery_capacity_wh = 20000
    coordinator._config.max_discharge_w = 7900
    coordinator._config.max_grid_export_w = 5000
    coordinator._optimizer = SimpleNamespace(efficiency=0.92)
    start = datetime(2026, 7, 18, 18, 0, tzinfo=timezone.utc)
    actions = [
        opt_module.ScheduleAction(
            timestamp=start - timedelta(minutes=5),
            action="self_consumption",
            power_w=0,
            soc=0.72,
            battery_discharge_w=0,
        )
    ]
    actions.extend(
        opt_module.ScheduleAction(
            timestamp=start + idx * timedelta(minutes=5),
            action="export" if idx < 18 else "self_consumption",
            power_w=3000 if idx < 18 else 1500,
            soc=max(0.20, 0.72 - idx * 0.02),
            battery_discharge_w=4500 if idx < 18 else 1500,
        )
        for idx in range(24)
    )
    schedule = opt_module.OptimizationSchedule(actions, 0, 0, start)

    spread = coordinator._spread_export_schedule(
        schedule,
        [False] + [True] * 24,
        export_reserve_floor=0.39,
    )

    window = spread.actions[1:]
    assert [action.action for action in window] == ["export"] * 24
    assert [action.power_w for action in window] == [1536.0] * 24
    assert [action.battery_discharge_w for action in window] == [3036.0] * 24
    assert min(action.soc for action in window) >= 0.39
    assert window[-1].soc == pytest.approx(0.39, abs=0.0001)
    spread_export_wh = sum(action.power_w for action in window) * (5 / 60)
    original_export_wh = sum(action.power_w for action in actions[1:19]) * (5 / 60)
    assert spread_export_wh == pytest.approx(3072.0, abs=0.1)
    assert spread_export_wh < original_export_wh


def test_spread_export_emits_none_when_home_load_alone_crosses_floor(opt_module):
    coordinator = _coordinator(opt_module, "globird", profit_max=True)
    coordinator.battery_system = "sigenergy"
    coordinator._config.spread_export_enabled = True
    coordinator._config.battery_capacity_wh = 10000
    coordinator._config.max_discharge_w = 5000
    coordinator._config.max_grid_export_w = 5000
    coordinator._optimizer = SimpleNamespace(efficiency=1.0)
    start = datetime(2026, 7, 18, 18, 0, tzinfo=timezone.utc)
    actions = [
        opt_module.ScheduleAction(
            timestamp=start - timedelta(minutes=5),
            action="self_consumption",
            power_w=0,
            soc=0.42,
            battery_discharge_w=0,
        )
    ]
    actions.extend(
        opt_module.ScheduleAction(
            timestamp=start + idx * timedelta(minutes=5),
            action="export" if idx < 2 else "self_consumption",
            power_w=500 if idx < 2 else 1000,
            soc=0.40,
            battery_discharge_w=1500 if idx < 2 else 1000,
        )
        for idx in range(4)
    )
    schedule = opt_module.OptimizationSchedule(actions, 0, 0, start)

    spread = coordinator._spread_export_schedule(
        schedule,
        [False] + [True] * 4,
        export_reserve_floor=0.39,
    )

    window = spread.actions[1:]
    assert [action.action for action in window] == ["self_consumption"] * 4
    assert [action.power_w for action in window] == [1000.0] * 4
    assert [action.battery_discharge_w for action in window] == [1000.0] * 4
    assert window[-1].soc == pytest.approx(0.3867, abs=0.0001)


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


def test_spread_export_schedule_spreads_reserve_limited_energy_across_window(opt_module):
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

    assert [action.action for action in spread.actions] == ["export"] * 6
    assert [action.power_w for action in spread.actions] == [2000.0] * 6
    assert min(action.soc for action in spread.actions) >= 0.30 - 1e-6
    assert spread.actions[-1].soc == pytest.approx(0.30, abs=0.0001)


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


def test_schedule_display_grid_import_includes_planned_ev_load(opt_module):
    """The final API flow rebuild must not drop the EV after the LP solve."""
    coordinator = _coordinator(opt_module, "flow_power", profit_max=True)
    coordinator._last_solar_forecast = [0.0]
    coordinator._last_load_forecast = [0.375]
    api_response = {
        "timestamps": ["2026-08-21T01:30:00+10:00"],
        "charge_w": [0.0],
        "ev_charging_w": [7360.0],
        "battery_consume_w": [375.0],
        "battery_export_w": [0.0],
    }

    grid_import, grid_export = coordinator._display_grid_arrays_from_schedule(
        api_response,
        raw_grid_import_w=[7360.0],
        raw_grid_export_w=[0.0],
    )

    assert grid_import == [7360.0]
    assert grid_export == [0.0]


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

    assert [action.action for action in spread.actions] == ["self_consumption"] * 3
    assert [action.power_w for action in spread.actions] == [1000.0, 5000.0, 1000.0]
    assert [action.battery_discharge_w for action in spread.actions] == [
        1000.0,
        5000.0,
        1000.0,
    ]

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


def test_spread_import_preserves_solar_charge_needed_for_deadline(opt_module):
    """Spread Import must not discard LP-counted natural solar charging.

    Charge By Time applies its SOC floor inside the LP.  Solar-surplus slots are
    emitted as self_consumption with battery_charge_w, while deliberate grid
    charging is emitted as charge.  Rewriting the whole equal-price window from
    only the explicit charge energy makes the displayed and executable schedule
    miss that already-solved SOC floor.
    """
    coordinator = _coordinator(opt_module, "agl")
    coordinator.battery_system = "sungrow"
    coordinator._config.spread_import_enabled = True
    coordinator._config.interval_minutes = 5
    coordinator._config.max_charge_w = 5000
    coordinator._config.battery_capacity_wh = 10000
    coordinator._optimizer = SimpleNamespace(efficiency=1.0)
    start = datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc)
    actions = [
        opt_module.ScheduleAction(
            timestamp=start,
            action="charge",
            power_w=5000,
            battery_charge_w=5000,
        ),
        opt_module.ScheduleAction(
            timestamp=start + timedelta(minutes=5),
            action="self_consumption",
            power_w=2000,
            battery_charge_w=2000,
        ),
        opt_module.ScheduleAction(
            timestamp=start + timedelta(minutes=10),
            action="self_consumption",
            power_w=2000,
            battery_charge_w=2000,
        ),
        opt_module.ScheduleAction(
            timestamp=start + timedelta(minutes=15),
            action="self_consumption",
            power_w=0,
            battery_charge_w=0,
        ),
    ]
    schedule = opt_module.OptimizationSchedule(
        actions=actions,
        predicted_cost=0,
        predicted_savings=0,
        last_updated=start,
    )

    spread = coordinator._spread_import_schedule(
        schedule,
        [0.066] * 4,
        [False] * 4,
        initial_soc=0.40,
        solar_forecast=[0.0, 3.0, 3.0, 0.0],
        load_forecast=[0.0, 1.0, 1.0, 0.0],
    )

    assert [action.action for action in spread.actions] == [
        "charge",
        "self_consumption",
        "self_consumption",
        "charge",
    ]
    assert [action.battery_charge_w for action in spread.actions] == [
        2500.0,
        2000,
        2000,
        2500.0,
    ]
    original_wh = sum(action.battery_charge_w for action in actions) * (5 / 60)
    spread_wh = sum(action.battery_charge_w for action in spread.actions) * (5 / 60)
    assert spread_wh == pytest.approx(original_wh, abs=0.1)
    assert spread.actions[-1].soc == pytest.approx(0.475, abs=0.0001)
    assert [action.soc for action in schedule.actions] == [None] * 4


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


def test_zerohero_export_price_gate_uses_bonus_value(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    coordinator.battery_system = "foxess"
    coordinator._entry = SimpleNamespace(
        options={
            "electricity_provider": "globird",
            "globird_plan": "zerohero_jul_2026",
        },
        data={},
    )
    start = datetime(2026, 8, 4, 18, 0, tzinfo=timezone.utc)
    action = SimpleNamespace(
        action="export",
        power_w=4200,
        timestamp=start,
    )
    coordinator._current_schedule = SimpleNamespace(actions=[action])
    coordinator._last_price_timestamps = [start]
    coordinator._last_export_prices = [0.0]
    coordinator._last_zerohero_bonus_prices = [0.10]
    coordinator._last_zerohero_bonus_cap_kwh = 15.0

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.force_discharge_calls == [(5, 4200, False, None)]
    assert battery.self_consumption_calls == 0
    assert coordinator._last_executed_action == "export"


def test_zerohero_export_price_gate_blocks_without_remaining_bonus(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    coordinator.battery_system = "foxess"
    coordinator._last_executed_action = "export"
    coordinator._entry = SimpleNamespace(
        options={
            "electricity_provider": "globird",
            "globird_plan": "zerohero_jul_2026",
        },
        data={},
    )
    start = datetime(2026, 8, 4, 18, 0, tzinfo=timezone.utc)
    action = SimpleNamespace(
        action="export",
        power_w=4200,
        timestamp=start,
    )
    coordinator._current_schedule = SimpleNamespace(actions=[action])
    coordinator._last_price_timestamps = [start]
    coordinator._last_export_prices = [0.0]
    coordinator._last_zerohero_bonus_prices = [0.10]
    coordinator._last_zerohero_bonus_cap_kwh = 0.0

    asyncio.run(coordinator._execute_optimizer_action(action))

    assert battery.force_discharge_calls == []
    assert battery.self_consumption_calls == 1
    assert coordinator._last_executed_action == "self_consumption"


def test_zerohero_export_price_gate_uses_future_day_group_cap(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    coordinator.battery_system = "foxess"
    coordinator._entry = SimpleNamespace(
        options={
            "electricity_provider": "globird",
            "globird_plan": "zerohero_jul_2026",
        },
        data={},
    )
    today = datetime(2026, 8, 6, 18, 0, tzinfo=timezone.utc)
    tomorrow = today + timedelta(days=1)
    action = SimpleNamespace(
        action="export",
        power_w=4200,
        timestamp=tomorrow,
    )
    coordinator._current_schedule = SimpleNamespace(actions=[action])
    coordinator._last_price_timestamps = [today, tomorrow]
    coordinator._last_export_prices = [0.0, 0.0]
    coordinator._last_zerohero_bonus_prices = [0.10, 0.10]
    coordinator._last_zerohero_bonus_cap_kwh = 0.0
    coordinator._last_export_bonus_group_ids = [None, "2026-08-07"]
    coordinator._last_export_bonus_caps_by_group = {"2026-08-07": 15.0}

    asyncio.run(coordinator._execute_optimizer_action(action))

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


def test_external_force_charge_refresh_cancels_service_expiry_timer(opt_module):
    """A service timer created during refresh must not be orphaned."""
    boundary_now = datetime(2026, 7, 16, 3, 30, tzinfo=timezone.utc)
    opt_module.dt_util.now = lambda *args, **kwargs: boundary_now
    opt_module.dt_util.utcnow = lambda *args, **kwargs: boundary_now
    force_state = {
        "active": True,
        "expires_at": boundary_now + timedelta(minutes=1),
        "hardware_expires_at": boundary_now + timedelta(minutes=1),
        "source": "optimizer",
    }
    service_timer_cancellations = []
    service_expiry = boundary_now + timedelta(minutes=10, seconds=3)

    class _RefreshingBattery(_FakeBattery):
        async def force_charge(
            self,
            duration_minutes=60,
            power_w=5000,
            _extend_hardware=False,
        ):
            await super().force_charge(
                duration_minutes=duration_minutes,
                power_w=power_w,
                _extend_hardware=_extend_hardware,
            )
            force_state["cancel_expiry_timer"] = (
                lambda: service_timer_cancellations.append("cancelled")
            )
            force_state["expires_at"] = service_expiry

    battery = _RefreshingBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    actions = [
        SimpleNamespace(
            action="charge",
            power_w=5000,
            timestamp=boundary_now + idx * timedelta(minutes=5),
        )
        for idx in range(2)
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)
    coordinator.hass.data = {
        "power_sync": {
            "entry-1": {
                "force_charge_state": force_state,
            }
        }
    }
    coordinator._force_state_getter = lambda: {
        "active": True,
        "type": "charge",
        "source": "optimizer",
    }

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))

    assert battery.force_charge_calls == [(10, 5000, True)]
    assert service_timer_cancellations == ["cancelled"]
    assert force_state["cancel_expiry_timer"] is not None
    assert coordinator.hass.scheduled[-1][1] == service_expiry


def test_external_force_extension_timer_ignores_stale_callback(opt_module):
    """A queued coordinator timer must honor the latest shared expiry."""
    now = [datetime(2026, 7, 16, 3, 30, tzinfo=timezone.utc)]
    opt_module.dt_util.now = lambda *args, **kwargs: now[0]
    opt_module.dt_util.utcnow = lambda *args, **kwargs: now[0]
    force_state = {
        "active": True,
        "expires_at": now[0] + timedelta(minutes=1),
        "hardware_expires_at": now[0] + timedelta(minutes=60),
        "source": "optimizer",
    }
    restore_calls = []

    async def _record_restore(*args, **kwargs):
        restore_calls.append((args, kwargs))

    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.80)
    coordinator.hass.services = SimpleNamespace(async_call=_record_restore)
    actions = [
        SimpleNamespace(
            action="charge",
            power_w=5000,
            timestamp=now[0] + idx * timedelta(minutes=5),
        )
        for idx in range(2)
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)
    coordinator.hass.data = {
        "power_sync": {
            "entry-1": {
                "force_charge_state": force_state,
            }
        }
    }
    coordinator._force_state_getter = lambda: {
        "active": True,
        "type": "charge",
        "source": "optimizer",
    }

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))
    stale_callback, stale_expiry = coordinator.hass.scheduled[-1]
    now[0] += timedelta(minutes=5)
    actions.append(
        SimpleNamespace(
            action="charge",
            power_w=5000,
            timestamp=now[0] + timedelta(minutes=5),
        )
    )
    asyncio.run(coordinator._execute_optimizer_action(actions[1]))

    assert force_state["expires_at"] > stale_expiry
    asyncio.run(stale_callback(stale_expiry))
    assert restore_calls == []


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


def test_tesla_force_charge_does_not_reupload_same_aligned_tariff_boundary(opt_module):
    """A solve seconds after the cached boundary command must reuse its tariff."""
    boundary = datetime(2026, 7, 20, 1, 0, tzinfo=timezone.utc)
    now = boundary + timedelta(seconds=5)
    opt_module.dt_util.now = lambda *args, **kwargs: now
    opt_module.dt_util.utcnow = lambda *args, **kwargs: now

    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.41)
    actions = [
        SimpleNamespace(
            action="charge",
            power_w=20000,
            timestamp=boundary + idx * timedelta(minutes=5),
        )
        for idx in range(36)
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)

    tariff_boundary = boundary + timedelta(hours=3)
    force_state = {
        "active": True,
        "type": "charge",
        "source": "optimizer",
        "expires_at": tariff_boundary + timedelta(seconds=4),
        "hardware_expires_at": tariff_boundary,
        "power_w": 20000,
    }
    coordinator.hass.data = {
        "power_sync": {
            "entry-1": {
                "force_charge_state": force_state,
            }
        }
    }
    coordinator._force_state_getter = lambda: force_state

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))

    assert battery.force_charge_calls == []
    assert force_state["hardware_expires_at"] == tariff_boundary


def test_tesla_force_charge_reuploads_when_plan_extends_tariff_by_one_slot(opt_module):
    """A real five-minute LP extension must still roll the Tesla tariff forward."""
    now = datetime(2026, 7, 20, 1, 30, tzinfo=timezone.utc)
    opt_module.dt_util.now = lambda *args, **kwargs: now
    opt_module.dt_util.utcnow = lambda *args, **kwargs: now

    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.41)
    actions = [
        SimpleNamespace(
            action="charge",
            power_w=20000,
            timestamp=now + idx * timedelta(minutes=5),
        )
        for idx in range(7)
    ]
    coordinator._current_schedule = SimpleNamespace(actions=actions)

    force_state = {
        "active": True,
        "type": "charge",
        "source": "optimizer",
        "expires_at": now + timedelta(minutes=30),
        "hardware_expires_at": now + timedelta(minutes=30),
        "power_w": 20000,
    }
    coordinator.hass.data = {
        "power_sync": {
            "entry-1": {
                "force_charge_state": force_state,
            }
        }
    }
    coordinator._force_state_getter = lambda: force_state

    asyncio.run(coordinator._execute_optimizer_action(actions[0]))

    assert battery.force_charge_calls == [(35, 20000.0, True)]


def _solar_export_capability_coordinator(opt_module, capability):
    """Coordinator whose adapter reports exactly the supplied capability."""
    coordinator = _sigenergy_profit_max_coordinator(opt_module)
    coordinator._solar_export_hold = SimpleNamespace(
        capability=lambda: dict(capability)
    )
    return coordinator


_SUPPORTED_WITHOUT_LIMIT = {
    "supported": True,
    "reason": "supported",
    "adapter": "fronius_reserva.entity.block_charging.v1",
}


def test_blank_export_limit_is_reported_as_an_unset_setting(opt_module):
    coordinator = _solar_export_capability_coordinator(
        opt_module, _SUPPORTED_WITHOUT_LIMIT
    )
    coordinator._config.max_grid_export_w = None

    status = coordinator._solar_export_capability()

    assert status["supported"] is False
    assert status["reason"] == "export_limit_not_configured"


def test_zero_export_site_is_not_reported_as_a_missing_setting(opt_module):
    coordinator = _solar_export_capability_coordinator(
        opt_module, _SUPPORTED_WITHOUT_LIMIT
    )
    coordinator._config.max_grid_export_w = 0

    status = coordinator._solar_export_capability()

    assert status["supported"] is False
    assert status["reason"] == "zero_export_site"


def test_configured_export_limit_satisfies_the_capability(opt_module):
    coordinator = _solar_export_capability_coordinator(
        opt_module, _SUPPORTED_WITHOUT_LIMIT
    )
    coordinator._config.max_grid_export_w = 5000

    status = coordinator._solar_export_capability()

    assert status["supported"] is True
    assert status["export_limit_kw"] == 5.0
    assert status["export_limit_source"] == "site_setting"


def test_inverter_reported_export_limit_needs_no_site_setting(opt_module):
    coordinator = _solar_export_capability_coordinator(
        opt_module,
        {**_SUPPORTED_WITHOUT_LIMIT, "export_limit_kw": 4.5},
    )
    coordinator._config.max_grid_export_w = None

    status = coordinator._solar_export_capability()

    assert status["supported"] is True
    assert status["export_limit_kw"] == 4.5
    assert status["export_limit_source"] == "inverter_reported"


def test_unset_export_limit_warns_once_and_clears_when_configured(
    opt_module, caplog
):
    coordinator = _solar_export_capability_coordinator(
        opt_module, _SUPPORTED_WITHOUT_LIMIT
    )
    coordinator._config.max_grid_export_w = None

    with caplog.at_level(logging.WARNING, logger=opt_module.__name__):
        coordinator._solar_export_capability()
        coordinator._solar_export_capability()

    warnings = [
        record
        for record in caplog.records
        if "Maximum grid export" in record.getMessage()
    ]
    assert len(warnings) == 1

    coordinator._config.max_grid_export_w = 5000
    assert coordinator._solar_export_capability()["supported"] is True
    assert coordinator._last_solar_export_limit_notice is None


def test_unset_export_limit_rejects_every_slot_with_that_reason(opt_module):
    coordinator = _solar_export_capability_coordinator(
        opt_module, _SUPPORTED_WITHOUT_LIMIT
    )
    coordinator._config.max_grid_export_w = None

    slots = coordinator._profit_max_solar_export_slots(
        [0.40, 0.05],
        [0.50, 0.05],
        [5.0, 0.0],
        [1.0, 1.0],
        0.5,
        [False, False],
        [True, True],
    )

    assert slots == [False, False]
    status = coordinator._solar_export_capability_status
    assert status["reason"] == "export_limit_not_configured"
    assert status["rejection_counts"] == {"export_limit_not_configured": 1}


def _hold_revision_result(opt_module, actions):
    """Minimal solved-result stand-in carrying only the labelled actions."""
    return SimpleNamespace(
        feasible=True,
        schedule=SimpleNamespace(
            actions=[
                SimpleNamespace(action=action, battery_charge_w=charge_w)
                for action, charge_w in actions
            ]
        ),
    )


def test_solar_export_hold_dropped_when_repaid_by_costlier_grid_charge(
    opt_module,
):
    """#383 variant: morning solar sold at 2.4c, replenished from 9.64c grid."""
    coordinator = _sigenergy_profit_max_coordinator(opt_module)
    coordinator._config.interval_minutes = 5

    import_prices = [0.1001, 0.1001, 0.0964, 0.0964]
    export_prices = [0.0241, 0.0241, 0.0204, 0.0204]
    held = [True, True, False, False]
    result = _hold_revision_result(
        opt_module,
        [
            ("solar_export", 0.0),
            ("solar_export", 0.0),
            ("charge", 25000.0),
            ("charge", 25000.0),
        ],
    )

    revised = coordinator._revise_solar_export_holds(
        result, import_prices, export_prices, held
    )

    assert revised == [False, False, False, False]


def test_solar_export_hold_kept_when_replenishment_is_genuinely_cheaper(
    opt_module,
):
    """High feed-in morning funded by a genuinely cheap grid window survives."""
    coordinator = _sigenergy_profit_max_coordinator(opt_module)
    coordinator._config.interval_minutes = 5

    import_prices = [0.35, 0.35, 0.05, 0.05]
    export_prices = [0.33, 0.33, 0.03, 0.03]
    held = [True, True, False, False]
    result = _hold_revision_result(
        opt_module,
        [
            ("solar_export", 0.0),
            ("solar_export", 0.0),
            ("charge", 25000.0),
            ("charge", 25000.0),
        ],
    )

    revised = coordinator._revise_solar_export_holds(
        result, import_prices, export_prices, held
    )

    assert revised == held


def test_solar_export_hold_ignores_grid_charging_before_the_hold(opt_module):
    """Deferred solar cannot displace grid charging already behind it."""
    coordinator = _sigenergy_profit_max_coordinator(opt_module)
    coordinator._config.interval_minutes = 5

    import_prices = [0.0964, 0.0964, 0.1001, 0.1001]
    export_prices = [0.0204, 0.0204, 0.0241, 0.0241]
    held = [False, False, True, True]
    result = _hold_revision_result(
        opt_module,
        [
            ("charge", 25000.0),
            ("charge", 25000.0),
            ("solar_export", 0.0),
            ("solar_export", 0.0),
        ],
    )

    revised = coordinator._revise_solar_export_holds(
        result, import_prices, export_prices, held
    )

    assert revised == held
