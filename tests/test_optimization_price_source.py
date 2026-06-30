"""Regression tests for optimizer price source selection."""

from __future__ import annotations

import asyncio
import ast
import importlib
import inspect
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
    "power_sync.flow_power_pricing",
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
    const_module.CONF_EPEX_IMPORT_PRICE_ENTITY = "epex_import_price_entity"
    const_module.CONF_EPEX_EXPORT_PRICE_ENTITY = "epex_export_price_entity"
    const_module.CONF_OPTIMIZATION_AUTO_APPLY_RESERVE = "optimization_auto_apply_reserve"
    const_module.CONF_OPTIMIZATION_BACKUP_RESERVE = "optimization_backup_reserve"
    const_module.CONF_OPTIMIZATION_EV_INTEGRATION = "optimization_ev_integration"
    const_module.CONF_OPTIMIZATION_LOAD_ENTITY = "optimization_load_entity"
    const_module.CONF_OPTIMIZATION_PLANNED_EV_LOAD_ENTITY = "optimization_planned_ev_load_entity"
    const_module.CONF_OPTIMIZATION_MANUAL_RESERVE = "optimization_manual_reserve"
    const_module.DEFAULT_SOLAR_FORECAST_PROVIDER = "solcast"
    const_module.DEFAULT_SOLCAST_ESTIMATE_TYPE = "estimate"
    const_module.SOLAR_FORECAST_PROVIDER_OPEN_METEO = "open_meteo"
    const_module.SOLAR_FORECAST_PROVIDER_SOLCAST = "solcast"
    const_module.SOLAR_FORECAST_PROVIDERS = {
        "solcast": "Solcast",
        "open_meteo": "Open-Meteo",
    }
    const_module.SOLCAST_ESTIMATE = "estimate"
    const_module.SOLCAST_ESTIMATE10 = "estimate10"
    const_module.SOLCAST_ESTIMATE90 = "estimate90"
    const_module.DEFAULT_OPTIMIZATION_INTERVAL = 5
    const_module.supports_no_idle_mode_provider = lambda provider: provider == "flow_power"
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


class FlowPowerKWatchPriceCoordinator(AEMOPriceCoordinator):
    pass


class _State:
    def __init__(
        self,
        entity_id: str,
        state: str,
        unit: str = "kW",
        friendly_name: str | None = None,
        attributes: dict | None = None,
    ) -> None:
        self.entity_id = entity_id
        self.state = state
        self.attributes = {
            "unit_of_measurement": unit,
            "friendly_name": friendly_name or entity_id,
        }
        if attributes:
            self.attributes.update(attributes)


class _States:
    def __init__(self, states: list[_State]) -> None:
        self._states = states
        self._by_id = {state.entity_id: state for state in states}

    def get(self, entity_id: str):
        return self._by_id.get(entity_id)

    def async_all(self, domain: str | None = None):
        if domain is None:
            return list(self._states)
        prefix = f"{domain}."
        return [state for state in self._states if state.entity_id.startswith(prefix)]


class _ConfigEntries:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    def async_update_entry(self, entry, **kwargs) -> None:
        if "data" in kwargs:
            entry.data = kwargs["data"]
        if "options" in kwargs:
            entry.options = kwargs["options"]
        self.updates.append(kwargs)


class _Entry:
    entry_id = "entry-1"

    def __init__(self, data: dict | None = None, options: dict | None = None) -> None:
        self.data = data or {}
        self.options = options or {}


def _planned_ev_load_coordinator(opt_module, states: list[_State]):
    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    coordinator.hass = SimpleNamespace(states=_States(states))
    coordinator.entry_id = "entry-1"
    coordinator._entry = None
    coordinator._config = opt_module.OptimizationConfig(horizon_hours=1)
    coordinator._enabled = False
    coordinator._configured_load_entity_id = None
    coordinator._planned_ev_load_entity_id = "sensor.planned_ev_load"
    coordinator._last_solar_forecast = None
    coordinator._last_solar_nowcast_ratio = None
    coordinator._last_load_forecast = None
    coordinator._last_import_prices = None
    coordinator._last_export_prices = None
    coordinator._last_display_import_prices = None
    coordinator._last_display_export_prices = None
    coordinator._current_schedule = None
    return coordinator


def test_planned_ev_load_window_sensor_maps_to_forecast_slots(opt_module):
    coordinator = _planned_ev_load_coordinator(
        opt_module,
        [
            _State(
                "sensor.planned_ev_load",
                "1",
                attributes={
                    "planned_load": [
                        {
                            "start": "2026-05-03T08:35:00+00:00",
                            "end": "2026-05-03T08:50:00+00:00",
                            "power_kw": 5.75,
                        }
                    ]
                },
            )
        ],
    )

    forecast = coordinator._get_planned_ev_load_forecast(6)

    assert forecast == [0.0, 5750.0, 5750.0, 5750.0, 0.0, 0.0]


def test_planned_ev_load_timestamped_watts_aligns_to_slots(opt_module):
    coordinator = _planned_ev_load_coordinator(
        opt_module,
        [
            _State(
                "sensor.planned_ev_load",
                "1",
                unit="W",
                attributes={
                    "planned_load": {
                        "2026-05-03T08:35:00+00:00": 2000,
                        "2026-05-03T08:45:00+00:00": 0,
                    }
                },
            )
        ],
    )

    forecast = coordinator._get_planned_ev_load_forecast(6)

    assert forecast == [0.0, 2000.0, 2000.0, 0.0, 0.0, 0.0]


def test_planned_ev_load_timestamped_values_align_timezone_offsets(opt_module):
    coordinator = _planned_ev_load_coordinator(
        opt_module,
        [
            _State(
                "sensor.planned_ev_load",
                "1",
                attributes={
                    "planned_load": {
                        "2026-05-03T18:35:00+10:00": 3.5,
                        "2026-05-03T18:45:00+10:00": 0,
                    }
                },
            )
        ],
    )

    forecast = coordinator._get_planned_ev_load_forecast(6)

    assert forecast == [0.0, 3500.0, 3500.0, 0.0, 0.0, 0.0]


def test_planned_ev_load_ignores_invalid_and_past_windows(opt_module):
    coordinator = _planned_ev_load_coordinator(
        opt_module,
        [
            _State(
                "sensor.planned_ev_load",
                "1",
                attributes={
                    "planned_load": [
                        {
                            "start": "2026-05-03T07:00:00+00:00",
                            "end": "2026-05-03T07:30:00+00:00",
                            "power_kw": 7,
                        },
                        {
                            "start": "not-a-time",
                            "end": "2026-05-03T09:00:00+00:00",
                            "power_kw": 7,
                        },
                        {
                            "start": "2026-05-03T08:40:00+00:00",
                            "end": "2026-05-03T08:45:00+00:00",
                            "power_kw": -2,
                        },
                    ]
                },
            )
        ],
    )

    assert coordinator._get_planned_ev_load_forecast(6) is None


def test_planned_ev_load_forecast_data_exposes_debug_fields(opt_module):
    coordinator = _planned_ev_load_coordinator(opt_module, [])
    coordinator._config = opt_module.OptimizationConfig(interval_minutes=5)
    coordinator._last_planned_ev_load_forecast_w = [0.0, 3000.0, 3000.0]
    coordinator._last_solar_nowcast_ratio = None
    coordinator._solar_nowcast_derate = 1.0
    coordinator._last_solar_forecast = []
    coordinator._last_load_forecast = None
    coordinator._last_import_prices = None
    coordinator._last_export_prices = None
    coordinator._last_display_import_prices = None
    coordinator._last_display_export_prices = None
    coordinator._current_schedule = None

    data = coordinator.get_forecast_data()

    assert data["planned_ev_load_forecast_w"] == [0.0, 3000.0, 3000.0]
    assert data["planned_ev_load_peak_kw"] == 3.0
    assert data["planned_ev_load_kwh"] == pytest.approx(0.5)


def test_planned_ev_load_settings_write_and_clear_without_ev_integration(opt_module):
    entry = _Entry(
        data={"optimization_ev_integration": False},
        options={"optimization_ev_integration": False},
    )
    config_entries = _ConfigEntries()
    coordinator = _planned_ev_load_coordinator(opt_module, [])
    coordinator.hass = SimpleNamespace(
        states=_States([]),
        data={"power_sync": {"entry-1": {}}},
        config_entries=config_entries,
    )
    coordinator._entry = entry
    coordinator._planned_ev_load_entity_id = None

    result = asyncio.run(
        coordinator.set_settings(
            {"planned_ev_load_entity": " sensor.node_red_ev_forecast "}
        )
    )

    assert result["success"] is True
    assert coordinator._planned_ev_load_entity_id == "sensor.node_red_ev_forecast"
    assert entry.data["optimization_planned_ev_load_entity"] == "sensor.node_red_ev_forecast"
    assert entry.options["optimization_planned_ev_load_entity"] == "sensor.node_red_ev_forecast"
    assert entry.options["optimization_ev_integration"] is False
    assert coordinator.hass.data["power_sync"]["entry-1"]["_skip_reload"] is True
    assert "planned_ev_load_entity: sensor.node_red_ev_forecast" in result["changes"]

    result = asyncio.run(coordinator.set_settings({"planned_ev_load_entity": ""}))

    assert result["success"] is True
    assert coordinator._planned_ev_load_entity_id is None
    assert entry.data["optimization_planned_ev_load_entity"] is None
    assert entry.options["optimization_planned_ev_load_entity"] is None
    assert "planned_ev_load_entity: cleared" in result["changes"]
    assert len(config_entries.updates) == 2


def test_load_entity_setting_updates_estimator_without_reload(opt_module):
    entry = _Entry(data={}, options={})
    config_entries = _ConfigEntries()
    coordinator = _planned_ev_load_coordinator(
        opt_module,
        [
            _State("sensor.power_sync_home_load", "1.8"),
            _State("sensor.household_power_no_ev", "820", unit="W"),
        ],
    )
    coordinator.hass.data = {"power_sync": {"entry-1": {}}}
    coordinator.hass.config_entries = config_entries
    coordinator._entry = entry
    coordinator._configured_load_entity_id = None
    coordinator._load_estimator = SimpleNamespace(
        load_entity_id="sensor.power_sync_home_load",
        _history_cache={"stale": []},
        _cache_time=object(),
    )

    result = asyncio.run(
        coordinator.set_settings({"load_entity": " sensor.household_power_no_ev "})
    )

    assert result["success"] is True
    assert coordinator._configured_load_entity_id == "sensor.household_power_no_ev"
    assert coordinator._load_estimator.load_entity_id == "sensor.household_power_no_ev"
    assert coordinator._load_estimator._history_cache == {}
    assert coordinator._load_estimator._cache_time is None
    assert entry.data["optimization_load_entity"] == "sensor.household_power_no_ev"
    assert entry.options["optimization_load_entity"] == "sensor.household_power_no_ev"
    assert coordinator.hass.data["power_sync"]["entry-1"]["_skip_reload"] is True
    assert "load_entity: sensor.household_power_no_ev" in result["changes"]
    assert len(config_entries.updates) == 1


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


def _coordinator_with_epex_provider(
    opt_coordinator,
    states: list[_State],
    provider: str = "epex",
):
    def price_entry(start: str, end: str, price: float, channel: str) -> dict:
        return {
            "valid_from": start,
            "valid_to": end,
            "duration": 60,
            "perKwh": price,
            "channelType": channel,
            "type": "CurrentInterval",
        }

    coordinator = object.__new__(opt_coordinator.OptimizationCoordinator)
    coordinator.hass = SimpleNamespace(
        states=_States(states),
        data={"power_sync": {"entry-1": {}}},
    )
    coordinator.entry_id = "entry-1"
    coordinator._entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "electricity_provider": provider,
            "epex_import_price_entity": "sensor.actual_import_price",
            "epex_export_price_entity": "sensor.actual_export_price",
        },
    )
    coordinator._config = opt_coordinator.OptimizationConfig(horizon_hours=1)
    coordinator.price_coordinator = SimpleNamespace(
        data={
            "current": [
                price_entry(
                    "2026-05-03T08:30:00+00:00",
                    "2026-05-03T09:30:00+00:00",
                    24.0,
                    "general",
                ),
                price_entry(
                    "2026-05-03T08:30:00+00:00",
                    "2026-05-03T09:30:00+00:00",
                    -8.0,
                    "feedIn",
                ),
            ],
            "forecast": [],
        }
    )
    coordinator._last_display_import_prices = None
    coordinator._last_display_export_prices = None
    coordinator._apply_export_boost = lambda export, import_prices=None: (export, [])
    coordinator._apply_saving_session_prices = lambda imports, exports: (imports, exports)
    coordinator._apply_chip_mode = lambda exports, *args: exports
    coordinator._apply_demand_charge_penalty = lambda imports: imports
    coordinator._apply_confidence_decay = lambda imports, exports, **kwargs: (imports, exports)
    return coordinator


def test_ev_integration_defaults_to_initial_config_data(opt_module):
    entry = SimpleNamespace(
        data={"optimization_ev_integration": True},
        options={},
    )

    coordinator = opt_module.OptimizationCoordinator(
        hass=SimpleNamespace(),
        entry_id="entry-1",
        battery_system="tesla",
        battery_controller=SimpleNamespace(),
        entry=entry,
    )

    assert coordinator._ev_integration_enabled is True


def test_load_sensor_auto_discovery_skips_generated_forecast_sensor(opt_module):
    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    coordinator._configured_load_entity_id = None
    coordinator.hass = SimpleNamespace(
        states=_States(
            [
                _State(
                    "sensor.powersync_home_load_forecast",
                    "0.0",
                    friendly_name="PowerSync Home Load Forecast",
                ),
                _State("sensor.house_load_power", "0.7"),
            ]
        )
    )

    assert coordinator._get_load_entity_id() == "sensor.house_load_power"


def test_configured_load_sensor_overrides_power_sync_home_load(opt_module):
    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    coordinator._configured_load_entity_id = "sensor.household_power_no_ev"
    coordinator.hass = SimpleNamespace(
        states=_States(
            [
                _State("sensor.power_sync_home_load", "1.8"),
                _State("sensor.household_power_no_ev", "820", unit="W"),
            ]
        )
    )

    assert coordinator._get_load_entity_id() == "sensor.household_power_no_ev"


def test_configured_generated_load_sensor_falls_back_to_auto_discovery(opt_module):
    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    coordinator._configured_load_entity_id = "sensor.household_power_forecast"
    coordinator.hass = SimpleNamespace(
        states=_States(
            [
                _State(
                    "sensor.household_power_forecast",
                    "600",
                    unit="W",
                    friendly_name="Household Power Forecast",
                ),
                _State("sensor.power_sync_home_load", "0.9"),
            ]
        )
    )

    assert coordinator._get_load_entity_id() == "sensor.power_sync_home_load"


def test_load_forecast_rediscovers_load_sensor_after_startup(opt_module):
    class _LoadEstimator:
        def __init__(self) -> None:
            self.load_entity_id = None
            self._history_cache = {"stale": []}
            self._cache_time = object()
            self.requested_horizon = None

        async def get_forecast(self, horizon_hours: int):
            self.requested_horizon = horizon_hours
            return [525.0]

    estimator = _LoadEstimator()
    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    coordinator.hass = SimpleNamespace(
        states=_States([_State("sensor.power_sync_home_load", "0.52")])
    )
    coordinator._configured_load_entity_id = None
    coordinator._load_estimator = estimator
    coordinator._config = SimpleNamespace(horizon_hours=48)

    forecast = asyncio.run(coordinator._get_load_forecast())

    assert forecast == [525.0]
    assert estimator.load_entity_id == "sensor.power_sync_home_load"
    assert estimator._history_cache == {}
    assert estimator._cache_time is None
    assert estimator.requested_horizon == 48


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


def test_epex_export_price_sensor_state_overrides_export_prices(opt_module):
    coordinator = _coordinator_with_epex_provider(
        opt_module,
        [_State("sensor.actual_export_price", "1.3", unit="ct/kWh")],
    )

    import_prices, export_prices = asyncio.run(coordinator._get_price_forecast())

    assert import_prices == [0.24] * 12
    assert export_prices == pytest.approx([0.013] * 12)
    assert coordinator._last_display_export_prices == pytest.approx([0.013] * 12)


def test_epex_import_price_sensor_state_overrides_import_prices(opt_module):
    coordinator = _coordinator_with_epex_provider(
        opt_module,
        [_State("sensor.actual_import_price", "31.5", unit="ct/kWh")],
    )

    import_prices, export_prices = asyncio.run(coordinator._get_price_forecast())

    assert import_prices == pytest.approx([0.315] * 12)
    assert export_prices == [0.08] * 12
    assert coordinator._last_display_import_prices == pytest.approx([0.315] * 12)


def test_epex_import_price_sensor_price_values_override_and_pad(opt_module):
    coordinator = _coordinator_with_epex_provider(
        opt_module,
        [
            _State(
                "sensor.actual_import_price",
                "0",
                unit="ct/kWh",
                attributes={"price_values": [25.0, 30.0, 35.0]},
            )
        ],
    )

    import_prices, _export_prices = asyncio.run(coordinator._get_price_forecast())

    assert import_prices[:3] == [0.25, 0.30, 0.35]
    assert import_prices[3:] == [0.35] * 9
    assert coordinator._last_display_import_prices == import_prices


def test_epex_import_price_sensor_timestamped_price_values_align_to_slots(opt_module):
    coordinator = _coordinator_with_epex_provider(
        opt_module,
        [
            _State(
                "sensor.actual_import_price",
                "0",
                unit="ct/kWh",
                attributes={
                    "price_values": {
                        "2026-05-03T10:00:00+02:00": 20.0,
                        "2026-05-03T11:00:00+02:00": 30.0,
                        "2026-05-03T12:00:00+02:00": 40.0,
                    }
                },
            )
        ],
    )

    import_prices, _export_prices = asyncio.run(coordinator._get_price_forecast())

    assert import_prices[:6] == pytest.approx([0.20] * 6)
    assert import_prices[6:] == pytest.approx([0.30] * 6)
    assert coordinator._last_display_import_prices == pytest.approx(import_prices)


def test_epex_export_price_sensor_price_values_override_and_pad(opt_module):
    coordinator = _coordinator_with_epex_provider(
        opt_module,
        [
            _State(
                "sensor.actual_export_price",
                "0",
                unit="ct/kWh",
                attributes={"price_values": [1.0, 2.0, 3.0]},
            )
        ],
    )

    import_prices, export_prices = asyncio.run(coordinator._get_price_forecast())

    assert import_prices == [0.24] * 12
    assert export_prices[:3] == [0.01, 0.02, 0.03]
    assert export_prices[3:] == [0.03] * 9
    assert coordinator._last_display_export_prices == export_prices


def test_epex_export_price_sensor_timestamped_price_values_align_to_slots(opt_module):
    coordinator = _coordinator_with_epex_provider(
        opt_module,
        [
            _State(
                "sensor.actual_export_price",
                "0",
                unit="ct/kWh",
                attributes={
                    "price_values": {
                        "2026-05-03T10:00:00+02:00": 1.0,
                        "2026-05-03T11:00:00+02:00": 2.0,
                        "2026-05-03T12:00:00+02:00": 3.0,
                    }
                },
            )
        ],
    )

    _import_prices, export_prices = asyncio.run(coordinator._get_price_forecast())

    assert export_prices[:6] == pytest.approx([0.01] * 6)
    assert export_prices[6:] == pytest.approx([0.02] * 6)
    assert coordinator._last_display_export_prices == pytest.approx(export_prices)


def test_epex_export_price_sensor_uses_direct_timestamp_attributes(opt_module):
    coordinator = _coordinator_with_epex_provider(
        opt_module,
        [
            _State(
                "sensor.actual_export_price",
                "0",
                unit="ct/kWh",
                attributes={
                    "2026-05-03T08:00:00+00:00": 1.0,
                    "2026-05-03T09:00:00+00:00": 2.0,
                    "2026-05-03T10:00:00+00:00": 3.0,
                },
            )
        ],
    )

    _import_prices, export_prices = asyncio.run(coordinator._get_price_forecast())

    assert export_prices[:6] == pytest.approx([0.01] * 6)
    assert export_prices[6:] == pytest.approx([0.02] * 6)


def test_epex_export_price_sensor_supports_major_unit(opt_module):
    coordinator = _coordinator_with_epex_provider(
        opt_module,
        [_State("sensor.actual_export_price", "0.021", unit="EUR/kWh")],
    )

    _import_prices, export_prices = asyncio.run(coordinator._get_price_forecast())

    assert export_prices == [0.021] * 12
    assert coordinator._last_display_export_prices == [0.021] * 12


def test_epex_export_price_sensor_missing_falls_back_to_epex_export(opt_module):
    coordinator = _coordinator_with_epex_provider(opt_module, [])

    _import_prices, export_prices = asyncio.run(coordinator._get_price_forecast())

    assert export_prices == [0.08] * 12
    assert coordinator._last_display_export_prices == [0.08] * 12


def test_epex_export_price_sensor_non_numeric_falls_back_to_epex_export(opt_module):
    coordinator = _coordinator_with_epex_provider(
        opt_module,
        [_State("sensor.actual_export_price", "not-a-price", unit="ct/kWh")],
    )

    _import_prices, export_prices = asyncio.run(coordinator._get_price_forecast())

    assert export_prices == [0.08] * 12
    assert coordinator._last_display_export_prices == [0.08] * 12


def test_epex_export_price_sensor_is_ignored_for_other_providers(opt_module):
    coordinator = _coordinator_with_epex_provider(
        opt_module,
        [
            _State("sensor.actual_import_price", "31.5", unit="ct/kWh"),
            _State("sensor.actual_export_price", "1.3", unit="ct/kWh"),
        ],
        provider="octopus",
    )

    _import_prices, export_prices = asyncio.run(coordinator._get_price_forecast())

    assert _import_prices == [0.24] * 12
    assert export_prices == [0.08] * 12
    assert coordinator._last_display_export_prices == [0.08] * 12


def test_static_tou_provider_does_not_attach_dynamic_aemo_listener(opt_module):
    coordinator = _coordinator_with_static_tou_provider(opt_module)

    asyncio.run(coordinator._setup_price_listener())

    assert coordinator._is_dynamic_pricing is False
    assert coordinator.price_coordinator.listener_added is False


def test_flow_power_kwatch_attaches_dynamic_price_listener(opt_module):
    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    coordinator.hass = SimpleNamespace(data={"power_sync": {"entry-1": {}}})
    coordinator.entry_id = "entry-1"
    coordinator._entry = SimpleNamespace(
        data={},
        options={"electricity_provider": "flow_power"},
    )
    coordinator.price_coordinator = FlowPowerKWatchPriceCoordinator()
    coordinator._price_listener_unsub = None
    coordinator._octopus_gate_listener_unsub = None
    coordinator._is_dynamic_pricing = False

    asyncio.run(coordinator._setup_price_listener())

    assert coordinator._is_dynamic_pricing is True
    assert coordinator.price_coordinator.listener_added is True


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
    coordinator._apply_chip_mode = lambda exports, *args: exports
    coordinator._apply_demand_charge_penalty = lambda imports: imports
    monkeypatch.setattr(
        opt_module,
        "_flow_power_network_tariff_rate",
        lambda when, network, tariff_code: 12.0,
    )

    import_prices, _ = asyncio.run(coordinator._get_price_forecast())

    # Base 34c + PEA (1.1*20c + 12c - 1.1*8c - 1.7c) = 57.5c/kWh.
    assert import_prices[0] == pytest.approx(0.575)
    assert coordinator._last_display_import_prices[0] == pytest.approx(0.575)


def test_flow_power_optimizer_uses_base_rate_from_entry_data(opt_module, monkeypatch):
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
        data={
            "electricity_provider": "flow_power",
            "flow_power_base_rate": 35.93,
            "flow_power_state": "NSW1",
            "fp_network": "Ausgrid",
            "fp_tariff_code": "EA025",
        },
        options={},
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
    coordinator._apply_chip_mode = lambda exports, *args: exports
    coordinator._apply_demand_charge_penalty = lambda imports: imports
    monkeypatch.setattr(
        opt_module,
        "_flow_power_network_tariff_rate",
        lambda when, network, tariff_code: 12.0,
    )

    import_prices, _ = asyncio.run(coordinator._get_price_forecast())

    # Base 35.93c + PEA (1.1*20c + 12c - 1.1*8c - 1.7c) = 59.43c/kWh.
    assert import_prices[0] == pytest.approx(0.5943)
    assert coordinator._last_display_import_prices[0] == pytest.approx(0.5943)


def test_flow_power_optimizer_uses_portal_twap_with_portal_pricing_inputs(opt_module, monkeypatch):
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
                    "flow_power_portal_data": {
                        "twap": 10.0,
                        "bpea": 2.0,
                        "gst_multiplier": 1.2,
                    },
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
    coordinator._apply_chip_mode = lambda exports, *args: exports
    coordinator._apply_demand_charge_penalty = lambda imports: imports
    monkeypatch.setattr(
        opt_module,
        "_flow_power_network_tariff_rate",
        lambda when, network, tariff_code: 12.0,
    )

    import_prices, _ = asyncio.run(coordinator._get_price_forecast())

    # Base 34c + PEA (1.2*20c + 12c - 1.2*10c - 2c) = 56c/kWh.
    assert import_prices[0] == pytest.approx(0.56)
    assert coordinator._last_display_import_prices[0] == pytest.approx(0.56)


def test_flow_power_optimizer_uses_current_interval_for_active_tariff_slot(
    opt_module,
    monkeypatch,
):
    nem_tz = opt_module.timezone(opt_module.timedelta(hours=10))
    now = datetime(2026, 6, 23, 21, 5, tzinfo=nem_tz)
    monkeypatch.setattr(opt_module.dt_util, "now", lambda *args, **kwargs: now)

    async def _executor(fn, *args):
        return fn(*args)

    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    coordinator.hass = SimpleNamespace(
        async_add_executor_job=_executor,
        data={
            "power_sync": {
                "entry-1": {
                    "fp_avg_daily_tariff": 5.0,
                    "flow_power_portal_data": {
                        "twap": 10.0,
                        "bpea": 2.0,
                        "gst_multiplier": 1.2,
                    },
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
    coordinator._last_display_import_prices = None
    coordinator._last_display_export_prices = None
    coordinator._apply_export_boost = lambda export, import_prices=None: (export, [])
    coordinator._apply_saving_session_prices = lambda imports, exports: (imports, exports)
    coordinator._apply_chip_mode = lambda exports, *args: exports
    coordinator._apply_demand_charge_penalty = lambda imports: imports
    coordinator._apply_confidence_decay = lambda imports, exports, **kwargs: (
        imports,
        exports,
    )
    monkeypatch.setattr(
        opt_module,
        "_flow_power_network_tariff_rate",
        lambda when, network, tariff_code: 12.0,
    )

    def price_entry(end: datetime, import_cents: float, channel: str, duration: int) -> dict:
        return {
            "nemTime": end.isoformat(),
            "duration": duration,
            "perKwh": -1.0 if channel == "feedIn" else import_cents,
            "wholesaleKWHPrice": import_cents,
            "channelType": channel,
            "type": "CurrentInterval" if duration == 5 else "ForecastInterval",
        }

    current_end = datetime(2026, 6, 23, 21, 5, tzinfo=nem_tz)
    forecast_end = datetime(2026, 6, 23, 21, 30, tzinfo=nem_tz)
    coordinator.price_coordinator = SimpleNamespace(
        data={
            "current": [
                price_entry(current_end, 20.0, "general", 5),
                price_entry(current_end, 20.0, "feedIn", 5),
            ],
            "forecast": [
                price_entry(forecast_end, 60.0, "general", 30),
                price_entry(forecast_end, 60.0, "feedIn", 30),
            ],
        }
    )

    import_prices, _ = asyncio.run(coordinator._get_price_forecast())

    # The forecast for the active half-hour would produce 99c/kWh. The live
    # KWatch current interval must override that active tariff slot, matching
    # the canonical Flow Power tariff schedule/current-price sensor.
    assert import_prices[0] == pytest.approx(0.56)
    assert coordinator._last_display_import_prices[0] == pytest.approx(0.56)


def test_flow_power_decays_far_future_import_spikes_but_keeps_happy_hour_export(
    opt_module,
    monkeypatch,
):
    now = datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(opt_module.dt_util, "now", lambda *args, **kwargs: now)

    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    coordinator.hass = SimpleNamespace(data={"power_sync": {"entry-1": {}}})
    coordinator._entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "electricity_provider": "flow_power",
            "flow_power_state": "NSW1",
        },
    )
    coordinator._config = opt_module.OptimizationConfig(
        horizon_hours=24,
        interval_minutes=5,
        profit_max_enabled=True,
    )
    coordinator._last_display_import_prices = None
    coordinator._last_display_export_prices = None
    coordinator._apply_export_boost = lambda export, import_prices=None: (export, [])
    coordinator._apply_saving_session_prices = lambda imports, exports: (imports, exports)
    coordinator._apply_chip_mode = lambda exports, *args: exports
    coordinator._apply_demand_charge_penalty = lambda imports: imports

    def price_entry(start: datetime, import_cents: float, channel: str) -> dict:
        end = start + opt_module.timedelta(minutes=30)
        return {
            "valid_from": start.isoformat(),
            "valid_to": end.isoformat(),
            "nemTime": end.isoformat(),
            "duration": 30,
            "perKwh": -1.0 if channel == "feedIn" else import_cents,
            "channelType": channel,
            "type": "ForecastInterval",
        }

    forecast = []
    spike_start = now + opt_module.timedelta(hours=20)
    for slot in range(48):
        start = now + opt_module.timedelta(minutes=30 * slot)
        import_cents = 2000.0 if start == spike_start else 20.0
        forecast.append(price_entry(start, import_cents, "general"))
        forecast.append(price_entry(start, import_cents, "feedIn"))

    coordinator.price_coordinator = SimpleNamespace(
        data={"current": [], "forecast": forecast}
    )

    import_prices, export_prices = asyncio.run(coordinator._get_price_forecast())

    spike_slot = int((spike_start - now).total_seconds() // (5 * 60))
    happy_hour_slot = int(
        (
            now.replace(hour=17, minute=30)
            - now
        ).total_seconds()
        // (5 * 60)
    )

    # UI/display data preserves the raw forecast-derived price, but LP input
    # decays the far-future speculative import spike toward the median.
    assert coordinator._last_display_import_prices[spike_slot] == pytest.approx(
        20.243
    )
    assert import_prices[spike_slot] < coordinator._last_display_import_prices[
        spike_slot
    ]
    assert import_prices[spike_slot] > 0.443

    # Happy Hour export is contractual and must not decay with speculative import
    # forecasts, otherwise Profit Max can miss a valid fixed export window.
    assert export_prices[happy_hour_slot] == pytest.approx(0.45)


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
    coordinator._apply_chip_mode = lambda exports, *args: exports
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


def test_schedule_polling_executes_cached_action_before_reoptimizing(opt_module):
    source = inspect.getsource(opt_module.OptimizationCoordinator._schedule_polling_loop)

    cached_action_call = "await self._execute_cached_current_action_if_changed()"
    optimization_call = "await self._run_optimization()"

    assert cached_action_call in source
    assert source.index(cached_action_call) < source.index(optimization_call)


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


def test_flow_power_kwatch_price_source_is_provider_gated():
    source = (COMPONENT_ROOT / "__init__.py").read_text()

    assert 'flow_power_price_source == "kwatch"' in source
    assert "FlowPowerKWatchPriceCoordinator" in source
    assert '"flow_power_kwatch_coordinator": flow_power_kwatch_coordinator' in source
    assert "or flow_power_kwatch_coordinator" in source
