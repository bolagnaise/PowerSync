"""Regression tests for provider-scoped battery export permissions."""

from __future__ import annotations

import importlib
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
    ha_dt.now = lambda *args, **kwargs: datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    ha_dt.utcnow = lambda *args, **kwargs: datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    ha_dt.UTC = timezone.utc
    ha_helpers.storage = ha_storage
    ha_helpers.update_coordinator = ha_update
    ha_util.dt = ha_dt
    ha_root.helpers = ha_helpers
    ha_root.util = ha_util

    sys.modules["homeassistant"] = ha_root
    sys.modules["homeassistant.core"] = ha_core
    sys.modules["homeassistant.exceptions"] = ha_exceptions
    sys.modules["homeassistant.helpers"] = ha_helpers
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
    const_module.CONF_ELECTRICITY_PROVIDER = "electricity_provider"
    const_module.CONF_FLOW_POWER_STATE = "flow_power_state"
    const_module.FLOW_POWER_EXPORT_RATES = {"NSW1": 0.45}
    const_module.CONF_EXPORT_BOOST_ENABLED = "export_boost_enabled"
    const_module.CONF_EXPORT_PRICE_OFFSET = "export_price_offset"
    const_module.CONF_EXPORT_MIN_PRICE = "export_min_price"
    const_module.CONF_EXPORT_BOOST_START = "export_boost_start"
    const_module.CONF_EXPORT_BOOST_END = "export_boost_end"
    const_module.CONF_EXPORT_BOOST_THRESHOLD = "export_boost_threshold"
    const_module.DEFAULT_EXPORT_BOOST_START = "17:00"
    const_module.DEFAULT_EXPORT_BOOST_END = "21:00"
    const_module.DEFAULT_EXPORT_BOOST_THRESHOLD = 0.0
    sys.modules["power_sync.const"] = const_module

    battery_module = types.ModuleType("power_sync.optimization.battery_optimizer")
    battery_module.BatteryOptimizer = type("BatteryOptimizer", (), {})
    battery_module.OptimizerResult = type("OptimizerResult", (), {})
    sys.modules["power_sync.optimization.battery_optimizer"] = battery_module

    schedule_module = types.ModuleType("power_sync.optimization.schedule_reader")
    schedule_module.OptimizationSchedule = type("OptimizationSchedule", (), {})
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


def _coordinator(opt_module, provider: str, profit_max: bool = False, **options):
    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    base_options = {"electricity_provider": provider}
    base_options.update(options)
    coordinator._entry = SimpleNamespace(options=base_options, data={})
    coordinator._config = opt_module.OptimizationConfig(
        interval_minutes=5,
        horizon_hours=24,
        profit_max_enabled=profit_max,
    )
    coordinator._saving_session_coordinator = None
    coordinator._last_export_boost_allowed_slots = []
    return coordinator


def _true_indexes(slots: list[bool]) -> list[int]:
    return [idx for idx, value in enumerate(slots) if value]


def test_octopus_profit_max_without_event_blocks_battery_export(opt_module):
    coordinator = _coordinator(opt_module, "octopus", profit_max=True)

    assert coordinator._battery_export_allowed_slots(12, [0.12] * 12) == [False] * 12
    assert coordinator._profit_max_terminal_weight() == 1.0


def test_octopus_joined_saving_session_allows_only_session_slots(opt_module):
    coordinator = _coordinator(opt_module, "octopus", profit_max=True)
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

    slots = coordinator._battery_export_allowed_slots(12, [0.12] * 12)

    assert _true_indexes(slots) == list(range(6, 12))


def test_octopus_free_electricity_does_not_allow_battery_export(opt_module):
    coordinator = _coordinator(opt_module, "octopus", profit_max=True)
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

    assert coordinator._battery_export_allowed_slots(12, [0.12] * 12) == [False] * 12


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
    assert _true_indexes(slots) == list(range(6, 12))
    assert boosted[:6] == export_prices[:6]
    assert all(price > 0.12 for price in boosted[6:])
