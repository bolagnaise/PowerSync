"""Focused regressions for bounded Tesla capability acquisition."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"
VIN = "5YJTEST00000000A1"
SERIAL = "WC-SERIAL-A"
VIN_B = "5YJTEST00000000B2"


def _load_module():
    package = types.ModuleType("capability_test_power_sync")
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
    automations = types.ModuleType(f"{package.__name__}.automations")
    automations.__path__ = [str(ROOT / "automations")]
    sys.modules[automations.__name__] = automations
    const = types.ModuleType(f"{package.__name__}.const")
    const.CONF_MONITORING_MODE = "monitoring_mode"
    const.DOMAIN = "power_sync"
    sys.modules[const.__name__] = const
    return importlib.import_module(
        f"{package.__name__}.automations.ev_capability_refresh"
    )


refresh = _load_module()


class _Entry:
    entry_id = "entry-1"
    data = {}
    options = {}


class _Hass:
    def __init__(self) -> None:
        self.data = {"power_sync": {"entry-1": {}}}
        self.tasks: list[asyncio.Task] = []

    def async_create_task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task


def _capability(*, stale: bool, serial: str = SERIAL, connected_at: float = 1.0):
    return {
        "association_known": True,
        "capability_known": True,
        "max_charge_amps": 10 if stale else 32,
        "max_charge_amps_source": "active_wall_connector_vehicle",
        "voltage": 240,
        "phases": 1,
        "active_wall_connector_serial": serial,
        "wall_connector_connected_observed_at": connected_at,
        "capability_refresh_required": stale,
    }


def _install_runtime(monkeypatch, *, resolver, external_takeover: bool = False):
    actions = types.ModuleType(f"{refresh.__package__}.actions")
    actions._wake_tesla_ev = AsyncMock(return_value=True)
    actions._set_vehicle_amps = AsyncMock(return_value=True)
    actions._action_start_ev_charging = AsyncMock(return_value=True)
    actions._action_stop_ev_charging = AsyncMock(return_value=True)
    actions._tesla_physical_charging_snapshot = lambda *_args, **_kwargs: {
        "charging": False,
        "measurements": frozenset(),
    }
    ownership: dict[str, dict] = {}

    async def confirm(*_args, **_kwargs):
        if external_takeover:
            ownership[VIN] = {
                "owner_mode": "external",
                "session_id": "external-session",
            }
        return True, "sensor.current=5.0A"

    actions._wait_for_tesla_physical_start = AsyncMock(side_effect=confirm)
    sys.modules[actions.__name__] = actions

    owners = types.ModuleType(f"{refresh.__package__}.ev_ownership")
    owners.persist_ev_runtime_state = AsyncMock(return_value=None)
    owners.can_claim_ev_ownership = (
        lambda *_args, **_kwargs: (True, None, None, None)
    )

    def claim(_hass, _entry, vehicle_id, **kwargs):
        ownership[vehicle_id] = dict(kwargs)
        return ownership[vehicle_id]

    def get(_hass, _entry, vehicle_id):
        lease = ownership.get(vehicle_id)
        return (vehicle_id, lease) if lease else (None, None)

    def release(_hass, _entry, vehicle_id, **_kwargs):
        return ownership.pop(vehicle_id, None)

    owners.claim_ev_ownership = claim
    owners.get_ev_ownership = get
    owners.release_ev_ownership = release
    sys.modules[owners.__name__] = owners

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(refresh.asyncio, "sleep", no_wait)
    return actions, owners, ownership


def test_probe_refreshes_once_and_stops_only_its_exact_vin(monkeypatch):
    async def scenario():
        hass = _Hass()
        resolver = AsyncMock(
            side_effect=[
                _capability(stale=True),
                _capability(stale=True),
                _capability(stale=False),
            ]
        )
        actions, _owners, _ownership = _install_runtime(
            monkeypatch, resolver=resolver
        )
        invalidated: list[str] = []
        coordinator = refresh.TeslaCapabilityRefreshCoordinator(
            hass,
            _Entry(),
            resolve_capability=resolver,
            is_eligible=AsyncMock(return_value=True),
            invalidate_plan=invalidated.append,
        )
        started = await coordinator.request(
            vehicle_id="1",
            vehicle_vin=VIN,
            capability=_capability(stale=True),
            configured_max_amps=32,
            min_charge_amps=5,
            voltage=240,
            phases=1,
        )
        assert started is True
        await asyncio.gather(*hass.tasks)

        actions._wake_tesla_ev.assert_awaited_once()
        actions._action_start_ev_charging.assert_awaited_once()
        actions._action_stop_ev_charging.assert_awaited_once()
        stop_params = actions._action_stop_ev_charging.await_args.args[2]
        assert stop_params["vehicle_vin"] == VIN
        assert stop_params["_force_tesla_stop_request"] is True
        assert invalidated == ["1"]
        record = next(iter(coordinator._records.values()))
        assert record["phase"] == "fresh"
        assert record["start_confirmed"] is True
        assert record["stop_required"] is False

        repeated = await coordinator.request(
            vehicle_id="1",
            vehicle_vin=VIN,
            capability=_capability(stale=True),
            configured_max_amps=32,
            min_charge_amps=5,
            voltage=240,
            phases=1,
        )
        assert repeated is False

    asyncio.run(scenario())


def test_wake_refresh_avoids_energizing_probe(monkeypatch):
    async def scenario():
        hass = _Hass()
        resolver = AsyncMock(
            side_effect=[_capability(stale=True), _capability(stale=False)]
        )
        actions, _owners, _ownership = _install_runtime(
            monkeypatch, resolver=resolver
        )
        invalidated: list[str] = []
        coordinator = refresh.TeslaCapabilityRefreshCoordinator(
            hass,
            _Entry(),
            resolve_capability=resolver,
            is_eligible=AsyncMock(return_value=True),
            invalidate_plan=invalidated.append,
        )
        assert await coordinator.request(
            vehicle_id="1",
            vehicle_vin=VIN,
            capability=_capability(stale=True),
            configured_max_amps=32,
            min_charge_amps=5,
            voltage=240,
            phases=1,
        )
        await asyncio.gather(*hass.tasks)

        actions._wake_tesla_ev.assert_awaited_once()
        actions._action_start_ev_charging.assert_not_awaited()
        actions._action_stop_ev_charging.assert_not_awaited()
        assert invalidated == ["1"]

    asyncio.run(scenario())


def test_external_takeover_suppresses_probe_stop(monkeypatch):
    async def scenario():
        hass = _Hass()
        resolver = AsyncMock(
            side_effect=[
                _capability(stale=True),
                _capability(stale=True),
                _capability(stale=False),
            ]
        )
        actions, _owners, ownership = _install_runtime(
            monkeypatch,
            resolver=resolver,
            external_takeover=True,
        )
        coordinator = refresh.TeslaCapabilityRefreshCoordinator(
            hass,
            _Entry(),
            resolve_capability=resolver,
            is_eligible=AsyncMock(return_value=True),
            invalidate_plan=lambda _vehicle: None,
        )
        assert await coordinator.request(
            vehicle_id="1",
            vehicle_vin=VIN,
            capability=_capability(stale=True),
            configured_max_amps=32,
            min_charge_amps=5,
            voltage=240,
            phases=1,
        )
        await asyncio.gather(*hass.tasks)

        actions._action_stop_ev_charging.assert_not_awaited()
        assert ownership[VIN]["owner_mode"] == "external"
        record = next(iter(coordinator._records.values()))
        assert record["phase"] == "cancelled"
        assert record["stop_required"] is False

    asyncio.run(scenario())


def test_two_wall_connectors_refresh_independently(monkeypatch):
    async def scenario():
        hass = _Hass()
        calls = {VIN: 0, VIN_B: 0}

        async def resolve(vin):
            calls[vin] += 1
            serial = SERIAL if vin == VIN else "WC-SERIAL-B"
            return _capability(
                stale=calls[vin] < 3,
                serial=serial,
                connected_at=1.0 if vin == VIN else 2.0,
            )

        actions, _owners, _ownership = _install_runtime(
            monkeypatch, resolver=resolve
        )
        invalidated: list[str] = []
        coordinator = refresh.TeslaCapabilityRefreshCoordinator(
            hass,
            _Entry(),
            resolve_capability=resolve,
            is_eligible=AsyncMock(return_value=True),
            invalidate_plan=invalidated.append,
        )
        requests = (
            ("1", VIN, SERIAL, 1.0),
            ("2", VIN_B, "WC-SERIAL-B", 2.0),
        )
        for vehicle_id, vin, serial, connected_at in requests:
            assert await coordinator.request(
                vehicle_id=vehicle_id,
                vehicle_vin=vin,
                capability=_capability(
                    stale=True,
                    serial=serial,
                    connected_at=connected_at,
                ),
                configured_max_amps=32,
                min_charge_amps=5,
                voltage=240,
                phases=1,
            )
        await asyncio.gather(*hass.tasks)

        assert actions._action_start_ev_charging.await_count == 2
        assert actions._action_stop_ev_charging.await_count == 2
        stopped_vins = {
            call.args[2]["vehicle_vin"]
            for call in actions._action_stop_ev_charging.await_args_list
        }
        assert stopped_vins == {VIN, VIN_B}
        assert sorted(invalidated) == ["1", "2"]

    asyncio.run(scenario())


def test_failed_probe_start_never_sends_compensating_stop(monkeypatch):
    async def scenario():
        hass = _Hass()
        resolver = AsyncMock(
            side_effect=[_capability(stale=True), _capability(stale=True)]
        )
        actions, _owners, _ownership = _install_runtime(
            monkeypatch, resolver=resolver
        )
        actions._action_start_ev_charging.return_value = False
        coordinator = refresh.TeslaCapabilityRefreshCoordinator(
            hass,
            _Entry(),
            resolve_capability=resolver,
            is_eligible=AsyncMock(return_value=True),
            invalidate_plan=lambda _vehicle: None,
        )
        assert await coordinator.request(
            vehicle_id="1",
            vehicle_vin=VIN,
            capability=_capability(stale=True),
            configured_max_amps=32,
            min_charge_amps=5,
            voltage=240,
            phases=1,
        )
        await asyncio.gather(*hass.tasks)

        actions._action_start_ev_charging.assert_awaited_once()
        actions._action_stop_ev_charging.assert_not_awaited()
        record = next(iter(coordinator._records.values()))
        assert record["phase"] == "failed"
        assert record["start_confirmed"] is False
        assert record["stop_required"] is False

    asyncio.run(scenario())


def test_restart_recovers_only_fresh_physical_probe_evidence(monkeypatch):
    async def scenario():
        hass = _Hass()
        episode_key = f"{VIN}|{SERIAL}|1.000000"
        hass.data["power_sync"]["entry-1"]["ev_capability_refresh_records"] = {
            episode_key: {
                "vehicle_id": "1",
                "vin": VIN,
                "connector_serial": SERIAL,
                "connected_observed_at": 1.0,
                "phase": "probing",
                "start_command_pending": False,
                "start_command_at": "2026-08-25T00:00:00+00:00",
                "start_issued": True,
                "start_confirmed": False,
                "stop_required": False,
            }
        }
        resolver = AsyncMock(return_value=_capability(stale=True))
        actions, _owners, _ownership = _install_runtime(
            monkeypatch, resolver=resolver
        )
        actions._tesla_physical_charging_snapshot = (
            lambda *_args, **_kwargs: {
                "charging": True,
                "measurements": frozenset({"sensor.current=5.0A"}),
                "fresh_measurements": frozenset({"sensor.current=5.0A"}),
                "fresh_direct_measurements": frozenset(),
            }
        )
        coordinator = refresh.TeslaCapabilityRefreshCoordinator(
            hass,
            _Entry(),
            resolve_capability=resolver,
            is_eligible=AsyncMock(return_value=False),
            invalidate_plan=lambda _vehicle: None,
        )
        assert await coordinator.request(
            vehicle_id="1",
            vehicle_vin=VIN,
            capability=_capability(stale=True),
            configured_max_amps=32,
            min_charge_amps=5,
            voltage=240,
            phases=1,
        )
        await asyncio.gather(*hass.tasks)

        actions._action_stop_ev_charging.assert_awaited_once()
        stop_params = actions._action_stop_ev_charging.await_args.args[2]
        assert stop_params["vehicle_vin"] == VIN
        assert stop_params["_force_tesla_stop_request"] is True
        record = coordinator._records[episode_key]
        assert record["phase"] == "cancelled"
        assert record["stop_required"] is False

    asyncio.run(scenario())


def test_monitoring_or_ambiguous_connector_never_starts_refresh(monkeypatch):
    async def scenario():
        hass = _Hass()
        entry = SimpleNamespace(
            entry_id="entry-1",
            data={},
            options={"monitoring_mode": True},
        )
        coordinator = refresh.TeslaCapabilityRefreshCoordinator(
            hass,
            entry,
            resolve_capability=AsyncMock(),
            is_eligible=AsyncMock(return_value=True),
            invalidate_plan=lambda _vehicle: None,
        )
        assert not await coordinator.request(
            vehicle_id="1",
            vehicle_vin=VIN,
            capability=_capability(stale=True),
            configured_max_amps=32,
            min_charge_amps=5,
            voltage=240,
            phases=1,
        )
        entry.options = {}
        ambiguous = _capability(stale=True)
        ambiguous.pop("active_wall_connector_serial")
        assert not await coordinator.request(
            vehicle_id="1",
            vehicle_vin=VIN,
            capability=ambiguous,
            configured_max_amps=32,
            min_charge_amps=5,
            voltage=240,
            phases=1,
        )
        assert hass.tasks == []

    asyncio.run(scenario())
