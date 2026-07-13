"""Regression coverage for shared per-vehicle EV capacity planning."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"
INIT_PATH = ROOT / "__init__.py"

sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))
_ha_root = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
_ha_core = sys.modules.setdefault("homeassistant.core", types.ModuleType("homeassistant.core"))
_ha_config_entries = sys.modules.setdefault(
    "homeassistant.config_entries", types.ModuleType("homeassistant.config_entries")
)
_ha_exceptions = sys.modules.setdefault(
    "homeassistant.exceptions", types.ModuleType("homeassistant.exceptions")
)
_ha_helpers = sys.modules.setdefault("homeassistant.helpers", types.ModuleType("homeassistant.helpers"))
_ha_storage = sys.modules.setdefault(
    "homeassistant.helpers.storage", types.ModuleType("homeassistant.helpers.storage")
)
_ha_update = sys.modules.setdefault(
    "homeassistant.helpers.update_coordinator",
    types.ModuleType("homeassistant.helpers.update_coordinator"),
)
_ha_event = sys.modules.setdefault(
    "homeassistant.helpers.event", types.ModuleType("homeassistant.helpers.event")
)
_ha_util = sys.modules.setdefault("homeassistant.util", types.ModuleType("homeassistant.util"))
_ha_dt = sys.modules.setdefault("homeassistant.util.dt", types.ModuleType("homeassistant.util.dt"))
_ha_core.HomeAssistant = type("HomeAssistant", (), {})
_ha_config_entries.ConfigEntry = type("ConfigEntry", (), {})
_ha_exceptions.ConfigEntryNotReady = type("ConfigEntryNotReady", (Exception,), {})
_ha_storage.Store = type("Store", (), {"__init__": lambda self, *args, **kwargs: None})
_ha_update.DataUpdateCoordinator = type(
    "DataUpdateCoordinator",
    (),
    {
        "__class_getitem__": classmethod(lambda cls, item: cls),
        "__init__": lambda self, *args, **kwargs: None,
    },
)
_ha_event.async_track_time_change = lambda *args, **kwargs: (lambda: None)
_ha_helpers.storage = _ha_storage
_ha_helpers.update_coordinator = _ha_update
_ha_helpers.event = _ha_event
_ha_dt.now = getattr(_ha_dt, "now", lambda *args, **kwargs: None)
_ha_util.dt = _ha_dt
_ha_root.helpers = _ha_helpers
_ha_root.util = _ha_util

_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps
_automations = types.ModuleType("power_sync.automations")
_automations.__path__ = [str(ROOT / "automations")]
sys.modules["power_sync.automations"] = _automations
sys.modules.pop("power_sync.const", None)

ev_planner = importlib.import_module("power_sync.automations.ev_charging_planner")
capacity = importlib.import_module("power_sync.automations.ev_vehicle_capacity")

AutoScheduleExecutor = ev_planner.AutoScheduleExecutor
AutoScheduleSettings = ev_planner.AutoScheduleSettings
ChargingPlan = ev_planner.ChargingPlan
ChargingPlanner = ev_planner.ChargingPlanner
ChargingPriority = ev_planner.ChargingPriority
PriceForecast = ev_planner.PriceForecast
SurplusForecast = ev_planner.SurplusForecast

CAPACITY_SOURCE_CHARGER_FALLBACK = capacity.CAPACITY_SOURCE_CHARGER_FALLBACK
CAPACITY_SOURCE_DEFAULT_ESTIMATE = capacity.CAPACITY_SOURCE_DEFAULT_ESTIMATE
CAPACITY_SOURCE_MANUAL = capacity.CAPACITY_SOURCE_MANUAL
CAPACITY_SOURCE_MODEL_ESTIMATE = capacity.CAPACITY_SOURCE_MODEL_ESTIMATE
CAPACITY_SOURCE_PROVIDER = capacity.CAPACITY_SOURCE_PROVIDER
ResolvedEVBatteryCapacity = capacity.ResolvedEVBatteryCapacity
canonical_vehicle_id = capacity.canonical_vehicle_id
resolve_ev_battery_capacity = capacity.resolve_ev_battery_capacity
validate_ev_battery_capacity = capacity.validate_ev_battery_capacity
vehicle_ids_match = capacity.vehicle_ids_match


@pytest.mark.parametrize(
    ("kwargs", "capacity", "source", "manual"),
    [
        (
            {
                "manual_capacity_kwh": 29.6,
                "charger_fallback_capacity_kwh": 45,
                "provider_capacity_kwh": 50,
                "model": "Tesla Model Y Long Range",
                "anonymous_loadpoint": True,
            },
            29.6,
            CAPACITY_SOURCE_MANUAL,
            29.6,
        ),
        (
            {
                "charger_fallback_capacity_kwh": 45.5,
                "provider_capacity_kwh": 50,
                "anonymous_loadpoint": True,
            },
            45.5,
            CAPACITY_SOURCE_CHARGER_FALLBACK,
            None,
        ),
        (
            {
                "charger_fallback_capacity_kwh": 45.5,
                "provider_capacity_kwh": 50,
                "anonymous_loadpoint": False,
            },
            50,
            CAPACITY_SOURCE_PROVIDER,
            None,
        ),
        (
            {"model": "Tesla Model 3", "trim": "Long Range"},
            82,
            CAPACITY_SOURCE_MODEL_ESTIMATE,
            None,
        ),
        (
            {"model": "Tesla Model 3"},
            60,
            CAPACITY_SOURCE_DEFAULT_ESTIMATE,
            None,
        ),
    ],
)
def test_capacity_resolution_precedence(kwargs, capacity, source, manual):
    resolved = resolve_ev_battery_capacity(**kwargs)

    assert resolved.effective_battery_capacity_kwh == capacity
    assert resolved.battery_capacity_source == source
    assert resolved.battery_capacity_kwh == manual


@pytest.mark.parametrize(
    "value",
    [0, 0.99, 250.01, float("nan"), float("inf"), -float("inf"), True, "bad"],
)
def test_capacity_validation_rejects_invalid_and_non_finite_values(value):
    with pytest.raises(ValueError):
        validate_ev_battery_capacity(value)


def test_capacity_validation_accepts_decimals_and_null_clear():
    assert validate_ev_battery_capacity("29.6") == 29.6
    assert validate_ev_battery_capacity(None) is None


def test_stable_vehicle_aliases_do_not_duplicate_ble_or_vin_profiles():
    assert canonical_vehicle_id("5YJ3E1EA7KF000001") == "5YJ3E1EA7KF000001"
    assert vehicle_ids_match("ble_My_Model_3", "my_model_3")
    assert vehicle_ids_match("ble_My_Model_3", "BLE_my_model_3")
    assert vehicle_ids_match("byd_DEVICE-123", "BYD_device-123")
    assert not vehicle_ids_match("ble_car_one", "ble_car_two")


def test_vehicle_capacity_api_regenerates_without_charger_command_and_refreshes_optimizer():
    """Capacity writes rebuild matching plans and only schedule optimizer work."""
    source = INIT_PATH.read_text()
    start = source.index("class VehicleChargingConfigView")
    end = source.index("class SolarSurplusConfigView", start)
    view_source = source[start:end]

    assert "validate_ev_battery_capacity" in view_source
    assert "vehicle_ids_match(config.get(\"vehicle_id\"), vehicle_id)" in view_source
    assert "await executor._regenerate_plan(" in view_source
    assert "_schedule_settings_reoptimization" in view_source
    assert "_start_charging(" not in view_source
    assert "_stop_charging(" not in view_source


class _SurplusForecaster:
    async def forecast_surplus(self, hours):
        now = datetime.now().replace(minute=0, second=0, microsecond=0)
        return [
            SurplusForecast(
                hour=(now + timedelta(hours=offset)).isoformat(),
                solar_kw=100,
                load_kw=0,
                surplus_kw=100,
                confidence=1,
            )
            for offset in range(hours)
        ]


class _PriceForecaster:
    async def get_price_forecast(self, hours):
        now = datetime.now().replace(minute=0, second=0, microsecond=0)
        return [
            PriceForecast(
                hour=(now + timedelta(hours=offset)).isoformat(),
                import_cents=20,
                export_cents=5,
                period="offpeak",
            )
            for offset in range(hours)
        ]


def _planner() -> ChargingPlanner:
    planner = object.__new__(ChargingPlanner)
    planner.surplus_forecaster = _SurplusForecaster()
    planner.price_forecaster = _PriceForecaster()
    planner._get_battery_schedule = None
    planner._grid_capacity_kw = 100
    return planner


def test_byd_29_6_kwh_energy_uses_explicit_capacity_not_60_kwh_fallback():
    plan = asyncio.run(
        _planner().plan_charging(
            vehicle_id="byd_device-123",
            current_soc=50,
            target_soc=80,
            target_time=None,
            resolved_capacity=resolve_ev_battery_capacity(
                provider_capacity_kwh=29.6
            ),
            charger_power_kw=100,
            priority=ChargingPriority.SOLAR_ONLY,
        )
    )

    assert plan.energy_needed_kwh == pytest.approx(9.866666, rel=1e-5)
    assert plan.to_dict()["energy_needed_kwh"] == 9.87
    assert plan.effective_battery_capacity_kwh == 29.6
    assert plan.battery_capacity_source == CAPACITY_SOURCE_PROVIDER


def test_two_identified_vehicles_keep_independent_capacity_and_energy():
    planner = _planner()
    async def build_plans():
        return await asyncio.gather(
            planner.plan_charging(
                vehicle_id="VIN_ONE_123456789",
                current_soc=50,
                target_soc=80,
                target_time=None,
                resolved_capacity=resolve_ev_battery_capacity(
                    manual_capacity_kwh=29.6
                ),
                charger_power_kw=100,
                priority=ChargingPriority.SOLAR_ONLY,
            ),
            planner.plan_charging(
                vehicle_id="VIN_TWO_123456789",
                current_soc=50,
                target_soc=80,
                target_time=None,
                resolved_capacity=resolve_ev_battery_capacity(
                    provider_capacity_kwh=82
                ),
                charger_power_kw=100,
                priority=ChargingPriority.SOLAR_ONLY,
            ),
        )

    first, second = asyncio.run(build_plans())

    assert first.energy_needed_kwh == pytest.approx(9.866666, rel=1e-5)
    assert second.energy_needed_kwh == pytest.approx(27.333333, rel=1e-5)
    assert sum(window.estimated_energy_kwh for window in first.windows) == pytest.approx(
        first.energy_needed_kwh
    )
    assert sum(window.estimated_energy_kwh for window in second.windows) == pytest.approx(
        second.energy_needed_kwh
    )


def test_plan_charging_has_no_implicit_capacity_default():
    with pytest.raises(TypeError, match="resolved_capacity"):
        asyncio.run(
            _planner().plan_charging(
                vehicle_id="unknown",
                current_soc=50,
                target_soc=80,
                target_time=None,
            )
        )


class _RecordingPlanner:
    def __init__(self):
        self.calls = []

    async def plan_charging(self, **kwargs):
        self.calls.append(kwargs)
        resolved = kwargs["resolved_capacity"]
        return ChargingPlan(
            vehicle_id=kwargs["vehicle_id"],
            current_soc=kwargs["current_soc"],
            target_soc=kwargs["target_soc"],
            target_time=None,
            energy_needed_kwh=(
                (kwargs["target_soc"] - kwargs["current_soc"])
                / 100
                * resolved.effective_battery_capacity_kwh
                / 0.9
            ),
            **resolved.to_dict(),
        )


def _executor_with_options(options=None):
    executor = object.__new__(AutoScheduleExecutor)
    executor.hass = SimpleNamespace(data={})
    executor.config_entry = SimpleNamespace(
        entry_id="entry",
        data={},
        options=options or {},
    )
    executor.planner = _RecordingPlanner()
    executor._store = None
    executor._settings = {}
    executor._state = {}
    return executor


def test_regeneration_passes_resolved_capacity_and_serializes_settings(monkeypatch):
    executor = _executor_with_options()
    settings = AutoScheduleSettings(
        vehicle_id="byd_device-123",
        target_soc=80,
        battery_capacity_kwh=29.6,
    )
    state = SimpleNamespace(current_plan=None, last_plan_update=None)

    asyncio.run(
        executor._regenerate_plan(
            settings.vehicle_id,
            settings,
            state,
            current_soc=50,
        )
    )

    resolved = executor.planner.calls[0]["resolved_capacity"]
    assert resolved == ResolvedEVBatteryCapacity(29.6, CAPACITY_SOURCE_MANUAL, 29.6)
    assert state.current_plan.energy_needed_kwh == pytest.approx(9.866666, rel=1e-5)
    assert settings.to_dict()["effective_battery_capacity_kwh"] == 29.6
    assert settings.to_dict()["battery_capacity_source"] == CAPACITY_SOURCE_MANUAL


def test_anonymous_generic_uses_shared_charger_fallback_not_manual():
    executor = _executor_with_options(
        {"generic_charger_battery_capacity_kwh": 44.4}
    )
    settings = AutoScheduleSettings(
        vehicle_id="generic_ev",
        charger_type="generic",
    )

    resolved = executor.resolve_vehicle_capacity("generic_ev", settings)

    assert resolved.effective_battery_capacity_kwh == 44.4
    assert resolved.battery_capacity_source == CAPACITY_SOURCE_CHARGER_FALLBACK
    assert resolved.battery_capacity_kwh is None


def test_clearing_manual_override_falls_back_to_provider():
    executor = _executor_with_options()
    executor._settings["byd_device-123"] = AutoScheduleSettings(
        vehicle_id="byd_device-123",
        battery_capacity_kwh=31,
        provider_battery_capacity_kwh=29.6,
    )

    updated = executor.update_settings(
        "byd_device-123", {"battery_capacity_kwh": None}
    )

    assert updated.battery_capacity_kwh is None
    assert updated.effective_battery_capacity_kwh == 29.6
    assert updated.battery_capacity_source == CAPACITY_SOURCE_PROVIDER
