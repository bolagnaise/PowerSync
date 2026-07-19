"""Tests for EV ownership persistence and restart recovery."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"

_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps

_automations = types.ModuleType("power_sync.automations")
_automations.__path__ = [str(ROOT / "automations")]
sys.modules["power_sync.automations"] = _automations

ev_ownership = importlib.import_module("power_sync.automations.ev_ownership")


class _Entry:
    entry_id = "entry-1"


class _Hass:
    def __init__(self) -> None:
        self.data = {"power_sync": {"entry-1": {}}}
        self.created_tasks = []

    def async_create_task(self, coro):
        self.created_tasks.append(coro)


class _Store:
    def __init__(self, data=None) -> None:
        self._data = data or {}
        self.saved = 0

    async def async_save(self):
        self.saved += 1


def test_persist_ev_runtime_state_saves_ownership_and_last_commands():
    hass = _Hass()
    store = _Store()
    hass.data["power_sync"]["entry-1"]["automation_store"] = store

    ev_ownership.claim_ev_ownership(
        hass,
        _Entry(),
        "VIN123",
        owner_mode="manual",
        command="start",
        reason="Manual start",
    )

    # Drain the best-effort save scheduled by claim_ev_ownership.
    for task in hass.created_tasks:
        asyncio.run(task)

    assert store.saved == 1
    runtime = store._data["ev_runtime_state"]
    assert runtime["active_ownership"]["VIN123"]["owner_mode"] == "manual"
    assert runtime["last_commands"]["VIN123"]["command"] == "start"


def test_restore_ev_runtime_state_clears_stale_active_ownership():
    store = _Store(
        {
            "ev_runtime_state": {
                "active_ownership": {
                    "VIN123": {
                        "owner": "powersync",
                        "owner_mode": "manual",
                        "last_command": {
                            "command": "start",
                            "at": "2026-05-01T00:00:00+00:00",
                            "source": "powersync",
                            "success": True,
                            "reason": "Manual start",
                        },
                    }
                },
                "last_commands": {
                    "VIN123": {
                        "command": "start",
                        "at": "2026-05-01T00:00:00+00:00",
                        "source": "powersync",
                        "success": True,
                        "reason": "Manual start",
                    }
                },
            }
        }
    )
    hass = _Hass()
    hass.data["power_sync"]["entry-1"]["automation_store"] = store

    result = ev_ownership.restore_ev_runtime_state(hass, _Entry(), store)
    for task in hass.created_tasks:
        asyncio.run(task)

    assert result["restored_ownership"] == 1
    assert result["restored_commands"] == 1
    assert result["resumable_manual_sessions"] == {}
    assert result["expired_manual_sessions"] == {}
    assert hass.data["power_sync"]["entry-1"]["ev_ownership"] == {}
    recovered = hass.data["power_sync"]["entry-1"]["ev_recovered_ownership"]
    assert recovered["VIN123"]["owner_mode"] == "manual"
    last_command = hass.data["power_sync"]["entry-1"]["ev_last_command"]["VIN123"]
    assert last_command["command"] == "ha_restart_recovery"
    assert last_command["success"] is True
    assert "manual ownership" in last_command["reason"]


def test_restore_ev_runtime_state_resaves_cleared_snapshot():
    store = _Store(
        {
            "ev_runtime_state": {
                "active_ownership": {
                    "VIN123": {"owner": "powersync", "owner_mode": "solar_surplus"}
                },
                "last_commands": {},
            }
        }
    )
    hass = _Hass()
    hass.data["power_sync"]["entry-1"]["automation_store"] = store

    ev_ownership.restore_ev_runtime_state(hass, _Entry(), store)
    for task in hass.created_tasks:
        asyncio.run(task)

    runtime = store._data["ev_runtime_state"]
    assert runtime["active_ownership"] == {}
    assert runtime["last_commands"]["VIN123"]["command"] == "ha_restart_recovery"


def test_restore_ev_runtime_state_returns_unexpired_manual_quick_session():
    store = _Store(
        {
            "ev_runtime_state": {
                "active_ownership": {
                    "generic_ev": {
                        "owner": "powersync",
                        "owner_mode": "manual",
                        "quick_control": True,
                        "duration_minutes": 30,
                        "expires_at": "2099-05-01T01:30:00+00:00",
                        "resume_params": {
                            "charger_type": "generic",
                            "charger_switch_entity": "switch.granny_charger",
                            "source_mode": "standard",
                        },
                    }
                },
                "last_commands": {},
            }
        }
    )
    hass = _Hass()
    hass.data["power_sync"]["entry-1"]["automation_store"] = store

    result = ev_ownership.restore_ev_runtime_state(hass, _Entry(), store)
    for task in hass.created_tasks:
        asyncio.run(task)

    assert result["resumable_manual_sessions"] == {
        "generic_ev": {
            "owner": "powersync",
            "owner_mode": "manual",
            "quick_control": True,
            "duration_minutes": 30,
            "expires_at": "2099-05-01T01:30:00+00:00",
            "resume_params": {
                "charger_type": "generic",
                "charger_switch_entity": "switch.granny_charger",
                "source_mode": "standard",
            },
        }
    }


def test_restore_ev_runtime_state_does_not_resume_expired_manual_quick_session():
    store = _Store(
        {
            "ev_runtime_state": {
                "active_ownership": {
                    "generic_ev": {
                        "owner": "powersync",
                        "owner_mode": "manual",
                        "quick_control": True,
                        "expires_at": "2020-05-01T01:30:00+00:00",
                        "resume_params": {
                            "charger_type": "generic",
                            "charger_switch_entity": "switch.granny_charger",
                        },
                    }
                },
                "last_commands": {},
            }
        }
    )
    hass = _Hass()
    hass.data["power_sync"]["entry-1"]["automation_store"] = store

    result = ev_ownership.restore_ev_runtime_state(hass, _Entry(), store)
    for task in hass.created_tasks:
        asyncio.run(task)

    assert result["resumable_manual_sessions"] == {}
    assert result["expired_manual_sessions"]["generic_ev"]["expires_at"] == (
        "2020-05-01T01:30:00+00:00"
    )


def test_takeover_flag_only_replaces_solar_surplus_ownership():
    assert ev_ownership.can_take_over_ev_ownership(
        "solar_surplus",
        "price_level_opportunity",
        allow_takeover=True,
    )
    assert ev_ownership.can_take_over_ev_ownership(
        "smart_schedule_solar_surplus",
        "price_level_opportunity",
        allow_takeover=True,
    )
    assert ev_ownership.can_take_over_ev_ownership(
        "solar_surplus",
        "smart_schedule",
        allow_takeover=True,
    )

    assert not ev_ownership.can_take_over_ev_ownership(
        "manual",
        "price_level_opportunity",
        allow_takeover=True,
    )
    assert not ev_ownership.can_take_over_ev_ownership(
        "smart_schedule",
        "price_level_opportunity",
        allow_takeover=True,
    )
