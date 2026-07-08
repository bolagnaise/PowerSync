"""Regression tests for OB-23: EV load profile must bucket by LOCAL hour/weekday.

`LoadProfileEstimator.update_from_history` builds a 24-hour weekday/weekend
load profile from Home Assistant history. `State.last_updated` is UTC-aware,
so bucketing directly off it (pre-fix) rotates the learned curve by the
local UTC offset on any non-UTC install. Consumers (`estimate_load_at_hour`
and friends) query the profile using the LOCAL hour, so the buckets must be
built in local time too.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"

# --- Home Assistant stub environment -----------------------------------
# Mirrors the proven-working stub set from tests/test_ev_price_level_ownership.py
# (confirmed to import power_sync.automations.ev_charging_planner standalone),
# plus a recorder/recorder.history stub (mirroring tests/test_load_estimator.py)
# since update_from_history() does a function-local
# `from homeassistant.components.recorder import get_instance` import.

sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))

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
_ha_components = sys.modules.setdefault(
    "homeassistant.components", types.ModuleType("homeassistant.components")
)
_ha_recorder = sys.modules.setdefault(
    "homeassistant.components.recorder", types.ModuleType("homeassistant.components.recorder")
)
_ha_recorder_history = sys.modules.setdefault(
    "homeassistant.components.recorder.history",
    types.ModuleType("homeassistant.components.recorder.history"),
)

_ha_core.HomeAssistant = type("HomeAssistant", (), {})
_ha_config_entries.ConfigEntry = type("ConfigEntry", (), {})
_ha_exceptions.ConfigEntryNotReady = type("ConfigEntryNotReady", (Exception,), {})
_ha_er.async_get = lambda hass: getattr(hass, "entity_registry", None)
_ha_dr.async_get = lambda hass: getattr(hass, "device_registry", None)
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

# The fixture under test converts UTC -> a fixed local offset of +10h
# (e.g. Australia/Sydney, AEST) to make the bug's rotation observable.
LOCAL_OFFSET = timedelta(hours=10)
_ha_dt.now = lambda *args, **kwargs: datetime.now(timezone.utc) + LOCAL_OFFSET
_ha_dt.utcnow = lambda *args, **kwargs: datetime.now(timezone.utc)
_ha_dt.as_local = lambda value: value + LOCAL_OFFSET

_ha_recorder.get_instance = lambda hass: getattr(hass, "_fake_recorder", None)
_ha_recorder_history.get_significant_states = object()  # never actually called by the fake recorder

_ha_helpers.entity_registry = _ha_er
_ha_helpers.device_registry = _ha_dr
_ha_helpers.storage = _ha_storage
_ha_helpers.update_coordinator = _ha_update
_ha_helpers.event = _ha_event
_ha_helpers.aiohttp_client = _ha_aiohttp_client
_ha_root.helpers = _ha_helpers
_ha_util.dt = _ha_dt
_ha_root.util = _ha_util
_ha_root.components = _ha_components
_ha_components.recorder = _ha_recorder
_ha_recorder.history = _ha_recorder_history

_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps

_automations = types.ModuleType("power_sync.automations")
_automations.__path__ = [str(ROOT / "automations")]
sys.modules["power_sync.automations"] = _automations

if not hasattr(sys.modules.get("power_sync.const"), "TESLA_INTEGRATIONS"):
    sys.modules.pop("power_sync.const", None)

ev_planner = importlib.import_module("power_sync.automations.ev_charging_planner")


# --- Fakes ---------------------------------------------------------------


class _FakeState:
    def __init__(self, entity_id: str, state: str, last_updated: datetime) -> None:
        self.entity_id = entity_id
        self.state = state
        self.last_updated = last_updated


class _FakeStates:
    """Minimal hass.states stub supporting async_entity_ids()."""

    def __init__(self, entity_ids: list[str]) -> None:
        self._entity_ids = entity_ids

    def async_entity_ids(self, domain: str | None = None):
        if domain is None:
            return list(self._entity_ids)
        return [e for e in self._entity_ids if e.startswith(f"{domain}.")]


class _FakeRecorder:
    """Returns canned history without touching get_significant_states."""

    def __init__(self, history: dict) -> None:
        self._history = history

    async def async_add_executor_job(self, func, hass, start_time, end_time, entity_ids):
        return self._history


class _FakeHass:
    def __init__(self, load_entity: str, history_states: list[_FakeState]) -> None:
        self.states = _FakeStates([load_entity])
        self._fake_recorder = _FakeRecorder({load_entity: history_states})


LOAD_ENTITY = "sensor.home_load_power"


def _run(coro):
    return asyncio.run(coro)


# --- Tests -----------------------------------------------------------------


def test_load_profile_bucketed_by_local_hour_not_utc():
    """08:00 UTC == 18:00 local (+10h). The reading must land in local bucket 18."""
    utc_instant = datetime(2026, 7, 8, 8, 0, tzinfo=timezone.utc)  # Wed both UTC and local
    state = _FakeState(LOAD_ENTITY, "3500", utc_instant)  # 3500 W -> 3.5 kW
    hass = _FakeHass(LOAD_ENTITY, [state])

    estimator = ev_planner.LoadProfileEstimator(hass)
    _run(estimator.update_from_history(days=14))

    weekday_profile = estimator._load_history["weekday"]

    # Fixed (local-hour) behaviour: the learned reading lands at local hour 18.
    assert weekday_profile[18] == pytest.approx(3.5)
    # Bucket 8 (the UTC hour) must NOT have been touched by this reading -
    # it should still hold the untouched default for that hour.
    assert weekday_profile[8] == pytest.approx(
        ev_planner.LoadProfileEstimator.DEFAULT_WEEKDAY_PROFILE[8]
    )
    assert weekday_profile[8] != pytest.approx(3.5)


def test_load_profile_weekend_flag_uses_local_weekday():
    """Sun 23:00 UTC == Mon 09:00 local (+10h): must bucket as WEEKDAY hour 9."""
    utc_instant = datetime(2026, 7, 12, 23, 0, tzinfo=timezone.utc)  # Sunday in UTC
    state = _FakeState(LOAD_ENTITY, "2500", utc_instant)  # 2500 W -> 2.5 kW
    hass = _FakeHass(LOAD_ENTITY, [state])

    estimator = ev_planner.LoadProfileEstimator(hass)
    _run(estimator.update_from_history(days=14))

    weekday_profile = estimator._load_history["weekday"]
    weekend_profile = estimator._load_history["weekend"]

    # Fixed behaviour: local time is Monday 09:00 -> weekday bucket 9.
    assert weekday_profile[9] == pytest.approx(2.5)
    # The weekend bucket for hour 23 must remain the untouched default -
    # the UTC-Sunday reading must not have landed there.
    assert weekend_profile[23] == pytest.approx(
        ev_planner.LoadProfileEstimator.DEFAULT_WEEKEND_PROFILE[23]
    )
    assert weekend_profile[23] != pytest.approx(2.5)
