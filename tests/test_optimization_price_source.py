"""Regression tests for optimizer price source selection."""

from __future__ import annotations

import asyncio
import ast
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
    "homeassistant.helpers.event",
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
    ha_event = types.ModuleType("homeassistant.helpers.event")
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
    ha_event.async_track_time_change = lambda *args, **kwargs: (lambda: None)
    ha_event.async_call_later = lambda *args, **kwargs: (lambda: None)
    ha_dt.now = lambda *args, **kwargs: datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    ha_dt.utcnow = lambda *args, **kwargs: datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    ha_dt.UTC = timezone.utc
    ha_helpers.storage = ha_storage
    ha_helpers.update_coordinator = ha_update
    ha_helpers.event = ha_event
    ha_util.dt = ha_dt
    ha_root.helpers = ha_helpers
    ha_root.util = ha_util

    sys.modules["homeassistant"] = ha_root
    sys.modules["homeassistant.core"] = ha_core
    sys.modules["homeassistant.exceptions"] = ha_exceptions
    sys.modules["homeassistant.helpers"] = ha_helpers
    sys.modules["homeassistant.helpers.storage"] = ha_storage
    sys.modules["homeassistant.helpers.update_coordinator"] = ha_update
    sys.modules["homeassistant.helpers.event"] = ha_event
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
    const_module.CONF_FLOW_POWER_BASE_RATE = "flow_power_base_rate"
    const_module.CONF_FLOW_POWER_EXPORT_RATE = "flow_power_export_rate"
    const_module.CONF_FLOW_POWER_STATE = "flow_power_state"
    const_module.CONF_FP_NETWORK = "fp_network"
    const_module.CONF_FP_TARIFF_CODE = "fp_tariff_code"
    const_module.CONF_FP_TWAP_OVERRIDE = "fp_twap_override"
    const_module.CONF_PEA_CUSTOM_VALUE = "pea_custom_value"
    const_module.CONF_PEA_ENABLED = "pea_enabled"
    const_module.CONF_SPIKE_PROTECTION_ENABLED = "spike_protection_enabled"
    const_module.CONF_DEMAND_CHARGE_ENABLED = "demand_charge_enabled"
    const_module.CONF_DEMAND_CHARGE_RATE = "demand_charge_rate"
    const_module.CONF_DEMAND_CHARGE_START_TIME = "demand_charge_start_time"
    const_module.CONF_DEMAND_CHARGE_END_TIME = "demand_charge_end_time"
    const_module.CONF_DEMAND_CHARGE_DAYS = "demand_charge_days"
    const_module.HAFO_DOMAIN = "hafo"
    const_module.HAFO_LOAD_SENSOR_PREFIX = "hafo_"
    const_module.DEFAULT_SOLCAST_ESTIMATE_TYPE = "estimate"
    const_module.SOLCAST_ESTIMATE = "estimate"
    const_module.SOLCAST_ESTIMATE10 = "estimate10"
    const_module.SOLCAST_ESTIMATE90 = "estimate90"
    const_module.DEFAULT_OPTIMIZATION_INTERVAL = 5
    const_module.FLOW_POWER_BENCHMARK = 1.7
    const_module.FLOW_POWER_DEFAULT_BASE_RATE = 34.0
    const_module.FLOW_POWER_EXPORT_RATES = {"NSW1": 0.45}
    const_module.FLOW_POWER_GST = 1.1
    const_module.FLOW_POWER_MARKET_AVG = 8.0
    const_module.NETWORK_API_NAME = {"Ausgrid": "ausgrid"}
    sys.modules["power_sync.const"] = const_module

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


class AEMOPriceCoordinator:
    def __init__(self) -> None:
        self.data = {
            "current": [
                {
                    "channelType": "general",
                    "perKwh": 1.0,
                    "nemTime": "2026-05-03T08:30:00+00:00",
                    "duration": 30,
                },
                {
                    "channelType": "feedIn",
                    "perKwh": -1.0,
                    "nemTime": "2026-05-03T08:30:00+00:00",
                    "duration": 30,
                },
            ],
            "forecast": [],
        }
        self.listener_added = False

    def async_add_listener(self, callback):
        self.listener_added = True
        return lambda: None


def _tariff_schedule() -> dict:
    return {
        "plan_name": "ZEROHERO",
        "tou_periods": {
            "SHOULDER": [
                {
                    "fromDayOfWeek": 0,
                    "toDayOfWeek": 6,
                    "fromHour": 0,
                    "toHour": 24,
                }
            ]
        },
        "buy_rates": {"SHOULDER": 0.33},
        "sell_rates": {"SHOULDER": 0.0},
    }


def _fresh_tariff_schedule() -> dict:
    return {
        "plan_name": "FOUR4FREE",
        "last_sync": "2026-05-03 08:31:00",
        "tou_periods": {
            "ON_PEAK": [
                {
                    "fromDayOfWeek": 0,
                    "toDayOfWeek": 6,
                    "fromHour": 0,
                    "toHour": 24,
                }
            ]
        },
        "buy_rates": {"ON_PEAK": 0.51},
        "sell_rates": {"ON_PEAK": 0.10},
    }


def _nested_free_tariff_schedule() -> dict:
    return {
        "plan_name": "FOUR4FREE",
        "tou_periods": {
            "PARTIAL_PEAK": {
                "periods": [
                    {
                        "fromDayOfWeek": 0,
                        "toDayOfWeek": 6,
                        "fromHour": 0,
                        "toHour": 24,
                    }
                ]
            },
            "WINDOW_2": {
                "periods": [
                    {
                        "fromDayOfWeek": 0,
                        "toDayOfWeek": 6,
                        "fromHour": 10,
                        "toHour": 14,
                    }
                ]
            },
        },
        "buy_rates": {"PARTIAL_PEAK": 0.31, "WINDOW_2": 0.0},
        "sell_rates": {"PARTIAL_PEAK": 0.0, "WINDOW_2": 0.0},
    }


def _coordinator_with_static_tou_provider(opt_coordinator):
    coordinator = object.__new__(opt_coordinator.OptimizationCoordinator)
    coordinator.hass = SimpleNamespace(data={"power_sync": {"entry-1": {}}})
    coordinator.entry_id = "entry-1"
    coordinator._entry = SimpleNamespace(
        data={},
        options={"electricity_provider": "globird"},
    )
    coordinator._tariff_schedule = _tariff_schedule()
    coordinator.price_coordinator = AEMOPriceCoordinator()
    coordinator._config = opt_coordinator.OptimizationConfig(horizon_hours=1)
    coordinator._saving_session_coordinator = None
    coordinator._is_dynamic_pricing = False
    coordinator._price_listener_unsub = None
    coordinator._octopus_gate_listener_unsub = None
    coordinator._last_display_import_prices = None
    coordinator._last_display_export_prices = None
    return coordinator


def test_static_tou_provider_uses_tariff_even_when_aemo_data_exists(opt_module):
    coordinator = _coordinator_with_static_tou_provider(opt_module)

    import_prices, export_prices = asyncio.run(coordinator._get_price_forecast())

    assert import_prices == [0.33] * 12
    assert export_prices == [0.0] * 12
    assert coordinator._last_display_import_prices == [0.33] * 12
    assert coordinator._last_display_export_prices == [0.0] * 12


def test_aemo_vpp_uses_tariff_schedule_for_normal_lp_prices(opt_module):
    coordinator = _coordinator_with_static_tou_provider(opt_module)
    coordinator._entry.options = {"electricity_provider": "aemo_vpp"}

    import_prices, export_prices = asyncio.run(coordinator._get_price_forecast())

    assert import_prices == [0.33] * 12
    assert export_prices == [0.0] * 12
    assert coordinator.price_coordinator.data is not None


def test_static_tou_provider_refreshes_stale_constructor_tariff(opt_module):
    """The tariff API can refresh hass.data after coordinator construction.

    The optimizer must use that shared schedule instead of a stale constructor
    copy, otherwise the LP can keep seeing a flat fallback tariff indefinitely.
    """
    coordinator = _coordinator_with_static_tou_provider(opt_module)
    fresh_tariff = _fresh_tariff_schedule()
    coordinator.hass.data["power_sync"]["entry-1"]["tariff_schedule"] = fresh_tariff

    import_prices, export_prices = asyncio.run(coordinator._get_price_forecast())

    assert import_prices == [0.51] * 12
    assert export_prices == [0.10] * 12
    assert coordinator._last_display_import_prices == [0.51] * 12
    assert coordinator._last_display_export_prices == [0.10] * 12
    assert coordinator._tariff_schedule is fresh_tariff


def test_static_tou_provider_matches_nested_tesla_periods(opt_module, monkeypatch):
    """Tesla tariff fetch can store periods as {"periods": [...]} mappings.

    The optimizer must match that same shape as the tariff sensor, otherwise
    free import windows fall back to the shoulder rate and stop showing charge.
    The free period name is arbitrary because Tesla behavior is rate-based.
    """
    coordinator = _coordinator_with_static_tou_provider(opt_module)
    coordinator._tariff_schedule = _nested_free_tariff_schedule()
    monkeypatch.setattr(
        opt_module.dt_util,
        "now",
        lambda *args, **kwargs: datetime(2026, 5, 10, 11, 5, tzinfo=timezone.utc),
    )

    import_prices, export_prices = asyncio.run(coordinator._get_price_forecast())

    assert import_prices == [1e-6] * 12
    assert export_prices == [0.0] * 12
    assert coordinator._last_display_import_prices == [0.0] * 12
    assert coordinator._last_display_export_prices == [0.0] * 12


def test_static_tou_provider_returns_none_when_tariff_missing(opt_module):
    """A static-TOU provider with no cached tariff must NOT fall through to a
    leftover AEMO coordinator's data. Returning None forces the LP to skip
    rather than silently optimize on stale wholesale prices."""
    coordinator = _coordinator_with_static_tou_provider(opt_module)
    coordinator._tariff_schedule = None  # not yet cached anywhere

    result = asyncio.run(coordinator._get_price_forecast())

    assert result is None
    # And the AEMO coordinator's data is present but explicitly ignored.
    assert coordinator.price_coordinator.data is not None


def test_static_tou_provider_does_not_attach_dynamic_aemo_listener(opt_module):
    coordinator = _coordinator_with_static_tou_provider(opt_module)

    asyncio.run(coordinator._setup_price_listener())

    assert coordinator._is_dynamic_pricing is False
    assert coordinator.price_coordinator.listener_added is False


def test_flow_power_optimizer_uses_v2_pea_formula(opt_module, monkeypatch):
    async def _executor(fn, *args):
        return fn(*args)

    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    coordinator.hass = SimpleNamespace(
        async_add_executor_job=_executor,
        data={
            "power_sync": {
                "entry-1": {
                    "fp_avg_daily_tariff": 5.0,
                    "flow_power_twap_tracker": SimpleNamespace(twap=8.0),
                }
            }
        },
    )
    coordinator._entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "electricity_provider": "flow_power",
            "flow_power_state": "NSW1",
            "fp_network": "Ausgrid",
            "fp_tariff_code": "EA025",
        },
    )
    coordinator._config = opt_module.OptimizationConfig(horizon_hours=1)
    coordinator.price_coordinator = SimpleNamespace(
        data={
            "current": [
                {
                    "channelType": "general",
                    "wholesaleKWHPrice": 20.0,
                    "perKwh": 20.0,
                    "nemTime": "2026-05-03T08:35:00+00:00",
                    "duration": 5,
                },
                {
                    "channelType": "feedIn",
                    "perKwh": -1.0,
                    "nemTime": "2026-05-03T08:35:00+00:00",
                    "duration": 5,
                },
            ],
            "forecast": [],
        }
    )
    coordinator._last_display_import_prices = None
    coordinator._last_display_export_prices = None
    coordinator._apply_export_boost = lambda export, import_prices=None: (export, [])
    coordinator._apply_saving_session_prices = lambda imports, exports: (imports, exports)
    coordinator._apply_chip_mode = lambda exports: exports
    coordinator._apply_demand_charge_penalty = lambda imports: imports
    monkeypatch.setattr(
        opt_module,
        "_flow_power_network_tariff_rate",
        lambda when, network, tariff_code: 12.0,
    )

    import_prices, _ = asyncio.run(coordinator._get_price_forecast())

    # Base 34c + PEA (1.1*20c + 12c - 1.1*8c - 5c - 1.7c) = 52.5c/kWh.
    assert import_prices[0] == pytest.approx(0.525)
    assert coordinator._last_display_import_prices[0] == pytest.approx(0.525)


def test_dynamic_price_forecast_preserves_boundaries_after_leading_gap(
    opt_module,
    monkeypatch,
):
    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    coordinator.hass = SimpleNamespace(data={"power_sync": {"entry-1": {}}})
    coordinator.entry_id = "entry-1"
    coordinator._entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={"electricity_provider": "octopus"},
    )
    coordinator._config = opt_module.OptimizationConfig(
        horizon_hours=10,
        interval_minutes=5,
    )
    coordinator._last_display_import_prices = None
    coordinator._last_display_export_prices = None
    coordinator._apply_export_boost = lambda export, import_prices=None: (export, [])
    coordinator._apply_saving_session_prices = lambda imports, exports: (imports, exports)
    coordinator._apply_chip_mode = lambda exports: exports
    coordinator._apply_demand_charge_penalty = lambda imports: imports
    coordinator._apply_confidence_decay = lambda imports, exports, **kwargs: (imports, exports)
    monkeypatch.setattr(
        opt_module.dt_util,
        "now",
        lambda *args, **kwargs: datetime(2026, 5, 23, 21, 5, tzinfo=timezone.utc),
    )

    def price_entry(start: str, end: str, price: float, channel: str) -> dict:
        return {
            "valid_from": start,
            "valid_to": end,
            "nemTime": end,
            "duration": 30,
            "perKwh": -price if channel == "feedIn" else price,
            "channelType": channel,
            "type": "ForecastInterval",
        }

    forecast = []
    for start, end, import_price in (
        ("2026-05-23T21:30:00+00:00", "2026-05-23T22:00:00+00:00", 6.9),
        ("2026-05-24T04:30:00+00:00", "2026-05-24T05:00:00+00:00", 6.9),
        ("2026-05-24T05:00:00+00:00", "2026-05-24T05:30:00+00:00", 6.9),
        ("2026-05-24T05:30:00+00:00", "2026-05-24T06:00:00+00:00", 28.56),
    ):
        forecast.append(price_entry(start, end, import_price, "general"))
        forecast.append(price_entry(start, end, 12.0, "feedIn"))
    coordinator.price_coordinator = SimpleNamespace(
        data={"current": [], "forecast": forecast}
    )

    import_prices, export_prices = asyncio.run(coordinator._get_price_forecast())

    # There is no 21:05-21:30 current interval in this fixture. The optimizer
    # must fill that leading gap without shifting the real 05:30 boundary to
    # 05:55.
    high_price_slot = int(
        (
            datetime(2026, 5, 24, 5, 30, tzinfo=timezone.utc)
            - datetime(2026, 5, 23, 21, 5, tzinfo=timezone.utc)
        ).total_seconds()
        // (5 * 60)
    )
    assert coordinator._last_display_import_prices[high_price_slot - 1] == pytest.approx(0.069)
    assert coordinator._last_display_import_prices[high_price_slot] == pytest.approx(0.2856)
    assert import_prices[high_price_slot] == pytest.approx(0.2856)
    assert export_prices[high_price_slot] == pytest.approx(0.12)


def test_schedule_polling_sleep_aligns_to_next_interval_boundary(opt_module, monkeypatch):
    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    coordinator._config = opt_module.OptimizationConfig(interval_minutes=5)

    monkeypatch.setattr(
        opt_module.dt_util,
        "now",
        lambda *args, **kwargs: datetime(2026, 5, 8, 17, 29, 50, tzinfo=timezone.utc),
    )
    assert coordinator._seconds_until_next_interval() == 10

    monkeypatch.setattr(
        opt_module.dt_util,
        "now",
        lambda *args, **kwargs: datetime(2026, 5, 8, 17, 30, 0, tzinfo=timezone.utc),
    )
    assert coordinator._seconds_until_next_interval() == 300

    monkeypatch.setattr(
        opt_module.dt_util,
        "now",
        lambda *args, **kwargs: datetime(2026, 5, 8, 17, 32, 30, tzinfo=timezone.utc),
    )
    assert coordinator._seconds_until_next_interval() == 150


def test_flow_power_aemo_price_source_is_provider_gated():
    tree = ast.parse((COMPONENT_ROOT / "__init__.py").read_text())
    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "use_aemo_pricing" for target in node.targets)
    ]

    assert assignments
    assignment_source = ast.get_source_segment(
        (COMPONENT_ROOT / "__init__.py").read_text(),
        assignments[0],
    )
    assert 'electricity_provider == "flow_power"' in assignment_source
    assert 'flow_power_price_source in ("aemo_sensor", "aemo")' in assignment_source
