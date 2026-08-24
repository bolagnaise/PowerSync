"""Tests for observed Tesla charging session tracking."""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"
VIN = "5YJTEST0000000001"
VIN_2 = "LRWTEST0000000002"
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
from power_sync.automations.ev_ownership import (  # noqa: E402
    can_claim_ev_ownership,
    claim_ev_ownership,
    get_ev_last_commands,
    get_ev_ownership,
    record_ev_command,
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
        self.data = {
            "power_sync": {
                "entry-1": {
                    "tesla_coordinator": _Coordinator(),
                    "tariff_schedule": {
                        "buy_price": 0.0,
                        "sell_price": 5.0,
                    },
                },
            },
        }


class _Session:
    def __init__(self, vehicle_id: str, mode: str) -> None:
        self.vehicle_id = vehicle_id
        self.mode = mode
        self.id = f"session-{vehicle_id}-{mode}"


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
        self.updated.append(
            (
                vehicle_id,
                power_kw,
                amps,
                is_solar,
                import_price_cents,
                export_price_cents,
                battery_soc,
            )
        )
        return self.active_sessions.get(vehicle_id)

    async def end_session(self, vehicle_id, reason, end_soc=None):
        session = self.active_sessions.pop(vehicle_id, None)
        self.ended.append((vehicle_id, reason, end_soc))
        return session


def _tracker(manager, vehicles, hass=None):
    return ObservedTeslaSessionTracker(
        hass or _Hass(),
        _Entry(),
        manager,
        lambda _hass, _entry: vehicles,
    )


def test_observed_tesla_charge_starts_and_updates_session():
    manager = _SessionManager()
    hass = _Hass()
    tracker = _tracker(
        manager,
        [{
            "vehicle_id": VIN,
            "vehicle_name": "Tessa",
            "ev_power_kw": 10.9,
            "ev_soc": 70,
            "is_charging": True,
            "is_connected": True,
        }],
        hass,
    )

    asyncio.run(tracker.poll())

    assert manager.started == [(VIN, OBSERVED_SESSION_MODE, 70, None)]
    assert manager.updated == [
        (VIN, 10.9, 0, False, 0.0, 5.0, 70)
    ]
    lease_id, lease = get_ev_ownership(hass, _Entry(), VIN)
    assert lease_id == VIN
    assert lease["owner"] == "external"
    assert lease["owner_mode"] == "external"
    assert lease["session_id"] == f"session-{VIN}-observed"
    assert get_ev_last_commands(hass, _Entry()) == {}


def test_observed_tesla_charge_does_not_duplicate_powersync_session():
    manager = _SessionManager()
    hass = _Hass()
    manager.active_sessions[VIN] = _Session(
        VIN,
        "solar_surplus",
    )
    tracker = _tracker(
        manager,
        [{
            "vehicle_id": VIN,
            "ev_power_kw": 10.9,
            "is_charging": True,
        }],
        hass,
    )
    claim_ev_ownership(
        hass,
        _Entry(),
        VIN,
        owner_mode="solar_surplus",
    )

    asyncio.run(tracker.poll())

    assert manager.started == []
    assert manager.updated == []
    assert manager.ended == []
    assert get_ev_ownership(hass, _Entry(), VIN)[1]["owner_mode"] == "solar_surplus"


def test_observed_tesla_charge_ends_after_idle_confirmation():
    manager = _SessionManager()
    hass = _Hass()
    vehicles = [{
        "vehicle_id": VIN,
        "ev_power_kw": 10.9,
        "ev_soc": 70,
        "is_charging": True,
        "is_connected": True,
    }]
    tracker = _tracker(manager, vehicles, hass)

    asyncio.run(tracker.poll())
    vehicles[0].update({
        "ev_power_kw": 0.0,
        "ev_soc": 72,
        "is_charging": False,
        "is_connected": True,
    })

    asyncio.run(tracker.poll())
    assert manager.ended == []

    asyncio.run(tracker.poll())
    assert manager.ended == [(VIN, "observed_charge_stopped", 72)]
    assert get_ev_ownership(hass, _Entry(), VIN)[1]["owner"] == "external"

    vehicles[0]["is_connected"] = False
    asyncio.run(tracker.poll())
    assert get_ev_ownership(hass, _Entry(), VIN) == (None, None)


def test_delayed_charge_readback_after_powersync_stop_does_not_stick_external():
    """A settled PowerSync stop must not poison the rest of the plug session."""
    manager = _SessionManager()
    hass = _Hass()
    vehicles = [{
        "vehicle_id": VIN,
        "ev_power_kw": 3.8,
        "ev_soc": 66,
        "is_charging": True,
        "is_connected": True,
    }]
    tracker = _tracker(manager, vehicles, hass)
    command = record_ev_command(
        hass,
        _Entry(),
        VIN,
        command="stop",
        success=True,
        reason="HA restart teardown",
    )
    vehicles[0]["_charging_observed_at"] = (
        datetime.fromisoformat(command["at"]) + timedelta(seconds=1)
    ).isoformat()

    # TESSY can continue echoing Charging for several minutes after the local
    # charger has accepted PowerSync's stop.  Protect that ambiguous interval,
    # but do not turn it into a sticky external lease.
    asyncio.run(tracker.poll())
    _lease_id, lease = get_ev_ownership(hass, _Entry(), VIN)
    assert lease["owner"] == "external"
    assert lease["stop_settling"] is True

    vehicles[0].update({
        "ev_power_kw": 0.0,
        "is_charging": False,
    })
    asyncio.run(tracker.poll())
    assert get_ev_ownership(hass, _Entry(), VIN)[1]["stop_settling"] is True
    asyncio.run(tracker.poll())

    assert get_ev_ownership(hass, _Entry(), VIN) == (None, None)
    allowed, _lease_id, _lease, reason = can_claim_ev_ownership(
        hass,
        _Entry(),
        VIN,
        owner_mode="smart_schedule",
        allow_takeover=True,
    )
    assert allowed is True
    assert reason is None


def test_external_tesla_ownership_is_vin_scoped():
    manager = _SessionManager()
    hass = _Hass()
    tracker = _tracker(
        manager,
        [
            {
                "vehicle_id": VIN,
                "ev_power_kw": 7.2,
                "is_charging": True,
                "is_connected": True,
            },
            {
                "vehicle_id": VIN_2,
                "ev_power_kw": 0.0,
                "is_charging": False,
                "is_connected": True,
            },
        ],
        hass,
    )

    asyncio.run(tracker.poll())

    assert get_ev_ownership(hass, _Entry(), VIN)[1]["owner"] == "external"
    assert get_ev_ownership(hass, _Entry(), VIN_2) == (None, None)


def test_generic_observation_does_not_claim_tesla_command_ownership():
    manager = _SessionManager()
    hass = _Hass()
    tracker = _tracker(
        manager,
        [{
            "vehicle_id": "generic_ev",
            "ev_power_kw": 7.2,
            "is_charging": True,
            "is_connected": True,
        }],
        hass,
    )

    asyncio.run(tracker.poll())

    assert get_ev_ownership(hass, _Entry(), "generic_ev") == (None, None)
