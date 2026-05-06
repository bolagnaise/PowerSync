"""Regression tests for provider-scoped battery export permissions."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
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
    const_module.CONF_MONITORING_MODE = "monitoring_mode"
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
    const_module.DISCHARGE_DURATIONS = [5, 10, 15, 30, 45, 60, 75, 90, 105, 120, 150, 180, 210, 240]
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


class _FakeBattery:
    def __init__(self, hardware_mode: str | None = None) -> None:
        self.hardware_mode = hardware_mode
        self.self_consumption_calls = 0
        self.backup_reserve_calls = []
        self.force_charge_calls = []
        self.force_discharge_calls = []

    async def get_tesla_operation_mode(self):
        return self.hardware_mode

    async def set_self_consumption_mode(self):
        self.self_consumption_calls += 1

    async def set_backup_reserve(self, percent):
        self.backup_reserve_calls.append(percent)

    async def force_charge(self, duration_minutes=60, power_w=5000, _extend_hardware=False):
        self.force_charge_calls.append((duration_minutes, power_w, _extend_hardware))

    async def force_discharge(self, duration_minutes=60, power_w=5000, _extend_hardware=False):
        self.force_discharge_calls.append((duration_minutes, power_w, _extend_hardware))


def _execution_coordinator(opt_module, battery: _FakeBattery, soc: float):
    coordinator = _coordinator(opt_module, "octopus")
    coordinator.hass = SimpleNamespace(data={})
    coordinator.entry_id = "entry-1"
    coordinator._entry = SimpleNamespace(options={}, data={})
    coordinator._executor = SimpleNamespace(battery_controller=battery)
    coordinator._force_state_getter = None
    coordinator._force_state_clearer = None
    coordinator._last_executed_action = "self_consumption"
    coordinator._startup_backup_reserve = 20
    coordinator._pre_idle_backup_reserve = None
    coordinator._idle_sc_holdoff = 0
    coordinator._charge_holdoff = 0
    coordinator._last_export_prices = None
    coordinator._offgrid_entry_holdoff = 0
    coordinator.energy_coordinator = None
    coordinator.battery_system = "tesla"
    coordinator._is_in_demand_window = lambda: False
    coordinator._should_block_export_for_demand = lambda: False
    coordinator._minutes_to_demand_start = lambda: None

    async def _battery_state():
        return soc, 13500

    coordinator._get_battery_state = _battery_state
    return coordinator


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


def test_charge_below_reserve_bypasses_charge_hysteresis(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.01)

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="charge", power_w=4200)
        )
    )

    assert battery.force_charge_calls == [(10, 4200, False)]
    assert battery.self_consumption_calls == 0
    assert coordinator._last_executed_action == "charge"


def test_sigenergy_charge_blocks_uneconomic_peak_grid_import(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.14)
    coordinator.battery_system = "sigenergy"
    coordinator._last_executed_action = "charge"
    coordinator._last_display_import_prices = [0.45, 0.32, 0.20, 0.12]
    coordinator._last_display_export_prices = [0.15, 0.15, 0.12, 0.08]

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="charge", power_w=11000)
        )
    )

    assert battery.force_charge_calls == []
    assert battery.self_consumption_calls == 1
    assert coordinator._last_executed_action == "self_consumption"


def test_sigenergy_charge_allows_cheap_grid_import(opt_module):
    battery = _FakeBattery()
    coordinator = _execution_coordinator(opt_module, battery, soc=0.14)
    coordinator.battery_system = "sigenergy"
    coordinator._last_display_import_prices = [0.05, 0.45, 0.40, 0.30]
    coordinator._last_display_export_prices = [0.05, 0.15, 0.15, 0.08]

    asyncio.run(
        coordinator._execute_optimizer_action(
            SimpleNamespace(action="charge", power_w=4200)
        )
    )

    assert battery.force_charge_calls == [(10, 4200, False)]
    assert battery.self_consumption_calls == 0
    assert coordinator._last_executed_action == "charge"


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

    assert battery.force_discharge_calls == [(45, 5000, False)]
    assert coordinator._last_executed_action == "export"
