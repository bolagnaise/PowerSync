"""Regression tests for the EV planner's shared plan clock.

Covers two co-designed bugs that share `AutoScheduleState.last_plan_update`:

- OB-15: `_regenerate_plan()` picked the weekday used for the
  `departure_times`/priority lookup from OS-local `datetime.now()` instead of
  HA-local time. On a UTC-container host with a non-UTC HA timezone, the OS
  weekday disagrees with the true local weekday for the whole UTC-offset
  window each day, selecting the wrong day's departure deadline and priority.

- OB-31: the plan-staleness throttle compared `state.last_plan_update`
  (stamped with OS-local `datetime.now()` inside `_regenerate_plan()`)
  against an HA-local read in `refresh_optimizer_forecast_plans()`, producing
  a garbage elapsed delta equal to roughly the UTC offset instead of the true
  few-seconds/minutes gap.

These two bugs share a clock: fixing OB-15 by switching `_regenerate_plan()`'s
stamp to HA-local also changes which clock `last_plan_update` is stamped
with, so every *read* of it (`refresh_optimizer_forecast_plans()` and
`_evaluate_vehicle()`) must be reconciled to the same HA-local clock or the
mismatch just reappears at a different call site.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"

sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))

# Mirrors the standalone-safe stub block used by tests/test_ev_price_level_ownership.py:
# ev_charging_planner.py pulls in ..optimization (coordinator/executor/battery_optimizer/
# ev_coordinator), so the minimal homeassistant.util.dt-only stub set only works when some
# earlier-collected test module already populated these sys.modules entries. Stub the full
# set here so this file also passes when run standalone (e.g. `pytest tests/test_ev_plan_clock.py`).
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


VIN = "LRWYHCEK3PC907290"
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


def test_regenerate_plan_selects_ha_local_weekday_not_os_weekday(monkeypatch):
    """OB-15: departure_times/priority must be chosen by HA-local weekday."""
    real_datetime = datetime

    # OS-local (container) clock: Thursday 2026-07-02 23:00, naive -- e.g. a
    # UTC container. HA-local clock (dt_util.now()): Friday 2026-07-03 09:00
    # +10 -- the SAME real instant, already the next calendar day in HA-local
    # terms. This is the classic UTC-container-vs-+10-HA mismatch window.
    os_now = real_datetime(2026, 7, 2, 23, 0)
    ha_now = real_datetime(2026, 7, 3, 9, 0, tzinfo=BRISBANE_TZ)
    assert os_now.weekday() == 3  # Thursday, per the OS clock
    assert ha_now.weekday() == 4  # Friday, the TRUE HA-local weekday

    class HostClockDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return os_now.replace(tzinfo=timezone.utc).astimezone(tz)
            return os_now

        @classmethod
        def fromisoformat(cls, value):
            return real_datetime.fromisoformat(value)

    monkeypatch.setattr(ev_planner, "datetime", HostClockDatetime)
    monkeypatch.setattr(ev_planner.dt_util, "now", lambda *a, **k: ha_now)

    settings = ev_planner.AutoScheduleSettings(
        enabled=True,
        vehicle_id=VIN,
        target_soc=80,
        departure_times={
            3: "23:30",  # Thursday deadline -- what the OS-local bug would pick
            4: "17:00",  # Friday deadline -- the correct HA-local "today"
        },
        departure_priorities={
            3: "solar_only",
            4: "time_critical",
        },
    )

    planner = _RecordingPlanner()
    executor = ev_planner.AutoScheduleExecutor(_Hass(), _ConfigEntry(), planner=planner)
    state = ev_planner.AutoScheduleState(vehicle_id=VIN)

    asyncio.run(executor._regenerate_plan(VIN, settings, state, current_soc=50))

    assert len(planner.calls) == 1
    call = planner.calls[0]

    # HA-local "today" is Friday: the plan must target Friday 17:00 with
    # Friday's priority, not Thursday 23:30 / Thursday's priority (which is
    # what walking forward from the OS-local weekday selects).
    assert call["target_time"] == real_datetime(2026, 7, 3, 17, 0)
    assert call["priority"] == ev_planner.ChargingPriority.TIME_CRITICAL

    # last_plan_update must be stamped from the same HA-local clock used to
    # pick the weekday (feeds OB-31 below).
    assert state.last_plan_update == real_datetime(2026, 7, 3, 9, 0)


def _install_clocked_datetime(monkeypatch, clock: dict, os_base: datetime, ha_base: datetime):
    real_datetime = datetime

    class ClockedDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            wall = os_base + timedelta(seconds=clock["elapsed"])
            if tz is not None:
                return wall.replace(tzinfo=timezone.utc).astimezone(tz)
            return wall

        @classmethod
        def fromisoformat(cls, value):
            return real_datetime.fromisoformat(value)

    monkeypatch.setattr(ev_planner, "datetime", ClockedDatetime)
    monkeypatch.setattr(
        ev_planner.dt_util,
        "now",
        lambda *a, **k: ha_base + timedelta(seconds=clock["elapsed"]),
    )


def test_refresh_optimizer_forecast_plans_does_not_regenerate_a_few_seconds_later(monkeypatch):
    """OB-31: refresh_optimizer_forecast_plans() must not treat a genuine
    few-second gap as staleness just because the OS/HA clocks disagree by a
    fixed UTC offset (here +10, i.e. "east of UTC" -- the case that
    previously caused a redundant regenerate on every LP solve)."""
    clock = {"elapsed": 0}
    os_base = datetime(2026, 7, 2, 23, 0, 0)
    ha_base = datetime(2026, 7, 3, 9, 0, 0, tzinfo=BRISBANE_TZ)
    _install_clocked_datetime(monkeypatch, clock, os_base, ha_base)

    async def vehicle_soc(self, vehicle_id):
        return 50

    async def at_home(*args, **kwargs):
        return "home"

    async def not_plugged(*args, **kwargs):
        return False

    monkeypatch.setattr(ev_planner.AutoScheduleExecutor, "_get_vehicle_soc", vehicle_soc)
    monkeypatch.setattr(ev_planner, "get_ev_location", at_home)
    monkeypatch.setattr(ev_planner, "is_ev_plugged_in", not_plugged)

    planner = _RecordingPlanner()
    executor = ev_planner.AutoScheduleExecutor(_Hass(), _ConfigEntry(), planner=planner)
    executor._settings[VIN] = ev_planner.AutoScheduleSettings(
        enabled=True,
        vehicle_id=VIN,
        target_soc=80,
    )

    asyncio.run(executor.refresh_optimizer_forecast_plans())
    assert len(planner.calls) == 1
    assert executor.get_state(VIN).last_plan_update is not None

    # 5 real seconds pass; the OS/HA offset (10h) stays fixed.
    clock["elapsed"] = 5

    asyncio.run(executor.refresh_optimizer_forecast_plans())

    assert len(planner.calls) == 1, (
        "plan_charging() was re-invoked ~5 seconds later -- the staleness "
        "read and the last_plan_update stamp are on different clocks"
    )


def test_evaluate_vehicle_staleness_read_matches_regenerate_plan_stamp_clock(monkeypatch):
    """OB-31 (second read site): _evaluate_vehicle()'s own staleness gate
    must also be reconciled to the clock _regenerate_plan() stamps with, or
    fixing only _regenerate_plan()'s stamp (OB-15) flips the mismatch onto
    this consumer instead of fixing it.

    Uses a genuinely stale gap (10 minutes, past the 5-minute
    _plan_update_interval) and asserts the plan DOES regenerate. A short gap
    is not discriminating here: with the stamp on HA-local (+10) and this
    read left on OS-local, `read - stamp` is permanently a large *negative*
    number (HA is always "ahead"), which is coincidentally also "not stale"
    -- so only a genuine-staleness check catches the broken combination
    (last_plan_update stamped HA-local, read here OS-local) where this
    consumer would silently never regenerate again.
    """
    clock = {"elapsed": 0}
    os_base = datetime(2026, 7, 2, 23, 0, 0)
    ha_base = datetime(2026, 7, 3, 9, 0, 0, tzinfo=BRISBANE_TZ)
    _install_clocked_datetime(monkeypatch, clock, os_base, ha_base)

    async def vehicle_soc(self, vehicle_id):
        return 50

    async def away(*args, **kwargs):
        # Short-circuits _evaluate_vehicle() immediately after the
        # plan-regeneration gate, before any of the charging-decision logic.
        return "not_home"

    monkeypatch.setattr(ev_planner.AutoScheduleExecutor, "_get_vehicle_soc", vehicle_soc)
    monkeypatch.setattr(ev_planner, "get_ev_location", away)

    planner = _RecordingPlanner()
    executor = ev_planner.AutoScheduleExecutor(_Hass(), _ConfigEntry(), planner=planner)
    settings = ev_planner.AutoScheduleSettings(
        enabled=True,
        vehicle_id=VIN,
        target_soc=80,
    )
    executor._settings[VIN] = settings

    asyncio.run(executor._evaluate_vehicle(VIN, settings, {}, None))
    assert len(planner.calls) == 1

    # 10 real minutes pass (> the 5-minute _plan_update_interval); the OS/HA
    # offset (10h) stays fixed.
    clock["elapsed"] = 10 * 60

    asyncio.run(executor._evaluate_vehicle(VIN, settings, {}, None))

    assert len(planner.calls) == 2, (
        "plan_charging() was NOT re-invoked after a genuine 10-minute gap "
        "via _evaluate_vehicle() -- its staleness read and the "
        "_regenerate_plan() stamp are on different clocks"
    )
