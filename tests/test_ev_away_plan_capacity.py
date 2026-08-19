"""Regression tests for Smart Schedule planning while a vehicle is away.

`_evaluate_vehicle()` deliberately regenerates the plan *before* the
location/plugged availability gates so an away EV with a deadline still
protects its future charging demand in the optimiser's load forecast.  Two
things went wrong inside that deliberate window:

- OB-53: `_resolve_tesla_active_charger_capability()` returns the 5A
  *command-path* safety floor (`TESLA_UNKNOWN_CHARGER_SAFE_AMPS`) whenever the
  live EVSE association or capability is unreadable -- which is exactly what an
  away, unplugged, or asleep vehicle reports.  `_regenerate_plan()` consumed
  that floor as the *planning* rate, so an away 32A/7.4kW loadpoint planned at
  1.15kW single-phase (3.45kW on three phases).  The safety floor is correct
  for issuing charger commands to an unidentified EVSE; it is not a forecast.

- OB-54: `_other_planned_ev_power_schedule()` subtracts every *other* enabled
  vehicle's planned power from the site import capacity, with no check on
  whether that vehicle is actually present.  An EV that is away and unplugged
  therefore throttled the planned rate of the EV sitting on the Wall Connector.
  With two cars this is symmetric -- both plans get reduced -- so neither car
  plans at the charger's real rate even when only one is home.

The Price-Level projection already models this correctly for the same
vehicles: `_expected_snapshot_reason()` demotes an away/unplugged vehicle to
*conditional* load rather than shrinking its power, and never feeds it into
the optimiser's expected demand.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"

sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))

# Standalone-safe stub block, mirroring tests/test_ev_plan_clock.py.
_ha_root = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
_ha_config_entries = sys.modules.setdefault(
    "homeassistant.config_entries", types.ModuleType("homeassistant.config_entries")
)
_ha_core = sys.modules.setdefault("homeassistant.core", types.ModuleType("homeassistant.core"))
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
_ha_er = sys.modules.setdefault(
    "homeassistant.helpers.entity_registry",
    types.ModuleType("homeassistant.helpers.entity_registry"),
)
_ha_dr = sys.modules.setdefault(
    "homeassistant.helpers.device_registry",
    types.ModuleType("homeassistant.helpers.device_registry"),
)
_ha_event = sys.modules.setdefault(
    "homeassistant.helpers.event", types.ModuleType("homeassistant.helpers.event")
)
_ha_aiohttp_client = sys.modules.setdefault(
    "homeassistant.helpers.aiohttp_client",
    types.ModuleType("homeassistant.helpers.aiohttp_client"),
)
_ha_util = sys.modules.setdefault("homeassistant.util", types.ModuleType("homeassistant.util"))
_ha_dt = sys.modules.setdefault("homeassistant.util.dt", types.ModuleType("homeassistant.util.dt"))
_ha_core.HomeAssistant = type("HomeAssistant", (), {})
_ha_config_entries.ConfigEntry = type("ConfigEntry", (), {})
_ha_exceptions.ConfigEntryNotReady = type("ConfigEntryNotReady", (Exception,), {})
_ha_er.async_get = lambda hass: getattr(hass, "entity_registry", SimpleNamespace(entities={}))
_ha_dr.async_get = lambda hass: getattr(hass, "device_registry", SimpleNamespace(devices={}))
_ha_storage.Store = type("Store", (), {"__init__": lambda self, *args, **kwargs: None})
_ha_update.DataUpdateCoordinator = type(
    "DataUpdateCoordinator",
    (),
    {
        "__class_getitem__": classmethod(lambda cls, item: cls),
        "__init__": lambda self, *args, **kwargs: None,
    },
)
_ha_event.async_track_time_interval = lambda *args, **kwargs: (lambda: None)
_ha_event.async_track_time_change = lambda *args, **kwargs: (lambda: None)
_ha_event.async_track_point_in_time = lambda *args, **kwargs: (lambda: None)
_ha_dt.now = getattr(_ha_dt, "now", lambda *args, **kwargs: None)
_ha_dt.utcnow = getattr(_ha_dt, "utcnow", lambda *args, **kwargs: None)
_ha_helpers.entity_registry = _ha_er
_ha_helpers.device_registry = _ha_dr
_ha_helpers.storage = _ha_storage
_ha_helpers.update_coordinator = _ha_update
_ha_helpers.event = _ha_event
_ha_helpers.aiohttp_client = _ha_aiohttp_client
_ha_root.helpers = _ha_helpers
_ha_util.dt = _ha_dt
_ha_root.util = _ha_util

_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps

_optimization = types.ModuleType("power_sync.optimization")
_optimization.__path__ = [str(ROOT / "optimization")]
sys.modules["power_sync.optimization"] = _optimization

_automations = types.ModuleType("power_sync.automations")
_automations.__path__ = [str(ROOT / "automations")]
sys.modules["power_sync.automations"] = _automations

if not hasattr(sys.modules.get("power_sync.const"), "TESLA_INTEGRATIONS"):
    sys.modules.pop("power_sync.const", None)

ev_planner = importlib.import_module("power_sync.automations.ev_charging_planner")


HOME_VIN = "5YJTEST0000000001"
AWAY_VIN = "5YJTEST0000000002"
BRISBANE_TZ = timezone(timedelta(hours=10))


class _Hass:
    def __init__(self) -> None:
        self.data: dict = {}
        self.entity_registry = SimpleNamespace(entities={})
        self.device_registry = SimpleNamespace(devices={})


class _ConfigEntry:
    entry_id = "entry-1"
    data: dict = {}
    options: dict = {}


class _RecordingPlanner:
    """Fake ChargingPlanner that records every plan_charging() call."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def plan_charging(self, **kwargs):
        self.calls.append(kwargs)
        target_time = kwargs.get("target_time")
        return ev_planner.ChargingPlan(
            vehicle_id=kwargs["vehicle_id"],
            current_soc=kwargs["current_soc"],
            target_soc=kwargs["target_soc"],
            target_time=target_time.isoformat() if target_time else None,
            energy_needed_kwh=5.0,
        )


def _settings(vehicle_id: str, **overrides) -> "ev_planner.AutoScheduleSettings":
    base = {
        "enabled": True,
        "vehicle_id": vehicle_id,
        "target_soc": 80,
        "charger_type": "tesla",
        "min_charge_amps": 5,
        "max_charge_amps": 32,
        "voltage": 230,
        "phases": 1,
    }
    base.update(overrides)
    return ev_planner.AutoScheduleSettings(**base)


def _safe_fallback_capability(phases: int = 1) -> dict:
    """What the Tesla resolver returns for an away/unplugged/asleep vehicle."""
    return {
        "association_known": False,
        "capability_known": False,
        "max_charge_amps": 5,
        "max_charge_amps_source": "safe_unplugged",
        "voltage": 230,
        "phases": phases,
    }


def _live_capability(max_amps: int, phases: int = 1) -> dict:
    """What the resolver returns for a plugged-in vehicle on a known EVSE."""
    return {
        "association_known": True,
        "capability_known": True,
        "max_charge_amps": max_amps,
        "max_charge_amps_source": "active_charger",
        "voltage": 230,
        "phases": phases,
    }


def _plan_with_window(vehicle_id: str, start: datetime, end: datetime, power_kw: float):
    return ev_planner.ChargingPlan(
        vehicle_id=vehicle_id,
        current_soc=40,
        target_soc=80,
        target_time=None,
        energy_needed_kwh=10.0,
        windows=[
            ev_planner.PlannedChargingWindow(
                start_time=start.isoformat(),
                end_time=end.isoformat(),
                source="grid_offpeak",
                estimated_power_kw=power_kw,
                estimated_energy_kwh=power_kw,
                price_cents_kwh=5.0,
                reason="cheap_rate",
            )
        ],
    )


def test_away_vehicle_plans_at_configured_rate_not_the_command_safety_floor():
    """OB-53: the 5A unknown-EVSE floor must not become the planned rate."""
    planner = _RecordingPlanner()
    executor = ev_planner.AutoScheduleExecutor(_Hass(), _ConfigEntry(), planner=planner)
    settings = _settings(HOME_VIN)
    state = ev_planner.AutoScheduleState(vehicle_id=HOME_VIN)

    asyncio.run(
        executor._regenerate_plan(
            HOME_VIN,
            settings,
            state,
            current_soc=40,
            charger_capability=_safe_fallback_capability(),
        )
    )

    assert len(planner.calls) == 1
    call = planner.calls[0]
    # Configured 32A x 230V x 1 phase = 7.36kW, NOT 5A x 230V = 1.15kW.
    assert call["charger_power_kw"] == 32 * 230 * 1 / 1000
    # The physical amp step and minimum must stay on the configured geometry.
    assert call["charging_power_step_kw"] == 230 * 1 / 1000
    assert call["minimum_charging_power_kw"] == pytest.approx(5 * 230 * 1 / 1000)


def test_away_three_phase_vehicle_does_not_plan_at_the_5a_three_phase_floor():
    """OB-53: 5A x 230V x 3 = 3.45kW is the floor, not a three-phase plan."""
    planner = _RecordingPlanner()
    executor = ev_planner.AutoScheduleExecutor(_Hass(), _ConfigEntry(), planner=planner)
    settings = _settings(HOME_VIN, phases=3)
    state = ev_planner.AutoScheduleState(vehicle_id=HOME_VIN)

    asyncio.run(
        executor._regenerate_plan(
            HOME_VIN,
            settings,
            state,
            current_soc=40,
            charger_capability=_safe_fallback_capability(phases=3),
        )
    )

    call = planner.calls[0]
    assert call["charger_power_kw"] != 5 * 230 * 3 / 1000  # 3.45kW
    assert call["charger_power_kw"] == 32 * 230 * 3 / 1000


def test_live_charger_limit_below_configured_is_still_honoured_for_planning():
    """A *known* lower EVSE limit (e.g. a 15A mobile connector) must survive."""
    planner = _RecordingPlanner()
    executor = ev_planner.AutoScheduleExecutor(_Hass(), _ConfigEntry(), planner=planner)
    settings = _settings(HOME_VIN)
    state = ev_planner.AutoScheduleState(vehicle_id=HOME_VIN)

    asyncio.run(
        executor._regenerate_plan(
            HOME_VIN,
            settings,
            state,
            current_soc=40,
            charger_capability=_live_capability(15),
        )
    )

    call = planner.calls[0]
    assert call["charger_power_kw"] == 15 * 230 * 1 / 1000


def test_away_vehicle_does_not_reserve_site_capacity_from_the_plugged_in_car():
    """OB-54: an away EV's retained plan must not throttle the present EV."""
    executor = ev_planner.AutoScheduleExecutor(_Hass(), _ConfigEntry(), planner=_RecordingPlanner())

    start = datetime(2026, 8, 19, 2, 0)
    end = datetime(2026, 8, 19, 4, 0)

    executor._settings[HOME_VIN] = _settings(HOME_VIN)
    executor._settings[AWAY_VIN] = _settings(AWAY_VIN)
    executor._state[HOME_VIN] = ev_planner.AutoScheduleState(vehicle_id=HOME_VIN)
    away_state = ev_planner.AutoScheduleState(vehicle_id=AWAY_VIN)
    away_state.current_plan = _plan_with_window(AWAY_VIN, start, end, 7.36)
    away_state.last_decision = "away"
    away_state.last_decision_reason = "Vehicle not at home (location: not_home)"
    executor._state[AWAY_VIN] = away_state

    reserved = executor._other_planned_ev_power_schedule(HOME_VIN)

    assert reserved == {}


def test_unplugged_vehicle_does_not_reserve_site_capacity():
    """OB-54: same for a car at home that is simply not plugged in."""
    executor = ev_planner.AutoScheduleExecutor(_Hass(), _ConfigEntry(), planner=_RecordingPlanner())

    start = datetime(2026, 8, 19, 2, 0)
    end = datetime(2026, 8, 19, 4, 0)

    executor._settings[HOME_VIN] = _settings(HOME_VIN)
    executor._settings[AWAY_VIN] = _settings(AWAY_VIN)
    executor._state[HOME_VIN] = ev_planner.AutoScheduleState(vehicle_id=HOME_VIN)
    idle_state = ev_planner.AutoScheduleState(vehicle_id=AWAY_VIN)
    idle_state.current_plan = _plan_with_window(AWAY_VIN, start, end, 7.36)
    idle_state.last_decision = "unplugged"
    idle_state.last_decision_reason = "Vehicle not plugged in"
    executor._state[AWAY_VIN] = idle_state

    assert executor._other_planned_ev_power_schedule(HOME_VIN) == {}


def test_present_second_vehicle_still_reserves_site_capacity():
    """The sharing behaviour must survive for two genuinely present cars."""
    executor = ev_planner.AutoScheduleExecutor(_Hass(), _ConfigEntry(), planner=_RecordingPlanner())

    start = datetime(2026, 8, 19, 2, 0)
    end = datetime(2026, 8, 19, 4, 0)

    executor._settings[HOME_VIN] = _settings(HOME_VIN)
    executor._settings[AWAY_VIN] = _settings(AWAY_VIN)
    executor._state[HOME_VIN] = ev_planner.AutoScheduleState(vehicle_id=HOME_VIN)
    present_state = ev_planner.AutoScheduleState(vehicle_id=AWAY_VIN)
    present_state.current_plan = _plan_with_window(AWAY_VIN, start, end, 7.36)
    present_state.last_decision = "charging"
    executor._state[AWAY_VIN] = present_state

    reserved = executor._other_planned_ev_power_schedule(HOME_VIN)

    assert reserved == {
        datetime(2026, 8, 19, 2, 0).isoformat(): 7.36,
        datetime(2026, 8, 19, 3, 0).isoformat(): 7.36,
    }
