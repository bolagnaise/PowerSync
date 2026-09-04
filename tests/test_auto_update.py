"""Tests for PowerSync auto-update helpers."""

from __future__ import annotations

import asyncio
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

_ha_dispatcher = sys.modules.setdefault(
    "homeassistant.helpers.dispatcher",
    types.ModuleType("homeassistant.helpers.dispatcher"),
)
_ha_dispatcher.async_dispatcher_send = lambda *args, **kwargs: None

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

_ps_const = types.ModuleType("power_sync.const")
_ps_const.CONF_AUTO_UPDATE_ENABLED = "auto_update_enabled"
_ps_const.CONF_AUTO_UPDATE_TIME = "auto_update_time"
_ps_const.DEFAULT_AUTO_UPDATE_TIME = "03:00"
_ps_const.DOMAIN = "power_sync"
sys.modules["power_sync.const"] = _ps_const

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

    def get(self, entity_id: str) -> _State | None:
        for state in self._states:
            if state.entity_id == entity_id:
                return state
        return None


class _Services:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None, bool]] = []

    async def async_call(
        self,
        domain: str,
        service: str,
        data: dict | None = None,
        *,
        blocking: bool = False,
    ) -> None:
        self.calls.append((domain, service, data, blocking))


class _Hass:
    def __init__(self, states: list[_State]) -> None:
        self.states = _States(states)
        self.services = _Services()
        self.data = {}


class _PrivateHacsRepository:
    """HACS internals that PowerSync must never call or mutate."""

    def __init__(self) -> None:
        self.refresh_count = 0
        self.content = types.SimpleNamespace(
            path=types.SimpleNamespace(remote="custom_components")
        )

    async def update_repository(
        self,
        *,
        ignore_issues: bool = False,
        force: bool = False,
    ) -> None:
        self.refresh_count += 1
        self.content.path.remote = "custom_components/None"
        raise RuntimeError("HACS repository tree is not initialized")


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


def test_install_does_not_mutate_hacs_repository_internals():
    state = _State(
        "update.power_sync_update",
        "on",
        {
            "friendly_name": "PowerSync Update",
            "supported_features": 1,
            "installed_version": "2.12.272",
            "latest_version": "2.12.273",
        },
    )
    hass = _Hass([state])
    repository = _PrivateHacsRepository()
    hass.data["hacs"] = types.SimpleNamespace(
        repositories=types.SimpleNamespace(list_downloaded=[repository])
    )

    result = asyncio.run(auto_update.async_install_power_sync_update(hass))

    assert result == auto_update.AutoUpdateInstallResult(
        entity_id="update.power_sync_update",
        action=auto_update.AUTO_UPDATE_ACTION_INSTALLED,
    )
    assert repository.refresh_count == 0
    assert repository.content.path.remote == "custom_components"
    assert (
        "update",
        "install",
        {"entity_id": "update.power_sync_update"},
        True,
    ) in hass.services.calls


def test_install_handles_hacs_pending_restart_without_reinstalling():
    state = _State(
        "update.power_sync_update",
        "off",
        {
            "friendly_name": "PowerSync Update",
            "supported_features": 1,
            "installed_version": "2.12.273",
            "latest_version": "2.12.273",
            "release_summary": "Restart of Home Assistant required",
        },
    )
    hass = _Hass([state])

    result = asyncio.run(auto_update.async_install_power_sync_update(hass))

    assert result == auto_update.AutoUpdateInstallResult(
        entity_id="update.power_sync_update",
        action=auto_update.AUTO_UPDATE_ACTION_PENDING_RESTART,
    )
    assert not any(
        domain == "update" and service == "install"
        for domain, service, _data, _blocking in hass.services.calls
    )


def test_install_retries_when_hacs_entity_appears_after_scheduler_starts():
    state = _State(
        "update.power_sync_update",
        "on",
        {
            "friendly_name": "PowerSync Update",
            "supported_features": 1,
            "installed_version": "2.12.1231",
            "latest_version": "2.12.1232",
        },
    )

    class _DelayedStates(_States):
        def __init__(self) -> None:
            super().__init__([state])
            self.discovery_count = 0

        def async_all(self, domain: str) -> list[_State]:
            assert domain == "update"
            self.discovery_count += 1
            return [] if self.discovery_count == 1 else self._states

    hass = _Hass([])
    hass.states = _DelayedStates()
    original_interval = auto_update.HACS_REFRESH_INTERVAL_S
    auto_update.HACS_REFRESH_INTERVAL_S = 0
    try:
        result = asyncio.run(auto_update.async_install_power_sync_update(hass))
    finally:
        auto_update.HACS_REFRESH_INTERVAL_S = original_interval

    assert result == auto_update.AutoUpdateInstallResult(
        entity_id="update.power_sync_update",
        action=auto_update.AUTO_UPDATE_ACTION_INSTALLED,
    )
    assert hass.states.discovery_count == 2


def test_scheduler_dispatches_state_refresh_for_already_ran_decision():
    captured: dict[str, object] = {}

    class _Entry:
        entry_id = "ticket-213"
        data: dict = {}
        options = {
            "auto_update_enabled": True,
            "auto_update_time": "09:40",
        }

    class _ScheduleStore:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def async_load(self):
            return {"last_run_date": "2026-09-04"}

    original_store = auto_update.Store
    original_tracker = auto_update.async_track_time_change
    original_send = auto_update.async_dispatcher_send
    auto_update.Store = _ScheduleStore
    auto_update.async_track_time_change = (
        lambda hass, callback, second=0: (
            captured.update(callback=callback, second=second) or (lambda: None)
        )
    )
    auto_update.async_dispatcher_send = (
        lambda hass, signal: captured.setdefault("signals", []).append(signal)
    )
    try:
        hass = _Hass([])
        asyncio.run(auto_update.async_setup_auto_update(hass, _Entry()))
        captured["callback"](datetime(2026, 9, 4, 9, 40))
    finally:
        auto_update.Store = original_store
        auto_update.async_track_time_change = original_tracker
        auto_update.async_dispatcher_send = original_send

    entry_data = hass.data["power_sync"]["ticket-213"]
    assert entry_data["auto_update_last_check_decision"] == "already_ran_today"
    assert captured["signals"] == ["power_sync_ticket-213_auto_update_state"]
