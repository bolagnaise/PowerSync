"""Focused durability tests for finite manual EV cleanup obligations."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"

_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps

_automations = types.ModuleType("power_sync.automations")
_automations.__path__ = [str(ROOT / "automations")]
sys.modules["power_sync.automations"] = _automations

_ha = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
_ha_helpers = sys.modules.setdefault(
    "homeassistant.helpers",
    types.ModuleType("homeassistant.helpers"),
)
_ha_event = sys.modules.setdefault(
    "homeassistant.helpers.event",
    types.ModuleType("homeassistant.helpers.event"),
)
setattr(_ha, "helpers", _ha_helpers)
setattr(_ha_helpers, "event", _ha_event)
# Keep the lightweight Home Assistant stub patchable by ``monkeypatch``.
_ha_event.async_track_point_in_utc_time = lambda *_args, **_kwargs: None

ev_ownership = importlib.import_module("power_sync.automations.ev_ownership")


class _Entry:
    entry_id = "entry-1"
    data = {}
    options = {}


class _Store:
    def __init__(self, data=None):
        self._data = data or {}
        self.saved = 0

    async def async_save(self):
        self.saved += 1


class _Hass:
    def __init__(self, store):
        self.data = {
            "power_sync": {
                "entry-1": {"automation_store": store},
            }
        }
        self.created_tasks = []

    def async_create_task(self, coro):
        self.created_tasks.append(coro)


def _close_created_tasks(hass):
    for task in hass.created_tasks:
        task.close()


def test_finite_obligation_is_persisted_before_cleanup_timer_and_keeps_original_deadline(
    monkeypatch,
):
    scheduled = []

    def track(_hass, callback, when):
        cancelled = []

        def cancel():
            cancelled.append(True)

        scheduled.append((callback, when, cancelled))
        return cancel

    monkeypatch.setattr(_ha_event, "async_track_point_in_utc_time", track)
    now = datetime(2026, 11, 1, 23, 0, tzinfo=timezone.utc)
    deadline = now + timedelta(minutes=3)
    store = _Store()
    hass = _Hass(store)

    record = asyncio.run(
        ev_ownership.async_begin_ev_quick_control_obligation(
            hass,
            _Entry(),
            "VIN-A",
            {
                "vehicle_vin": "VIN-A",
                "charger_type": "tesla",
                "quick_control": True,
                "duration_minutes": 3,
                "expires_at": deadline.isoformat(),
                "_boost_start_deadline": deadline.timestamp(),
            },
        )
    )

    assert record is not None
    assert record["expires_at"] == deadline.isoformat()
    assert store.saved == 1
    assert store._data["ev_runtime_state"]["ev_quick_control_obligations"]["VIN-A"][
        "expires_at"
    ] == deadline.isoformat()
    assert scheduled[0][1] == deadline
    _close_created_tasks(hass)


def test_expiry_callback_uses_exact_vehicle_cleanup_and_clears_only_after_terminal_stop(
    monkeypatch,
):
    callbacks = []
    timers = []

    def track(_hass, callback, when):
        callbacks.append((callback, when))

        def cancel():
            timers.append(when)

        return cancel

    monkeypatch.setattr(_ha_event, "async_track_point_in_utc_time", track)
    actions = types.ModuleType("power_sync.automations.actions")
    stop = AsyncMock(return_value=True)
    actions._action_stop_ev_charging_dynamic = stop
    monkeypatch.setitem(sys.modules, "power_sync.automations.actions", actions)
    deadline = datetime.now(timezone.utc) + timedelta(minutes=2)
    store = _Store()
    hass = _Hass(store)

    record = asyncio.run(
        ev_ownership.async_begin_ev_quick_control_obligation(
            hass,
            _Entry(),
            "VIN-A",
            {
                "vehicle_vin": "VIN-A",
                "charger_type": "tesla",
                "quick_control": True,
                "expires_at": deadline.isoformat(),
                "_boost_start_deadline": deadline.timestamp(),
            },
        )
    )
    assert record is not None

    asyncio.run(callbacks[0][0](deadline))

    stop.assert_awaited_once()
    cleanup_params = stop.await_args.args[2]
    assert cleanup_params["vehicle_id"] == "VIN-A"
    assert cleanup_params["vehicle_vin"] == "VIN-A"
    assert cleanup_params["stop_untracked"] is True
    assert cleanup_params["_force_tesla_stop_request"] is True
    assert cleanup_params["_quick_control_obligation_id"] == record[
        "obligation_id"
    ]
    assert "VIN-A" not in hass.data["power_sync"]["entry-1"].get(
        "ev_quick_control_obligations", {}
    )
    assert timers
    _close_created_tasks(hass)


def test_failed_expiry_stop_retries_without_moving_original_deadline(monkeypatch):
    scheduled = []

    def track(_hass, callback, when):
        scheduled.append((callback, when))
        return lambda: None

    monkeypatch.setattr(_ha_event, "async_track_point_in_utc_time", track)
    actions = types.ModuleType("power_sync.automations.actions")
    stop = AsyncMock(return_value=False)
    actions._action_stop_ev_charging_dynamic = stop
    monkeypatch.setitem(sys.modules, "power_sync.automations.actions", actions)
    # Exercise the retry branch as it runs at/after the original expiry.
    deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
    store = _Store()
    hass = _Hass(store)

    record = asyncio.run(
        ev_ownership.async_begin_ev_quick_control_obligation(
            hass,
            _Entry(),
            "VIN-A",
            {
                "vehicle_vin": "VIN-A",
                "charger_type": "tesla",
                "quick_control": True,
                "expires_at": deadline.isoformat(),
                "_boost_start_deadline": deadline.timestamp(),
            },
        )
    )
    asyncio.run(scheduled[0][0](deadline))

    assert stop.await_count == 1
    assert len(scheduled) == 2
    assert scheduled[1][1] > deadline
    assert scheduled[1][1] != datetime.fromisoformat(record["expires_at"])
    retained = ev_ownership.get_ev_quick_control_obligation(
        hass,
        _Entry(),
        "VIN-A",
    )
    assert retained["expires_at"] == record["expires_at"]
    assert retained["retry_count"] == 1
    _close_created_tasks(hass)


def test_old_obligation_token_cannot_clear_replacement_session(monkeypatch):
    monkeypatch.setattr(
        _ha_event,
        "async_track_point_in_utc_time",
        lambda *_args, **_kwargs: (lambda: None),
    )
    deadline = datetime.now(timezone.utc) + timedelta(minutes=2)
    hass = _Hass(_Store())
    params = {
        "vehicle_vin": "VIN-A",
        "charger_type": "tesla",
        "quick_control": True,
        "expires_at": deadline.isoformat(),
        "_boost_start_deadline": deadline.timestamp(),
    }
    first = asyncio.run(
        ev_ownership.async_begin_ev_quick_control_obligation(
            hass, _Entry(), "VIN-A", params
        )
    )
    second = asyncio.run(
        ev_ownership.async_begin_ev_quick_control_obligation(
            hass, _Entry(), "VIN-A", params
        )
    )
    assert first["obligation_id"] != second["obligation_id"]
    assert not ev_ownership.clear_ev_quick_control_obligation(
        hass,
        _Entry(),
        "VIN-A",
        expected_token=first["obligation_id"],
    )
    retained = ev_ownership.get_ev_quick_control_obligation(
        hass, _Entry(), "VIN-A"
    )
    assert retained["obligation_id"] == second["obligation_id"]
    _close_created_tasks(hass)


def test_replacement_save_cancellation_restores_previous_timer_and_record(monkeypatch):
    scheduled = []

    def track(_hass, callback, when):
        cancelled = []

        def cancel():
            cancelled.append(True)

        scheduled.append((callback, when, cancelled))
        return cancel

    monkeypatch.setattr(_ha_event, "async_track_point_in_utc_time", track)
    old_deadline = datetime.now(timezone.utc) + timedelta(minutes=2)
    new_deadline = old_deadline + timedelta(minutes=5)
    hass = _Hass(_Store())
    params = {
        "vehicle_vin": "VIN-A",
        "charger_type": "tesla",
        "quick_control": True,
        "expires_at": old_deadline.isoformat(),
        "_boost_start_deadline": old_deadline.timestamp(),
    }
    old = asyncio.run(
        ev_ownership.async_begin_ev_quick_control_obligation(
            hass, _Entry(), "VIN-A", params
        )
    )

    async def cancelled_persist(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(ev_ownership, "persist_ev_runtime_state", cancelled_persist)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            ev_ownership.async_begin_ev_quick_control_obligation(
                hass,
                _Entry(),
                "VIN-A",
                {**params, "expires_at": new_deadline.isoformat(), "_boost_start_deadline": new_deadline.timestamp()},
            )
        )
    retained = ev_ownership.get_ev_quick_control_obligation(hass, _Entry(), "VIN-A")
    assert retained["obligation_id"] == old["obligation_id"]
    assert retained["expires_at"] == old["expires_at"]
    assert scheduled[-1][1] == old_deadline
    _close_created_tasks(hass)


def test_reload_restores_obligation_without_renewing_deadline(monkeypatch):
    scheduled = []

    def track(_hass, _callback, when):
        scheduled.append(when)
        return lambda: None

    monkeypatch.setattr(_ha_event, "async_track_point_in_utc_time", track)
    deadline = datetime.now(timezone.utc) + timedelta(minutes=4)
    store = _Store(
        {
            "ev_runtime_state": {
                "ev_quick_control_obligations": {
                    "VIN-A": {
                        "vehicle_id": "VIN-A",
                        "expires_at": deadline.isoformat(),
                        "_boost_start_deadline": deadline.timestamp(),
                        "params": {
                            "vehicle_vin": "VIN-A",
                            "charger_type": "tesla",
                        },
                    }
                }
            }
        }
    )
    hass = _Hass(store)
    restored = ev_ownership.restore_ev_runtime_state(hass, _Entry(), store)
    obligations = restored["ev_quick_control_obligations"]
    assert obligations["VIN-A"]["expires_at"] == deadline.isoformat()

    ev_ownership.arm_ev_quick_control_obligation(
        hass,
        _Entry(),
        "VIN-A",
        obligations["VIN-A"],
    )

    assert scheduled == [deadline]
    _close_created_tasks(hass)


@pytest.mark.parametrize("params", [{}, "malformed"])
def test_malformed_quick_obligation_is_retained_but_not_armed(monkeypatch, params):
    """Malformed persisted cleanup evidence cannot become an executable Stop."""
    scheduled = []

    monkeypatch.setattr(
        _ha_event,
        "async_track_point_in_utc_time",
        lambda *_args, **_kwargs: scheduled.append(True) or (lambda: None),
    )
    deadline = datetime.now(timezone.utc) + timedelta(minutes=2)
    record = {
        "vehicle_id": "VIN-A",
        "obligation_id": "malformed",
        "expires_at": deadline.isoformat(),
        "params": params,
    }
    store = _Store({"ev_runtime_state": {"ev_quick_control_obligations": {"VIN-A": record}}})
    hass = _Hass(store)
    restored = ev_ownership.restore_ev_runtime_state(hass, _Entry(), store)
    assert restored["ev_quick_control_obligations"]["VIN-A"] == record
    assert ev_ownership.arm_ev_quick_control_obligation(
        hass, _Entry(), "VIN-A", record
    ) is None
    assert scheduled == []


def test_expiry_callback_is_inert_after_runtime_store_replacement(monkeypatch):
    """A callback from an old setup cannot act on a reloaded runtime."""
    callbacks = []

    def track(_hass, callback, when):
        callbacks.append(callback)
        return lambda: None

    monkeypatch.setattr(_ha_event, "async_track_point_in_utc_time", track)
    actions = types.ModuleType("power_sync.automations.actions")
    stop = AsyncMock(return_value=True)
    actions._action_stop_ev_charging_dynamic = stop
    monkeypatch.setitem(sys.modules, "power_sync.automations.actions", actions)

    deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
    store = _Store()
    hass = _Hass(store)
    record = asyncio.run(
        ev_ownership.async_begin_ev_quick_control_obligation(
            hass,
            _Entry(),
            "VIN-A",
            {
                "vehicle_vin": "VIN-A",
                "charger_type": "tesla",
                "quick_control": True,
                "expires_at": deadline.isoformat(),
                "_boost_start_deadline": deadline.timestamp(),
            },
        )
    )
    assert record is not None

    # Simulate HA replacing the config-entry runtime while retaining the
    # persisted token. The callback captured the old entry/store scope.
    hass.data["power_sync"]["entry-1"] = {
        "automation_store": _Store(),
        "ev_quick_control_obligations": {
            "VIN-A": record,
        },
    }
    asyncio.run(callbacks[0](deadline))

    stop.assert_not_awaited()
    _close_created_tasks(hass)
