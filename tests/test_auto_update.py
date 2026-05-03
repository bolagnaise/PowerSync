"""Tests for PowerSync auto-update helpers."""

from __future__ import annotations

import enum
import importlib
import sys
import types
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"

_ha_root = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
_ha_components = sys.modules.setdefault(
    "homeassistant.components",
    types.ModuleType("homeassistant.components"),
)
_ha_update = sys.modules.setdefault(
    "homeassistant.components.update",
    types.ModuleType("homeassistant.components.update"),
)
_ha_root.components = _ha_components
_ha_components.update = _ha_update


class _UpdateEntityFeature(enum.IntFlag):
    INSTALL = 1


_ha_update.UpdateEntityFeature = _UpdateEntityFeature

_ha_config_entries = sys.modules.setdefault(
    "homeassistant.config_entries",
    types.ModuleType("homeassistant.config_entries"),
)
_ha_config_entries.ConfigEntry = object

_ha_const = sys.modules.setdefault("homeassistant.const", types.ModuleType("homeassistant.const"))
_ha_const.ATTR_ENTITY_ID = "entity_id"

_ha_core = sys.modules.setdefault("homeassistant.core", types.ModuleType("homeassistant.core"))
_ha_core.HomeAssistant = object
_ha_core.callback = lambda func: func

_ha_helpers = sys.modules.setdefault("homeassistant.helpers", types.ModuleType("homeassistant.helpers"))
_ha_event = sys.modules.setdefault(
    "homeassistant.helpers.event",
    types.ModuleType("homeassistant.helpers.event"),
)
_ha_event.async_track_time_change = lambda *args, **kwargs: lambda: None
_ha_event.async_call_later = lambda *args, **kwargs: lambda: None
_ha_helpers.event = _ha_event

_ha_storage = sys.modules.setdefault(
    "homeassistant.helpers.storage",
    types.ModuleType("homeassistant.helpers.storage"),
)


class _Store:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def async_load(self):
        return None

    async def async_save(self, data):
        return None


_ha_storage.Store = _Store
_ha_helpers.storage = _ha_storage

_ha_util = sys.modules.setdefault("homeassistant.util", types.ModuleType("homeassistant.util"))
_ha_dt = sys.modules.setdefault("homeassistant.util.dt", types.ModuleType("homeassistant.util.dt"))
_ha_dt.utcnow = lambda: datetime(2026, 5, 2, tzinfo=timezone.utc)
_ha_util.dt = _ha_dt

_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps

sys.modules.pop("power_sync.auto_update", None)
auto_update = importlib.import_module("power_sync.auto_update")


class _State:
    def __init__(self, entity_id: str, state: str, attrs: dict) -> None:
        self.entity_id = entity_id
        self.state = state
        self.attributes = attrs


class _States:
    def __init__(self, states: list[_State]) -> None:
        self._states = states

    def async_all(self, domain: str) -> list[_State]:
        assert domain == "update"
        return self._states


class _Hass:
    def __init__(self, states: list[_State]) -> None:
        self.states = _States(states)


def test_auto_update_time_normalizes_hhmm_and_hhmmss():
    assert auto_update.normalize_auto_update_time("3:05") == "03:05"
    assert auto_update.normalize_auto_update_time("03:05:30") == "03:05"


def test_auto_update_time_rejects_out_of_range_values():
    try:
        auto_update.parse_auto_update_time("24:00")
    except ValueError:
        pass
    else:
        raise AssertionError("Expected invalid hour to raise ValueError")


def test_find_power_sync_update_entities_requires_install_capability():
    hass = _Hass([
        _State(
            "update.power_sync_update",
            "on",
            {"friendly_name": "PowerSync Update", "supported_features": 16},
        ),
        _State(
            "update.tesla_amber_sync_update",
            "on",
            {"friendly_name": "Tesla Amber Sync", "supported_features": 1},
        ),
        _State(
            "update.other_addon_update",
            "on",
            {"friendly_name": "Other Add-on", "supported_features": 1},
        ),
    ])

    assert auto_update.find_power_sync_update_entities(hass) == [
        "update.tesla_amber_sync_update",
    ]
    assert auto_update.find_power_sync_update_entities(
        hass,
        require_install=False,
    ) == [
        "update.power_sync_update",
        "update.tesla_amber_sync_update",
    ]
