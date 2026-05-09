"""Tests for observed Tesla charging session tracking."""

from __future__ import annotations

import asyncio
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

from power_sync.automations.observed_tesla_sessions import (  # noqa: E402
    OBSERVED_SESSION_MODE,
    ObservedTeslaSessionTracker,
)


class _Entry:
    entry_id = "entry-1"


class _Coordinator:
    def __init__(self) -> None:
        self.data = {
            "grid_power": 12.0,
            "solar_power": 9.0,
        }


class _Hass:
    def __init__(self) -> None:
        self.data = {"power_sync": {"entry-1": {"tesla_coordinator": _Coordinator()}}}


class _Session:
    def __init__(self, vehicle_id: str, mode: str) -> None:
        self.vehicle_id = vehicle_id
        self.mode = mode


class _SessionManager:
    def __init__(self) -> None:
        self.active_sessions = {}
        self.started = []
        self.updated = []
        self.ended = []

    async def start_session(self, vehicle_id, mode, start_soc=None, target_soc=None):
        session = _Session(vehicle_id, mode)
        self.active_sessions[vehicle_id] = session
        self.started.append((vehicle_id, mode, start_soc, target_soc))
        return session

    async def update_session(
        self,
        vehicle_id,
        power_kw,
        amps,
        is_solar,
        import_price_cents=30.0,
        export_price_cents=8.0,
        battery_soc=None,
    ):
        self.updated.append((vehicle_id, power_kw, amps, is_solar, battery_soc))
        return self.active_sessions.get(vehicle_id)

    async def end_session(self, vehicle_id, reason, end_soc=None):
        session = self.active_sessions.pop(vehicle_id, None)
        self.ended.append((vehicle_id, reason, end_soc))
        return session


def _tracker(manager, vehicles):
    return ObservedTeslaSessionTracker(
        _Hass(),
        _Entry(),
        manager,
        lambda _hass, _entry: vehicles,
    )


def test_observed_tesla_charge_starts_and_updates_session():
    manager = _SessionManager()
    tracker = _tracker(
        manager,
        [{
            "vehicle_id": "LRW3TESTVIN12345",
            "vehicle_name": "Tessa",
            "ev_power_kw": 10.9,
            "ev_soc": 70,
            "is_charging": True,
        }],
    )

    asyncio.run(tracker.poll())

    assert manager.started == [("LRW3TESTVIN12345", OBSERVED_SESSION_MODE, 70, None)]
    assert manager.updated == [("LRW3TESTVIN12345", 10.9, 0, False, 70)]


def test_observed_tesla_charge_does_not_duplicate_powersync_session():
    manager = _SessionManager()
    manager.active_sessions["LRW3TESTVIN12345"] = _Session(
        "LRW3TESTVIN12345",
        "solar_surplus",
    )
    tracker = _tracker(
        manager,
        [{
            "vehicle_id": "LRW3TESTVIN12345",
            "ev_power_kw": 10.9,
            "is_charging": True,
        }],
    )

    asyncio.run(tracker.poll())

    assert manager.started == []
    assert manager.updated == []
    assert manager.ended == []


def test_observed_tesla_charge_ends_after_idle_confirmation():
    manager = _SessionManager()
    manager.active_sessions["LRW3TESTVIN12345"] = _Session(
        "LRW3TESTVIN12345",
        OBSERVED_SESSION_MODE,
    )
    tracker = _tracker(
        manager,
        [{
            "vehicle_id": "LRW3TESTVIN12345",
            "ev_power_kw": 0.0,
            "ev_soc": 72,
            "is_charging": False,
        }],
    )

    asyncio.run(tracker.poll())
    assert manager.ended == []

    asyncio.run(tracker.poll())
    assert manager.ended == [
        ("LRW3TESTVIN12345", "observed_charge_stopped", 72)
    ]
