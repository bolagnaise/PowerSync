"""Focused tests for the bounded exact-vehicle Tesla observation helper."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import math
from pathlib import Path
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"
package = sys.modules.setdefault("power_sync", types.ModuleType("power_sync"))
package.__path__ = [str(ROOT)]

from power_sync import tesla_ev_observation as observation  # noqa: E402


VIN_A = "5YJ3E1EA7NF0000A1"
VIN_B = "5YJ3E1EA7NF0000B2"


@pytest.fixture(autouse=True)
def stub_native_vehicle_sources(monkeypatch):
    """Keep source discovery explicit without duplicating production logic."""

    native = types.ModuleType("power_sync.native_rate_telemetry")

    def vehicle_sources(hass, vin):
        matches = []
        for domain in ("teslemetry", "tesla_fleet"):
            for entry in hass.config_entries.async_entries(domain):
                for vehicle in getattr(getattr(entry, "runtime_data", None), "vehicles", ()):
                    if str(getattr(vehicle, "vin", "")).upper() != str(vin).upper():
                        continue
                    coordinator = getattr(vehicle, "coordinator", None)
                    if coordinator is not None:
                        matches.append((domain, entry, vehicle, coordinator))
        return matches

    native.vehicle_sources = vehicle_sources
    monkeypatch.setitem(sys.modules, "power_sync.native_rate_telemetry", native)


class _State:
    def __init__(self, entity_id, value, stamp=None):
        self.entity_id = entity_id
        self.state = value
        self.last_reported = stamp
        self.last_updated = stamp


class _Coordinator:
    def __init__(self, data, refresh=None):
        self.data = data
        self.last_update_success = True
        self.refresh = refresh
        self.refreshes = 0

    async def async_request_refresh(self):
        self.refreshes += 1
        if self.refresh is not None:
            await self.refresh(self)


class _Hass:
    __slots__ = (
        "data",
        "states_by_id",
        "states",
        "config_entries",
        "entity_registry",
        "device_registry",
    )

    def __init__(self, entries, states=None, registrations=None):
        self.data = {}
        self.states_by_id = states or {}
        self.states = types.SimpleNamespace(
            get=self.states_by_id.get,
            async_all=lambda: list(self.states_by_id.values()),
        )
        self.config_entries = types.SimpleNamespace(
            async_entries=lambda domain: entries.get(domain, [])
        )
        self.entity_registry = types.SimpleNamespace(
            async_get=lambda entity_id: (registrations or {}).get(entity_id)
        )
        self.device_registry = types.SimpleNamespace(devices={})


def _entry(entry_id, vehicles):
    return types.SimpleNamespace(
        entry_id=entry_id,
        data={},
        options={},
        runtime_data=types.SimpleNamespace(vehicles=vehicles),
    )


def _vehicle(vin, coordinator, device_id, domain="tesla_fleet"):
    return types.SimpleNamespace(
        vin=vin,
        coordinator=coordinator,
        device=types.SimpleNamespace(
            id=device_id,
            identifiers={(domain, vin)},
        ),
    )


def _charge(timestamp, state="Stopped", current=0, power=0, limit=80, requested=16):
    return {
        "charge_state": {
            "timestamp": timestamp,
            "charging_state": state,
            "charger_actual_current": current,
            "charger_power": power,
            "charge_limit_soc": limit,
            # Pilot current is EVSE availability, not Tesla's requested rate.
            "charger_pilot_current": requested + 100,
            "charge_current_request": requested,
        }
    }


def _hass_for_cloud(vin_coordinators, registrations=None, states=None):
    vehicles = [
        _vehicle(vin, coordinator, device_id)
        for vin, coordinator, device_id in vin_coordinators
    ]
    entry = _entry("fleet-entry", vehicles)
    return _Hass({"tesla_fleet": [entry]}, states=states, registrations=registrations)


@pytest.mark.asyncio
async def test_exact_vin_and_preferred_entity_select_the_matching_provider():
    now = datetime.now(timezone.utc).timestamp()
    coordinator_a = _Coordinator(_charge(now - 1, current=2, power=1))
    coordinator_b = _Coordinator(_charge(now - 1, current=12, power=7))
    preferred = "sensor.car_b_charging_state"
    hass = _hass_for_cloud(
        [(VIN_A, coordinator_a, "device-a"), (VIN_B, coordinator_b, "device-b")],
        registrations={
            preferred: types.SimpleNamespace(
                config_entry_id="fleet-entry",
                device_id="device-b",
                platform="tesla_fleet",
                unique_id="b",
            )
        },
    )

    async def refresh(coord):
        charge = _charge(datetime.now(timezone.utc).timestamp(), current=12, power=7)
        coord.data = charge

    coordinator_b.refresh = refresh
    result = await observation.async_read_tesla_vehicle(
        hass, VIN_B, preferred_entity_id=preferred
    )

    assert result is not None
    assert result.vin == VIN_B
    assert result.domain == "tesla_fleet"
    assert result.device_id == "device-b"
    assert coordinator_a.refreshes == 0
    assert coordinator_b.refreshes == 1


def test_cached_fresh_observation_does_not_request_refresh():
    timestamp = math.floor(datetime.now(timezone.utc).timestamp()) - 1
    coordinator = _Coordinator(_charge(timestamp, state="Charging", current=11, power=6))
    hass = _hass_for_cloud([(VIN_A, coordinator, "device-a")])

    result = observation.get_tesla_vehicle_observation(hass, VIN_A)

    assert result is not None
    assert result.source_charge_timestamp == timestamp
    assert result.charge_state == "Charging"
    assert result.charger_actual_current == 11
    assert coordinator.refreshes == 0


def test_cached_old_observation_and_wrong_preferred_source_are_rejected():
    old = datetime.now(timezone.utc).timestamp() - 300
    coordinator = _Coordinator(_charge(old))
    preferred = "sensor.wrong_provider"
    hass = _hass_for_cloud(
        [(VIN_A, coordinator, "device-a")],
        registrations={
            preferred: types.SimpleNamespace(
                config_entry_id="another-entry",
                device_id="device-a",
                platform="tesla_fleet",
            )
        },
    )

    assert observation.get_tesla_vehicle_observation(hass, VIN_A) is None
    assert (
        observation.get_tesla_vehicle_observation(
            hass, VIN_A, preferred_entity_id=preferred
        )
        is None
    )
    assert coordinator.refreshes == 0


@pytest.mark.parametrize(
    "timestamp_factory",
    [
        lambda base: base,
        lambda base: str(base),
        lambda base: base * 1000,
    ],
)
def test_cached_observation_accepts_seconds_milliseconds_and_numeric_strings(timestamp_factory):
    base = math.floor(datetime.now(timezone.utc).timestamp()) - 1
    timestamp = timestamp_factory(base)
    coordinator = _Coordinator(_charge(timestamp, current=5, power=2))
    hass = _hass_for_cloud([(VIN_A, coordinator, "device-a")])

    result = observation.get_tesla_vehicle_observation(
        hass, VIN_A, updated_after=base - 1
    )

    assert result is not None
    assert result.source_charge_timestamp == base


def test_unchanged_but_fresh_raw_source_is_valid_without_cutoff_only():
    timestamp = math.floor(datetime.now(timezone.utc).timestamp()) - 1
    coordinator = _Coordinator(_charge(timestamp))
    hass = _hass_for_cloud([(VIN_A, coordinator, "device-a")])

    assert observation.get_tesla_vehicle_observation(hass, VIN_A) is not None
    assert (
        observation.get_tesla_vehicle_observation(
            hass, VIN_A, updated_after=timestamp
        )
        is None
    )
    assert (
        observation.get_tesla_vehicle_observation(
            hass, VIN_A, updated_after=timestamp - 1
        )
        is not None
    )


@pytest.mark.asyncio
async def test_preferred_native_entity_cannot_cross_provider_entry_or_device():
    first = _Coordinator(_charge(datetime.now(timezone.utc).timestamp() - 1))
    second = _Coordinator(_charge(datetime.now(timezone.utc).timestamp() - 1))
    first_entry = _entry("fleet-first", [_vehicle(VIN_A, first, "device-a")])
    second_entry = _entry("fleet-second", [_vehicle(VIN_A, second, "device-b")])
    preferred = "sensor.car_a_second"
    hass = _Hass(
        {"tesla_fleet": [first_entry, second_entry]},
        registrations={
            preferred: types.SimpleNamespace(
                config_entry_id="fleet-second",
                device_id="device-b",
                platform="tesla_fleet",
            )
        },
    )

    async def refresh(coord):
        coord.data = _charge(datetime.now(timezone.utc).timestamp(), current=9, power=5)

    second.refresh = refresh
    result = await observation.async_read_tesla_vehicle(
        hass, VIN_A, preferred_entity_id=preferred
    )

    assert result is not None
    assert result.config_entry_id == "fleet-second"
    assert first.refreshes == 0
    assert second.refreshes == 1


@pytest.mark.asyncio
async def test_bluetooth_commands_preference_resolves_only_native_teslemetry_source():
    native = _Coordinator(_charge(datetime.now(timezone.utc).timestamp() - 1))
    fleet = _Coordinator(_charge(datetime.now(timezone.utc).timestamp() - 1))
    native_entry = _entry(
        "teslemetry-entry", [_vehicle(VIN_A, native, "native-device", "teslemetry")]
    )
    fleet_entry = _entry(
        "fleet-entry", [_vehicle(VIN_A, fleet, "fleet-device", "tesla_fleet")]
    )
    preferred = "button.car_a_start_bluetooth"
    hass = _Hass(
        {"teslemetry": [native_entry], "tesla_fleet": [fleet_entry]},
        registrations={
            preferred: types.SimpleNamespace(
                config_entry_id="commands-entry",
                device_id="native-device",
                platform="teslemetry_bluetooth",
            )
        },
    )

    async def refresh(coord):
        coord.data = _charge(datetime.now(timezone.utc).timestamp(), current=3, power=2)

    native.refresh = refresh
    result = await observation.async_read_tesla_vehicle(
        hass, VIN_A, preferred_entity_id=preferred
    )

    assert result is not None
    assert result.domain == "teslemetry"
    assert native.refreshes == 1
    assert fleet.refreshes == 0


@pytest.mark.asyncio
async def test_stale_cached_response_does_not_pass_after_refresh_without_new_charge_timestamp():
    old = datetime.now(timezone.utc).timestamp() - 300
    coordinator = _Coordinator(_charge(old))
    hass = _hass_for_cloud([(VIN_A, coordinator, "device-a")])

    result = await observation.async_read_tesla_vehicle(hass, VIN_A)

    assert result is None
    assert coordinator.refreshes == 1


@pytest.mark.asyncio
async def test_unchanged_fresh_charge_timestamp_is_still_cached():
    now = datetime.now(timezone.utc).timestamp()
    coordinator = _Coordinator(_charge(now - 1))
    hass = _hass_for_cloud([(VIN_A, coordinator, "device-a")])

    result = await observation.async_read_tesla_vehicle(hass, VIN_A)

    assert result is None
    assert coordinator.refreshes == 1


@pytest.mark.asyncio
async def test_slowly_advanced_old_timestamp_is_rejected_as_before_request():
    old = math.floor(datetime.now(timezone.utc).timestamp()) - 89
    coordinator = _Coordinator(_charge(old))
    hass = _hass_for_cloud([(VIN_A, coordinator, "device-a")])

    async def refresh(coord):
        coord.data = _charge(old + 1)

    coordinator.refresh = refresh
    assert await observation.async_read_tesla_vehicle(hass, VIN_A) is None


@pytest.mark.asyncio
async def test_refresh_without_successful_coordinator_update_is_rejected():
    coordinator = _Coordinator(_charge(datetime.now(timezone.utc).timestamp() - 1))
    hass = _hass_for_cloud([(VIN_A, coordinator, "device-a")])

    async def refresh(coord):
        coord.last_update_success = False
        coord.data = _charge(datetime.now(timezone.utc).timestamp())

    coordinator.refresh = refresh
    assert await observation.async_read_tesla_vehicle(hass, VIN_A) is None


@pytest.mark.asyncio
async def test_selected_provider_failure_does_not_fall_through_to_other_provider():
    old = datetime.now(timezone.utc).timestamp() - 300
    teslemetry = _Coordinator(_charge(old))
    fleet = _Coordinator(_charge(old))
    entry_t = _entry("teslemetry-entry", [_vehicle(VIN_A, teslemetry, "device-a")])
    entry_f = _entry("fleet-entry", [_vehicle(VIN_A, fleet, "device-b")])
    hass = _Hass({"teslemetry": [entry_t], "tesla_fleet": [entry_f]})

    result = await observation.async_read_tesla_vehicle(hass, VIN_A)

    assert result is None
    assert teslemetry.refreshes == 1
    assert fleet.refreshes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "current", "power"),
    [("Charging", 16, 7.2), ("Stopped", 0, 0)],
)
async def test_positive_charging_and_zero_stopped_values_are_preserved(state, current, power):
    coordinator = _Coordinator(_charge(datetime.now(timezone.utc).timestamp() - 1))
    hass = _hass_for_cloud([(VIN_A, coordinator, "device-a")])

    async def refresh(coord):
        coord.data = _charge(
            datetime.now(timezone.utc).timestamp(),
            state=state,
            current=current,
            power=power,
        )

    coordinator.refresh = refresh
    result = await observation.async_read_tesla_vehicle(hass, VIN_A)

    assert result is not None
    assert result.charge_state == state
    assert result.charger_actual_current == current
    assert result.charger_power == power
    assert result.requested_current == 16
    assert result.charge_limit_soc == 80


@pytest.mark.asyncio
async def test_waiter_cancellation_does_not_cancel_shared_provider_read():
    started = asyncio.Event()
    release = asyncio.Event()
    coordinator = _Coordinator(_charge(datetime.now(timezone.utc).timestamp() - 1))

    async def refresh(coord):
        started.set()
        await release.wait()
        coord.data = _charge(datetime.now(timezone.utc).timestamp(), current=8, power=4)

    coordinator.refresh = refresh
    hass = _hass_for_cloud([(VIN_A, coordinator, "device-a")])
    first = asyncio.create_task(observation.async_read_tesla_vehicle(hass, VIN_A, timeout=2))
    await started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    second = asyncio.create_task(observation.async_read_tesla_vehicle(hass, VIN_A, timeout=2))
    await asyncio.sleep(0)
    release.set()
    result = await second

    assert result is not None
    assert coordinator.refreshes == 1


@pytest.mark.asyncio
async def test_waiter_timeout_does_not_cancel_shared_provider_read():
    started = asyncio.Event()
    release = asyncio.Event()
    coordinator = _Coordinator(_charge(datetime.now(timezone.utc).timestamp() - 1))

    async def refresh(coord):
        started.set()
        await release.wait()
        coord.data = _charge(datetime.now(timezone.utc).timestamp(), current=4, power=2)

    coordinator.refresh = refresh
    hass = _hass_for_cloud([(VIN_A, coordinator, "device-a")])
    task = asyncio.create_task(
        observation.async_read_tesla_vehicle(hass, VIN_A, timeout=0.01)
    )
    await started.wait()
    assert await task is None
    release.set()
    await asyncio.sleep(0)
    assert coordinator.refreshes == 1


@pytest.mark.asyncio
async def test_replaced_provider_source_cannot_complete_old_read():
    started = asyncio.Event()
    release = asyncio.Event()
    old = _Coordinator(_charge(datetime.now(timezone.utc).timestamp() - 1))
    new = _Coordinator(_charge(datetime.now(timezone.utc).timestamp() - 1))

    async def refresh(coord):
        started.set()
        await release.wait()
        coord.data = _charge(datetime.now(timezone.utc).timestamp(), current=10, power=6)

    old.refresh = refresh
    hass = _hass_for_cloud([(VIN_A, old, "device-a")])
    task = asyncio.create_task(observation.async_read_tesla_vehicle(hass, VIN_A))
    await started.wait()
    entry = hass.config_entries.async_entries("tesla_fleet")[0]
    entry.runtime_data.vehicles = [_vehicle(VIN_A, new, "device-a")]
    release.set()

    assert await task is None


def test_invalid_vin_is_rejected_before_provider_discovery():
    hass = _Hass({})
    assert asyncio.run(observation.async_read_tesla_vehicle(hass, "5YJINVALID")) is None
