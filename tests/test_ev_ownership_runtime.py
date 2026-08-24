"""Tests for EV ownership persistence and restart recovery."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime, timedelta, timezone
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


def test_consume_recovered_ev_ownership_is_fresh_exact_and_one_shot():
    vehicle_id = "5YJTEST0000000001"
    saved_at = datetime.now(timezone.utc).isoformat()
    store = _Store(
        {
            "ev_runtime_state": {
                "active_ownership": {
                    vehicle_id: {
                        "owner": "powersync",
                        "owner_mode": "solar_surplus",
                        "charger_type": "tesla",
                        "session_id": "solar-session",
                        "last_commanded_amps": 1,
                    }
                },
                "last_commands": {},
                "saved_at": saved_at,
            }
        }
    )
    hass = _Hass()
    hass.data["power_sync"]["entry-1"]["automation_store"] = store

    ev_ownership.restore_ev_runtime_state(hass, _Entry(), store)
    for task in hass.created_tasks:
        asyncio.run(task)

    assert (
        ev_ownership.consume_recovered_ev_ownership(
            hass,
            _Entry(),
            "5YJOTHER000000001",
            expected_owner_family="solar_surplus",
        )
        is None
    )
    recovered = ev_ownership.consume_recovered_ev_ownership(
        hass,
        _Entry(),
        vehicle_id,
        expected_owner_family="solar_surplus",
    )
    assert recovered is not None
    assert recovered["session_id"] == "solar-session"
    assert recovered["last_commanded_amps"] == 1
    assert recovered["recovered_saved_at"] == saved_at
    assert (
        ev_ownership.consume_recovered_ev_ownership(
            hass,
            _Entry(),
            vehicle_id,
            expected_owner_family="solar_surplus",
        )
        is None
    )


def test_consume_recovered_ev_ownership_rejects_stale_snapshot():
    vehicle_id = "5YJTEST0000000001"
    store = _Store(
        {
            "ev_runtime_state": {
                "active_ownership": {
                    vehicle_id: {
                        "owner": "powersync",
                        "owner_mode": "solar_surplus",
                        "charger_type": "tesla",
                        "session_id": "stale-session",
                    }
                },
                "last_commands": {},
                "saved_at": (
                    datetime.now(timezone.utc) - timedelta(minutes=16)
                ).isoformat(),
            }
        }
    )
    hass = _Hass()
    hass.data["power_sync"]["entry-1"]["automation_store"] = store

    ev_ownership.restore_ev_runtime_state(hass, _Entry(), store)
    for task in hass.created_tasks:
        asyncio.run(task)

    assert (
        ev_ownership.consume_recovered_ev_ownership(
            hass,
            _Entry(),
            vehicle_id,
            expected_owner_family="solar_surplus",
        )
        is None
    )
    assert hass.data["power_sync"]["entry-1"]["ev_recovered_ownership"] == {}


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


def test_explicit_boost_can_take_over_automated_or_manual_ownership():
    assert ev_ownership.can_take_over_ev_ownership("smart_schedule", "boost")
    assert ev_ownership.can_take_over_ev_ownership("manual", "boost")
    assert ev_ownership.can_take_over_ev_ownership("solar_surplus", "boost")
    assert ev_ownership.can_take_over_ev_ownership("scheduled", "boost")


def test_boost_is_its_own_arbitration_family_and_yields_to_manual():
    assert ev_ownership.owner_family("boost") == "boost"
    # Boost supersedes automated owners, but a later manual command still wins.
    assert ev_ownership.can_take_over_ev_ownership("boost", "manual")
    assert not ev_ownership.can_take_over_ev_ownership("boost", "smart_schedule")


def test_automated_owners_yield_to_external_ownership():
    hass = _Hass()
    vehicle_id = "5YJTEST0000000001"
    ev_ownership.claim_ev_ownership(
        hass,
        _Entry(),
        vehicle_id,
        owner="external",
        owner_mode="external",
    )

    for owner_mode in (
        "smart_schedule",
        "price_level_opportunity",
        "scheduled",
        "solar_surplus",
    ):
        can_claim, _lease_id, lease, reason = ev_ownership.can_claim_ev_ownership(
            hass,
            _Entry(),
            vehicle_id,
            owner_mode=owner_mode,
            allow_takeover=True,
        )
        assert can_claim is False
        assert lease["owner"] == "external"
        assert reason == "external already owns this loadpoint"


def test_manual_command_can_take_over_external_ownership():
    hass = _Hass()
    vehicle_id = "5YJTEST0000000001"
    ev_ownership.claim_ev_ownership(
        hass,
        _Entry(),
        vehicle_id,
        owner="external",
        owner_mode="external",
    )

    can_claim, lease_id, _lease, reason = ev_ownership.can_claim_ev_ownership(
        hass,
        _Entry(),
        vehicle_id,
        owner_mode="manual",
    )

    assert can_claim is True
    assert lease_id == vehicle_id
    assert reason is None


def test_recent_powersync_command_blocks_stale_external_observation():
    hass = _Hass()
    vehicle_id = "5YJTEST0000000001"
    command = ev_ownership.record_ev_command(
        hass,
        _Entry(),
        vehicle_id,
        command="stop_smart_schedule",
        success=True,
    )

    claimed = ev_ownership.ensure_external_ev_ownership(
        hass,
        _Entry(),
        vehicle_id,
        observed_at=datetime.fromisoformat(command["at"]) - timedelta(seconds=1),
    )

    assert claimed is False
    assert ev_ownership.get_ev_ownership(hass, _Entry(), vehicle_id) == (None, None)


def test_new_observation_after_powersync_command_claims_external_ownership():
    hass = _Hass()
    vehicle_id = "5YJTEST0000000001"
    command = ev_ownership.record_ev_command(
        hass,
        _Entry(),
        vehicle_id,
        command="stop_smart_schedule",
        success=True,
    )

    claimed = ev_ownership.ensure_external_ev_ownership(
        hass,
        _Entry(),
        vehicle_id,
        session_id="observed-session",
        observed_at=datetime.fromisoformat(command["at"]) + timedelta(seconds=1),
    )

    assert claimed is True
    _lease_id, lease = ev_ownership.get_ev_ownership(hass, _Entry(), vehicle_id)
    assert lease["owner"] == "external"
    assert lease["session_id"] == "observed-session"
    assert lease["stop_settling"] is True


def test_external_start_after_confirmed_powersync_stop_is_sticky():
    hass = _Hass()
    vehicle_id = "5YJTEST0000000001"
    command = ev_ownership.record_ev_command(
        hass,
        _Entry(),
        vehicle_id,
        command="stop",
        success=True,
    )
    command_time = datetime.fromisoformat(command["at"])

    assert ev_ownership.confirm_powersync_ev_stop(
        hass,
        _Entry(),
        vehicle_id,
        observed_at=command_time + timedelta(seconds=1),
    ) is True
    assert ev_ownership.ensure_external_ev_ownership(
        hass,
        _Entry(),
        vehicle_id,
        observed_at=command_time + timedelta(seconds=2),
    ) is True

    _lease_id, lease = ev_ownership.get_ev_ownership(
        hass,
        _Entry(),
        vehicle_id,
    )
    assert lease["owner"] == "external"
    assert "stop_settling" not in lease
    assert ev_ownership.confirm_powersync_ev_stop(
        hass,
        _Entry(),
        vehicle_id,
        observed_at=command_time + timedelta(seconds=3),
    ) is False
    assert ev_ownership.get_ev_ownership(hass, _Entry(), vehicle_id)[1] is lease


def test_new_external_session_promotes_provisional_stop_lease_to_sticky():
    hass = _Hass()
    vehicle_id = "5YJTEST0000000001"
    command = ev_ownership.record_ev_command(
        hass,
        _Entry(),
        vehicle_id,
        command="stop",
        success=True,
    )
    command_time = datetime.fromisoformat(command["at"])

    assert ev_ownership.ensure_external_ev_ownership(
        hass,
        _Entry(),
        vehicle_id,
        session_id="delayed-readback",
        observed_at=command_time + timedelta(seconds=1),
    ) is True
    assert ev_ownership.ensure_external_ev_ownership(
        hass,
        _Entry(),
        vehicle_id,
        session_id="genuine-external-session",
        observed_at=command_time + timedelta(seconds=2),
    ) is True

    _lease_id, lease = ev_ownership.get_ev_ownership(
        hass,
        _Entry(),
        vehicle_id,
    )
    assert lease["session_id"] == "genuine-external-session"
    assert "stop_settling" not in lease
    assert ev_ownership.confirm_powersync_ev_stop(
        hass,
        _Entry(),
        vehicle_id,
        observed_at=command_time + timedelta(seconds=3),
    ) is False


def test_fresh_recovered_powersync_session_blocks_external_claim():
    hass = _Hass()
    vehicle_id = "5YJTEST0000000001"
    entry = hass.data["power_sync"][_Entry.entry_id]
    entry["ev_recovered_ownership"] = {
        vehicle_id: {"owner": "powersync", "owner_mode": "solar_surplus"}
    }
    entry["ev_recovered_ownership_saved_at"] = datetime.now(timezone.utc).isoformat()

    claimed = ev_ownership.ensure_external_ev_ownership(
        hass,
        _Entry(),
        vehicle_id,
        observed_at=datetime.now(timezone.utc),
    )

    assert claimed is False
    assert ev_ownership.get_ev_ownership(hass, _Entry(), vehicle_id) == (None, None)
