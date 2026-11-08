"""Focused tests for the bounded exact-vehicle Tesla observation helper."""
from __future__ import annotations

import asyncio
import ast
from copy import deepcopy
from datetime import datetime, timezone
import math
from pathlib import Path
import sys
import time
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

    def vehicle_sources(hass, vin, *, include_tessie=False):
        matches = []
        for domain in (("teslemetry", "tesla_fleet", "tessie") if include_tessie else ("teslemetry", "tesla_fleet")):
            for entry in hass.config_entries.async_entries(domain):
                for vehicle in getattr(getattr(entry, "runtime_data", None), "vehicles", ()):
                    if str(getattr(vehicle, "vin", "")).upper() != str(vin).upper():
                        continue
                    coordinator = getattr(vehicle, "data_coordinator" if domain == "tessie" else "coordinator", None)
                    if coordinator is not None:
                        matches.append((domain, entry, vehicle, coordinator))
        return matches

    native.vehicle_sources = vehicle_sources
    monkeypatch.setitem(sys.modules, "power_sync.native_rate_telemetry", native)


@pytest.fixture(autouse=True)
def bind_current_registry_helpers(monkeypatch):
    """Keep lazy HA registry imports bound to each test's harness.

    Full collection imports several lightweight Home Assistant module trees.
    A later tree can leave ``device_registry.async_get`` returning a detached
    empty registry, making the exact Fleet source look missing before refresh.
    """
    helpers = sys.modules.get("homeassistant.helpers")
    entity_registry = sys.modules.get("homeassistant.helpers.entity_registry")
    device_registry = sys.modules.get("homeassistant.helpers.device_registry")
    if helpers is None or entity_registry is None or device_registry is None:
        return
    monkeypatch.setattr(
        entity_registry, "async_get", lambda hass: hass.entity_registry,
        raising=False,
    )
    monkeypatch.setattr(
        device_registry, "async_get", lambda hass: hass.device_registry,
        raising=False,
    )
    monkeypatch.setattr(helpers, "entity_registry", entity_registry, raising=False)
    monkeypatch.setattr(helpers, "device_registry", device_registry, raising=False)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity_registry", entity_registry)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.device_registry", device_registry)


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


def _fleet_command_hass(coordinator, *, vin=VIN_A, registrations=None):
    entity_id = "switch.car_charge"
    vehicle = _vehicle(vin, coordinator, "device-a", "tesla_fleet")
    entry = _entry("fleet-entry", [vehicle])
    registration = types.SimpleNamespace(
        config_entry_id="fleet-entry",
        device_id="device-a",
        platform="tesla_fleet",
        unique_id="fleet-charge",
        disabled_by=None,
    )
    hass = _Hass(
        {"tesla_fleet": [entry]},
        registrations={entity_id: registration, **(registrations or {})},
    )
    hass.device_registry.devices = {
        "device-a": types.SimpleNamespace(
            id="device-a",
            identifiers={("tesla_fleet", vin)},
        )
    }
    return hass, entity_id


def _fleet_data(state, timestamp=None):
    data = {"state": state}
    if timestamp is not None:
        data.update(
            {
                "charge_state_timestamp": timestamp,
                "charge_state_charging_state": "Stopped",
                "charge_state_charger_actual_current": 0,
                "charge_state_charger_power": 0,
            }
        )
    return data


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


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["ASLEEP", "OFFLINE"])
async def test_fleet_preparation_refreshes_stale_online_to_sleep_for_provider_wake(state):
    old = math.floor(datetime.now(timezone.utc).timestamp()) - 300
    coordinator = _Coordinator(_fleet_data("ONLINE", old))
    hass, command_entity = _fleet_command_hass(coordinator)

    async def refresh(coord):
        coord.data = _fleet_data(state)

    coordinator.refresh = refresh

    assert await observation.async_prepare_tesla_fleet_command(
        hass, VIN_A, command_entity
    ) is True
    assert coordinator.refreshes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh_result", ["unchanged", "error", "rate_limited"])
async def test_fleet_preparation_rejects_online_without_new_successful_observation(
    refresh_result,
):
    old = math.floor(datetime.now(timezone.utc).timestamp()) - 1
    coordinator = _Coordinator(_fleet_data("ONLINE", old))
    hass, command_entity = _fleet_command_hass(coordinator)

    async def refresh(coord):
        if refresh_result == "error":
            raise RuntimeError("provider failure")
        if refresh_result == "rate_limited":
            coord.last_update_success = False
        coord.data = _fleet_data("ONLINE", old)

    coordinator.refresh = refresh

    assert await observation.async_prepare_tesla_fleet_command(
        hass, VIN_A, command_entity
    ) is False
    assert coordinator.refreshes == 1


@pytest.mark.asyncio
async def test_fleet_preparation_accepts_new_fresh_exact_online_observation():
    old = math.floor(datetime.now(timezone.utc).timestamp()) - 1
    coordinator = _Coordinator(_fleet_data("ONLINE", old))
    hass, command_entity = _fleet_command_hass(coordinator)

    async def refresh(coord):
        coord.data = _fleet_data("ONLINE", math.floor(time.time()))

    coordinator.refresh = refresh

    assert await observation.async_prepare_tesla_fleet_command(
        hass, VIN_A, command_entity
    ) is True
    assert coordinator.refreshes == 1


@pytest.mark.asyncio
async def test_concurrent_fleet_preparations_share_one_bounded_refresh():
    coordinator = _Coordinator(_fleet_data("ONLINE", math.floor(time.time()) - 1))
    hass, command_entity = _fleet_command_hass(coordinator)
    started = asyncio.Event()
    release = asyncio.Event()

    async def refresh(coord):
        started.set()
        await release.wait()
        coord.data = _fleet_data("ASLEEP")

    coordinator.refresh = refresh
    first = asyncio.create_task(
        observation.async_prepare_tesla_fleet_command(hass, VIN_A, command_entity)
    )
    second = None
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        second = asyncio.create_task(
            observation.async_prepare_tesla_fleet_command(hass, VIN_A, command_entity)
        )
        await asyncio.sleep(0)
        release.set()

        assert await asyncio.wait_for(
            asyncio.gather(first, second), timeout=1
        ) == [True, True]
        assert coordinator.refreshes == 1
    finally:
        release.set()
        tasks = [task for task in (first, second) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_fleet_preparation_rejects_missing_or_rebound_exact_source():
    old = math.floor(datetime.now(timezone.utc).timestamp()) - 1
    coordinator = _Coordinator(_fleet_data("ONLINE", old))
    hass, command_entity = _fleet_command_hass(coordinator)
    assert await observation.async_prepare_tesla_fleet_command(
        hass, VIN_A, "switch.missing"
    ) is False
    assert coordinator.refreshes == 0

    replacement = _Coordinator(_fleet_data("ASLEEP"))
    entry = hass.config_entries.async_entries("tesla_fleet")[0]

    async def refresh(coord):
        entry.runtime_data.vehicles = [_vehicle(VIN_A, replacement, "device-a")]
        coord.data = _fleet_data("ONLINE", math.floor(time.time()))

    coordinator.refresh = refresh
    assert await observation.async_prepare_tesla_fleet_command(
        hass, VIN_A, command_entity
    ) is False
    assert coordinator.refreshes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["platform", "config_entry_id", "device_id", "unique_id"])
async def test_fleet_preparation_requires_complete_active_command_registration(field):
    coordinator = _Coordinator(_fleet_data("ASLEEP"))
    hass, command_entity = _fleet_command_hass(coordinator)
    registration = hass.entity_registry.async_get(command_entity)
    setattr(registration, field, None)

    assert await observation.async_prepare_tesla_fleet_command(
        hass, VIN_A, command_entity
    ) is False
    assert coordinator.refreshes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("field_value", ["replacement", "disabled"])
async def test_fleet_preparation_rejects_command_identity_rebind_after_refresh(field_value):
    coordinator = _Coordinator(_fleet_data("ASLEEP"))
    hass, command_entity = _fleet_command_hass(coordinator)
    registration = hass.entity_registry.async_get(command_entity)

    async def refresh(coord):
        if field_value == "replacement":
            registration.unique_id = "other-fleet-charge"
        else:
            registration.disabled_by = "user"
        coord.data = _fleet_data("ASLEEP")

    coordinator.refresh = refresh
    assert await observation.async_prepare_tesla_fleet_command(
        hass, VIN_A, command_entity
    ) is False
    assert coordinator.refreshes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation_target", ["vehicle", "registry"])
async def test_fleet_preparation_rejects_provider_vin_rebind_after_refresh(
    mutation_target,
):
    coordinator = _Coordinator(_fleet_data("ASLEEP"))
    hass, command_entity = _fleet_command_hass(coordinator)
    entry = hass.config_entries.async_entries("tesla_fleet")[0]
    vehicle = entry.runtime_data.vehicles[0]
    registered = hass.device_registry.devices["device-a"]

    async def refresh(coord):
        if mutation_target == "vehicle":
            vehicle.device.identifiers = {("tesla_fleet", VIN_B)}
        else:
            registered.identifiers = {("tesla_fleet", VIN_B)}
        coord.data = _fleet_data("ASLEEP")

    coordinator.refresh = refresh
    assert await observation.async_prepare_tesla_fleet_command(
        hass, VIN_A, command_entity
    ) is False
    assert coordinator.refreshes == 1


@pytest.mark.asyncio
async def test_fleet_preparation_ignores_unrelated_three_part_identifier():
    coordinator = _Coordinator(_fleet_data("ASLEEP"))
    hass, command_entity = _fleet_command_hass(coordinator)
    vehicle = hass.config_entries.async_entries("tesla_fleet")[0].runtime_data.vehicles[0]
    vehicle.device.identifiers.add(("teslemetry", VIN_A, "vehicle"))
    hass.device_registry.devices["device-a"].identifiers.add(
        ("teslemetry", VIN_A, "vehicle")
    )

    assert await observation.async_prepare_tesla_fleet_command(
        hass, VIN_A, command_entity
    ) is True
    assert coordinator.refreshes == 1


@pytest.mark.asyncio
async def test_fleet_preparation_rejects_ambiguous_same_binding_sources():
    coordinator = _Coordinator(_fleet_data("ASLEEP"))
    hass, command_entity = _fleet_command_hass(coordinator)
    entry = hass.config_entries.async_entries("tesla_fleet")[0]
    entry.runtime_data.vehicles.append(
        _vehicle(VIN_A, _Coordinator(_fleet_data("ASLEEP")), "device-a")
    )

    assert await observation.async_prepare_tesla_fleet_command(
        hass, VIN_A, command_entity
    ) is False
    assert coordinator.refreshes == 0


@pytest.mark.asyncio
async def test_fleet_preparation_rejects_wrong_vin_provider_and_propagates_cancel():
    old = math.floor(datetime.now(timezone.utc).timestamp()) - 1
    coordinator = _Coordinator(_fleet_data("ONLINE", old))
    hass, command_entity = _fleet_command_hass(coordinator)
    assert await observation.async_prepare_tesla_fleet_command(
        hass, VIN_B, command_entity
    ) is False
    assert coordinator.refreshes == 0

    fleet_entry = hass.config_entries.async_entries("tesla_fleet")[0]
    fleet_entry.runtime_data.vehicles = []
    teslemetry_entry = _entry(
        "teslemetry-entry",
        [_vehicle(VIN_A, coordinator, "device-a", "teslemetry")],
    )
    hass.config_entries.async_entries = lambda domain: {
        "tesla_fleet": [fleet_entry],
        "teslemetry": [teslemetry_entry],
    }.get(domain, [])
    assert await observation.async_prepare_tesla_fleet_command(
        hass, VIN_A, command_entity
    ) is False
    assert coordinator.refreshes == 0

    fleet_entry.runtime_data.vehicles = [
        _vehicle(VIN_A, coordinator, "device-a", "tesla_fleet")
    ]
    hass.config_entries.async_entries = lambda domain: {
        "tesla_fleet": [fleet_entry],
    }.get(domain, [])

    started = asyncio.Event()
    release = asyncio.Event()

    async def refresh(coord):
        started.set()
        await release.wait()
        coord.data = _fleet_data("ASLEEP")

    coordinator.refresh = refresh
    task = asyncio.create_task(
        observation.async_prepare_tesla_fleet_command(
            hass, VIN_A, command_entity, timeout=2
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await asyncio.sleep(0)
    assert coordinator.refreshes == 1


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


@pytest.fixture
def independent_source(monkeypatch):
    """Model HA's separate native cloud coordinator and command router."""
    now = 2_000_000_000.0
    monkeypatch.setattr(observation, "time", types.SimpleNamespace(time=lambda: now))

    class Cloud:
        def __init__(self):
            self.response = {"vin": VIN_A, **_charge(now)}
            self.calls = []
            self.before_return = None
            self.wake_response = {"response": {"state": "online"}}
            self.vehicle_responses = []
            self.wake_calls = []
            self.refresh_calls = []
            self.refresh_response = {"response": True}
            self.before_refresh_return = None

        async def state(self, *, use_cache=True):
            self.calls.append({"use_cache": use_cache})
            if self.before_return is not None:
                await self.before_return()
            return deepcopy(self.response)

        async def vehicle_data(self, *, endpoints):
            self.calls.append(endpoints)
            if self.before_return is not None:
                await self.before_return()
            return {"response": deepcopy(self.response)}

        async def data_refresh(self):
            self.refresh_calls.append(True)
            if self.before_refresh_return is not None:
                await self.before_refresh_return()
            return deepcopy(self.refresh_response)

        async def wake_up(self, *, wait=False, timeout=60):
            self.wake_calls.append((wait, timeout))
            return deepcopy(self.wake_response)

        async def vehicle(self):
            if self.vehicle_responses:
                return deepcopy(self.vehicle_responses.pop(0))
            return {"response": {"state": "online"}}

    class NativeCoordinator(_Coordinator):
        def __init__(self):
            super().__init__(_charge(now - 10))
            self.api = Cloud()
            self.lock = asyncio.Lock()
            self._debounced_refresh = types.SimpleNamespace(async_lock=lambda: self.lock)

    for domain, coordinator_name, api_module, api_name in (
        ("tesla_fleet", "TeslaFleetVehicleDataCoordinator", "tesla_fleet_api.tesla", "VehicleFleet"),
        ("teslemetry", "TeslemetryVehicleDataCoordinator", "tesla_fleet_api.teslemetry", "Vehicle"),
        ("tessie", "TessieStateUpdateCoordinator", "tesla_fleet_api.tessie", "Vehicle"),
    ):
        module = types.ModuleType(f"homeassistant.components.{domain}.coordinator")
        setattr(module, coordinator_name, NativeCoordinator)
        monkeypatch.setitem(sys.modules, module.__name__, module)
        module = types.ModuleType(api_module)
        setattr(module, api_name, Cloud)
        monkeypatch.setitem(sys.modules, module.__name__, module)
    const = types.ModuleType("tesla_fleet_api.const")
    const.VehicleDataEndpoint = types.SimpleNamespace(CHARGE_STATE="charge_state")
    monkeypatch.setitem(sys.modules, const.__name__, const)

    def make(domain="tesla_fleet"):
        coordinator = NativeCoordinator()
        vehicle = _vehicle(VIN_A, coordinator, "device-a", domain)
        if domain == "tessie":
            vehicle.data_coordinator = coordinator
            del vehicle.coordinator
        # Native Teslemetry commands can use a BLE/cloud router while the
        # coordinator retains the separate cloud Vehicle client.
        vehicle.api = object()
        entry = _entry("native-entry", [vehicle])
        hass = _Hass({domain: [entry]})
        return hass, entry, vehicle, coordinator

    return make


@pytest.fixture
def command_recovery_read(independent_source, monkeypatch):
    """Use real command authority and readers around native cached SDK I/O."""
    from conftest import load_power_sync_submodule
    from collections.abc import Mapping
    from power_sync.tesla_ble_mapping import canonical_tesla_vehicle_id

    if "power_sync.automations" not in sys.modules:
        automations = types.ModuleType("power_sync.automations")
        automations.__path__ = [str(ROOT / "automations")]
        monkeypatch.setitem(sys.modules, automations.__name__, automations)

    hass, native_entry, vehicle, coordinator = independent_source("teslemetry")
    _, fleet_entry, _, fleet = independent_source("tesla_fleet")
    fleet_entry.entry_id = "fleet-entry"
    fleet_entry.runtime_data.vehicles[0].device.id = "fleet-device"
    control = types.SimpleNamespace(entry_id="control-entry", data={}, options={"ev_provider": "both"})
    runtime = {"entry": control}
    hass.data["power_sync"] = {control.entry_id: runtime}
    recovery = load_power_sync_submodule("tesla_ev_recovery", "power_sync")
    runtime["_tesla_ev_recovery_sidecar"] = recovery._new_sidecar()
    runtime["_tesla_ev_recovery_sidecar_loaded"] = True
    hass.config_entries.async_entries = lambda domain: {
        "power_sync": [control], "teslemetry": [native_entry],
        "tesla_fleet": [fleet_entry],
    }.get(domain, [])
    entity = "switch.native_charge"
    registration = types.SimpleNamespace(entity_id=entity, platform="teslemetry",
        config_entry_id=native_entry.entry_id, device_id="device-a",
        unique_id="native-charge", disabled_by=None)
    registrations = {entity: registration}
    hass.entity_registry = types.SimpleNamespace(async_get=registrations.get)
    hass.device_registry.devices["device-a"] = vehicle.device
    clock = {"now": 2_000_000_000.0}
    monkeypatch.setattr(observation, "time", types.SimpleNamespace(time=lambda: clock["now"]))
    monkeypatch.setitem(sys.modules, "power_sync.tesla_ev_observation", observation)
    coordinator.api.response = {"vin": VIN_A, **_charge(clock["now"] - 30, current=0, power=0,
                                                       requested=15, limit=70)}

    async def cached_native_refresh(coord):
        # Installed HA's native Teslemetry coordinator invokes the default
        # SDK vehicle_data, which can return the same classic cache document.
        coord.data = deepcopy((await type(coord.api).vehicle_data(
            coord.api, endpoints=["charge_state"]))["response"])

    coordinator.refresh = cached_native_refresh
    coordinator.data = deepcopy(coordinator.api.response)
    coordinator.api.refresh_response = {"response": {"vin": VIN_A,
        **_charge(clock["now"] + 0.25, current=0, power=0, requested=15, limit=70)}}
    constants = load_power_sync_submodule("const", "command_recovery_observation_probe")
    source = ROOT / "automations/actions.py"
    names = {"_fresh_tesla_command_observation", "_tesla_transport_authority",
             "_tesla_native_deadline_token", "_tesla_native_binding",
             "_tesla_legacy_entity_identity"}
    nodes = [node for node in ast.parse(source.read_text()).body if getattr(node, "name", None) in names]

    class ClockDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clock["now"], tz)

    namespace = {**vars(constants), "__name__": "power_sync.automations._command_read_probe",
        "__package__": "power_sync.automations", "__spec__": None,
        "datetime": ClockDatetime, "dt_timezone": timezone, "math": math, "Mapping": Mapping,
        "re": __import__("re"),
        "DEFAULT_VEHICLE_ID": "_default", "canonical_tesla_vehicle_id": canonical_tesla_vehicle_id,
        "er": types.SimpleNamespace(async_get=lambda _hass: hass.entity_registry),
        "dr": types.SimpleNamespace(async_get=lambda _hass: types.SimpleNamespace(async_get=hass.device_registry.devices.get))}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec",
                 flags=__import__("__future__").annotations.compiler_flag), namespace)
    return types.SimpleNamespace(hass=hass, control=control, runtime=runtime,
        native_entry=native_entry, vehicle=vehicle, coordinator=coordinator, fleet=fleet,
        fleet_entry=fleet_entry, recovery=recovery,
        entity=entity, registration=registration, clock=clock,
        params={"vehicle_vin": VIN_A, "_tesla_command_entity_id": entity,
                "_boost_start_deadline": clock["now"] + 30},
        read=namespace["_fresh_tesla_command_observation"])


@pytest.mark.asyncio
async def test_command_recovery_cached_teslemetry_read_needs_one_exact_paid_refresh(command_recovery_read):
    probe = command_recovery_read
    # Establish the unchanged coordinator contract without a mocked None read.
    ordinary = await observation.async_read_tesla_vehicle(
        probe.hass, VIN_A, preferred_entity_id=probe.entity)
    assert ordinary is None
    assert len(probe.coordinator.api.calls) == 1 and probe.fleet.api.calls == []
    probe.coordinator.api.calls.clear()
    result = await probe.read(probe.hass, probe.control, VIN_A, probe.params)
    assert result is not None
    assert result.domain == "teslemetry" and result.requested_current == 15
    assert result.charge_limit_soc == 70 and result.charger_actual_current == 0
    assert probe.coordinator.api.refresh_calls == [True]
    assert len(probe.coordinator.api.calls) == 1
    assert probe.fleet.api.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,paid", [("both", True), ("fleet_api", True), ("tesla_ble", False), ("teslemetry_bt", False)])
async def test_command_recovery_paid_read_follows_current_cloud_policy(command_recovery_read, provider, paid):
    probe = command_recovery_read
    probe.control.options["ev_provider"] = provider
    result = await probe.read(probe.hass, probe.control, VIN_A, probe.params)
    assert (result is not None) is paid
    assert len(probe.coordinator.api.refresh_calls) == int(paid)
    assert len(probe.coordinator.api.calls) == 1
    assert probe.fleet.api.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["provider", "deadline", "deadline_extended", "runtime", "generation", "registry", "client", "anchor", "owner"])
async def test_command_recovery_revoked_scope_after_cached_read_never_pays(command_recovery_read, monkeypatch, mutation):
    probe = command_recovery_read
    client = probe.coordinator.api
    paid_reads = []
    real_paid_read = observation.async_read_independent_tesla_vehicle

    async def paid_read(*args, **kwargs):
        paid_reads.append(True)
        return await real_paid_read(*args, **kwargs)

    monkeypatch.setattr(observation, "async_read_independent_tesla_vehicle", paid_read)

    async def revoke():
        if mutation == "provider":
            probe.control.options["ev_provider"] = "tesla_ble"
        elif mutation == "deadline":
            probe.clock["now"] += 31
        elif mutation == "deadline_extended":
            probe.params["_boost_start_deadline"] += 60
        elif mutation == "runtime":
            probe.hass.data["power_sync"][probe.control.entry_id] = {"entry": probe.control}
        elif mutation == "generation":
            probe.runtime["_tesla_ev_recovery_sidecar"]["generations"][VIN_A] = 1
        elif mutation == "registry":
            probe.registration.unique_id = "replacement-native-charge"
        elif mutation == "client":
            probe.coordinator.api = object()
        elif mutation == "anchor":
            probe.params["_tesla_command_entity_id"] = "switch.another_charge"
        else:
            probe.runtime["ev_ownership"][VIN_A] = {"owner": "powersync", "owner_mode": "manual"}

    client.before_return = revoke
    assert await probe.read(probe.hass, probe.control, VIN_A, probe.params) is None
    assert paid_reads == []
    assert client.refresh_calls == []
    assert len(client.calls) == 1 and probe.fleet.api.calls == []


@pytest.mark.asyncio
async def test_command_recovery_rechecks_cloud_policy_inside_native_lock(command_recovery_read):
    probe = command_recovery_read
    ordinary_refresh = probe.coordinator.refresh
    entered = asyncio.Event()

    async def cached_read_then_lock(coord):
        await ordinary_refresh(coord)
        await coord.lock.acquire()
        entered.set()

    probe.coordinator.refresh = cached_read_then_lock
    task = asyncio.create_task(probe.read(probe.hass, probe.control, VIN_A, probe.params))
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.sleep(0)
    assert not task.done()
    probe.control.options["ev_provider"] = "tesla_ble"
    probe.coordinator.lock.release()
    assert await asyncio.wait_for(task, 1) is None
    assert len(probe.coordinator.api.calls) == 1
    assert probe.coordinator.api.refresh_calls == probe.fleet.api.calls == []


@pytest.mark.asyncio
async def test_command_recovery_rechecks_policy_after_paid_refresh(command_recovery_read):
    probe = command_recovery_read

    async def revoke():
        probe.control.options["ev_provider"] = "tesla_ble"

    probe.coordinator.api.before_refresh_return = revoke
    assert await probe.read(probe.hass, probe.control, VIN_A, probe.params) is None
    assert probe.coordinator.api.refresh_calls == [True]
    # The one ordinary read happened before refresh; revocation prevents a
    # cache fallback or accepting the full document returned by refresh.
    assert len(probe.coordinator.api.calls) == 1 and probe.fleet.api.calls == []


@pytest.mark.asyncio
async def test_command_recovery_recomputes_timeout_from_original_deadline(command_recovery_read, monkeypatch):
    probe = command_recovery_read
    probe.params["_boost_start_deadline"] = probe.clock["now"] + 6

    async def slow_cache():
        probe.clock["now"] += 4

    async def fresh():
        probe.coordinator.api.refresh_response = {"response": {"vin": VIN_A,
            **_charge(probe.clock["now"] + 0.125, requested=15, limit=70)}}

    probe.coordinator.api.before_return = slow_cache
    probe.coordinator.api.before_refresh_return = fresh
    real_paid_read = observation.async_read_independent_tesla_vehicle
    timeouts = []

    async def read(*args, **kwargs):
        timeouts.append(kwargs["timeout"])
        return await real_paid_read(*args, **kwargs)

    monkeypatch.setattr(observation, "async_read_independent_tesla_vehicle", read)
    assert await probe.read(probe.hass, probe.control, VIN_A, probe.params) is not None
    assert timeouts == [2]
    assert probe.params["_boost_start_deadline"] == 2_000_000_006
    assert probe.coordinator.api.refresh_calls == [True]


@pytest.mark.asyncio
async def test_command_recovery_fresh_ordinary_source_does_not_pay(command_recovery_read):
    probe = command_recovery_read
    probe.coordinator.api.response["charge_state"]["timestamp"] = probe.clock["now"] + 0.25
    result = await probe.read(probe.hass, probe.control, VIN_A, probe.params)
    assert result is not None and result.origin == "native_coordinator"
    assert probe.coordinator.api.refresh_calls == []
    assert len(probe.coordinator.api.calls) == 1 and probe.fleet.api.calls == []


@pytest.mark.asyncio
async def test_command_recovery_pinned_fleet_never_refreshes_other_teslemetry_source(command_recovery_read):
    probe = command_recovery_read
    probe.registration.platform = "tesla_fleet"
    probe.registration.config_entry_id = probe.fleet_entry.entry_id
    probe.registration.device_id = "fleet-device"
    probe.fleet.api.response["charge_state"]["timestamp"] = probe.clock["now"] - 30
    probe.fleet.data = deepcopy(probe.fleet.api.response)

    async def cached(coord):
        coord.data = deepcopy((await type(coord.api).vehicle_data(
            coord.api, endpoints=["charge_state"]))["response"])

    probe.fleet.refresh = cached
    assert await probe.read(probe.hass, probe.control, VIN_A, probe.params) is None
    assert len(probe.fleet.api.calls) == 2 and probe.fleet.api.refresh_calls == []
    assert probe.coordinator.api.calls == probe.coordinator.api.refresh_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('provider,mutation', [
    ('both', None), ('fleet_api', None), ('tesla_ble', None),
    *[('both', item) for item in ('provider', 'deadline', 'deadline_extended', 'runtime',
                                 'generation', 'registry', 'client', 'anchor', 'owner')],
])
async def test_command_recovery_pinned_fleet_uses_one_qualified_independent_read(
    command_recovery_read, monkeypatch, provider, mutation,
):
    probe = command_recovery_read
    probe.control.options['ev_provider'] = provider
    probe.registration.platform = 'tesla_fleet'
    probe.registration.config_entry_id = probe.fleet_entry.entry_id
    probe.registration.device_id = 'fleet-device'
    client = probe.fleet.api
    client.response = {'vin': VIN_A, **_charge(probe.clock['now']-30, current=0, power=0)}
    probe.fleet.data = deepcopy(client.response)
    async def refresh_stale_coordinator(coord):
        coord.data = deepcopy((await type(client).vehicle_data(client, endpoints=['charge_state']))['response'])
        # The exact cloud source advances while HA's coordinator still holds
        # the older stopped document. No cross-provider read may fill the gap.
        client.response = {'vin': VIN_A, **_charge(probe.clock['now']+0.25, state='Charging', current=5, power=1.2)}
    probe.fleet.refresh = refresh_stale_coordinator
    async def revoke():
        if len(client.calls) != 2: return
        if mutation == 'provider': probe.control.options['ev_provider'] = 'tesla_ble'
        if mutation == 'deadline': probe.clock['now'] += 31
        if mutation == 'deadline_extended': probe.params['_boost_start_deadline'] += 60
        if mutation == 'runtime': probe.hass.data['power_sync'][probe.control.entry_id] = {'entry': probe.control}
        if mutation == 'generation': probe.runtime['_tesla_ev_recovery_sidecar']['generations'][VIN_A] = 1
        if mutation == 'registry': probe.registration.unique_id = 'replacement'
        if mutation == 'client': probe.fleet.api = object()
        if mutation == 'anchor': probe.params['_tesla_command_entity_id'] = 'switch.other_charge'
        if mutation == 'owner': probe.runtime['ev_ownership'][VIN_A] = {'owner': 'powersync', 'owner_mode': 'manual'}
    client.before_return = revoke
    real_read = observation.async_read_independent_tesla_vehicle
    reads = []
    async def independent(*args, **kwargs):
        assert kwargs['preferred_entity_id'] == probe.entity
        assert kwargs['allow_paid_refresh'] is False
        assert kwargs['authorize']()
        reads.append(True)
        return await real_read(*args, **kwargs)
    monkeypatch.setattr(observation, 'async_read_independent_tesla_vehicle', independent)
    result = await probe.read(probe.hass, probe.control, VIN_A, probe.params)
    assert (result is not None) is (provider != 'tesla_ble' and mutation is None)
    if result is not None:
        assert result.domain == 'tesla_fleet' and result.origin == 'native_cloud'
        assert result.charge_state == 'Charging' and result.charger_actual_current == 5
    assert len(reads) == int(provider != 'tesla_ble')
    assert len(client.calls) == 1+int(provider != 'tesla_ble')
    assert client.refresh_calls == []
    assert probe.coordinator.api.calls == probe.coordinator.api.refresh_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("rebind", [False, True])
async def test_command_recovery_pins_durable_native_anchor_before_read(command_recovery_read, monkeypatch, rebind):
    probe = command_recovery_read
    probe.params.pop("_tesla_command_entity_id")
    route = {"kind": "ha", "entity_id": probe.entity}
    monkeypatch.setattr(probe.recovery, "get_vehicle_route", lambda *_args: dict(route))

    async def change_route():
        if rebind:
            route["entity_id"] = "switch.another_charge"

    probe.coordinator.api.before_return = change_route
    result = await probe.read(probe.hass, probe.control, VIN_A, probe.params)
    assert (result is not None) is (not rebind)
    assert len(probe.coordinator.api.refresh_calls) == int(not rebind)
    assert probe.fleet.api.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["tesla_fleet", "teslemetry"])
async def test_independent_read_uses_exact_cloud_payload_not_published_stream(independent_source, domain):
    hass, _entry, _vehicle, coordinator = independent_source(domain)

    async def stream_race():
        coordinator.data = _charge(2_000_000_001, requested=99, limit=100)

    coordinator.api.before_return = stream_race
    result = await observation.async_read_independent_tesla_vehicle(hass, VIN_A)

    assert result is not None
    assert result.origin == "native_cloud"
    assert result.domain == domain
    assert result.requested_current == 16
    assert result.charge_limit_soc == 80
    assert result.source_charge_timestamp == 2_000_000_000
    assert result.field_provenance["requested_current"] == "charge_state.charge_current_request"
    assert coordinator.api.calls == [["charge_state"]]
    assert coordinator.refreshes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing_requested", "actual_only", "missing_limit", "missing_power", "missing_actual", "stale", "wrong_vin"])
async def test_independent_read_rejects_incomplete_or_wrong_source(independent_source, fault):
    hass, _entry, _vehicle, coordinator = independent_source()
    charge = coordinator.api.response["charge_state"]
    if fault in {"missing_requested", "actual_only"}:
        charge.pop("charge_current_request")
        if fault == "actual_only":
            charge["charger_actual_current"] = 16
            charge["charger_pilot_current"] = 16
    elif fault.startswith("missing_"):
        charge.pop({"missing_limit": "charge_limit_soc", "missing_power": "charger_power", "missing_actual": "charger_actual_current"}[fault])
    elif fault == "stale":
        charge["timestamp"] = 1_999_999_990
    else:
        coordinator.api.response["vin"] = VIN_B
    assert await observation.async_read_independent_tesla_vehicle(hass, VIN_A) is None


@pytest.mark.asyncio
async def test_independent_reads_require_advancement_between_reconciliation_reads(independent_source):
    hass, _entry, _vehicle, coordinator = independent_source()
    first = await observation.async_read_independent_tesla_vehicle(hass, VIN_A)
    assert first is not None
    assert await observation.async_read_independent_tesla_vehicle(hass, VIN_A) is None
    coordinator.api.response["charge_state"]["timestamp"] += 1
    later = await observation.async_read_independent_tesla_vehicle(hass, VIN_A, updated_after=first.source_charge_timestamp)
    assert later is not None
    assert later.source_charge_timestamp > first.source_charge_timestamp


@pytest.mark.asyncio
async def test_paid_teslemetry_refresh_produces_two_advancing_documents_from_stale_cache(independent_source):
    hass, _entry, _vehicle, coordinator = independent_source("teslemetry")
    client = coordinator.api
    client.response["charge_state"]["timestamp"] = 1_999_999_970_767
    # A cloud-origin SDK response is still cached; a one-second retry does
    # not make its source timestamp fresh or advance the returned settings.
    assert await observation.async_read_independent_tesla_vehicle(hass, VIN_A) is None
    assert await observation.async_read_independent_tesla_vehicle(hass, VIN_A) is None
    assert client.refresh_calls == []

    async def fresh_document():
        client.refresh_response = {"response": {"vin": VIN_A,
            **_charge(2_000_000_000 + len(client.refresh_calls), current=0, power=0,
                      requested=15, limit=70)}}

    client.before_refresh_return = fresh_document
    allowed = lambda: True
    first = await observation.async_read_independent_tesla_vehicle(
        hass, VIN_A, allow_paid_refresh=True, authorize=allowed)
    second = await observation.async_read_independent_tesla_vehicle(
        hass, VIN_A, allow_paid_refresh=True, authorize=allowed,
        updated_after=first.source_charge_timestamp)
    assert first is not None and second is not None
    assert second.source_charge_timestamp > first.source_charge_timestamp
    assert first.requested_current == second.requested_current == 15
    assert first.charge_limit_soc == second.charge_limit_soc == 70
    assert len(client.refresh_calls) == 2
    assert len(client.calls) == 2  # Only the two initial default cached reads.


@pytest.mark.asyncio
@pytest.mark.parametrize("domain,paid", [("teslemetry", False), ("tesla_fleet", False), ("tesla_fleet", True)])
async def test_default_and_fleet_reads_never_request_paid_refresh(independent_source, domain, paid):
    hass, _entry, _vehicle, coordinator = independent_source(domain)
    result = await observation.async_read_independent_tesla_vehicle(
        hass, VIN_A, allow_paid_refresh=paid, authorize=lambda: True)
    assert result is not None
    assert coordinator.api.refresh_calls == []
    assert len(coordinator.api.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [None, False, "raises"])
async def test_paid_refresh_requires_current_explicit_authorization(independent_source, policy):
    hass, _entry, _vehicle, coordinator = independent_source("teslemetry")

    def authorize():
        if policy == "raises":
            raise RuntimeError("policy unavailable")
        return policy

    assert await observation.async_read_independent_tesla_vehicle(
        hass, VIN_A, allow_paid_refresh=True,
        authorize=None if policy is None else authorize) is None
    assert coordinator.api.refresh_calls == coordinator.api.calls == []


@pytest.mark.asyncio
async def test_paid_refresh_rechecks_authorization_after_native_lock_wait(independent_source):
    hass, _entry, _vehicle, coordinator = independent_source("teslemetry")
    allowed = True
    await coordinator.lock.acquire()
    task = asyncio.create_task(observation.async_read_independent_tesla_vehicle(
        hass, VIN_A, allow_paid_refresh=True, authorize=lambda: allowed))
    await asyncio.sleep(0)
    assert not task.done()
    allowed = False
    coordinator.lock.release()
    assert await asyncio.wait_for(task, 1) is None
    assert coordinator.api.refresh_calls == coordinator.api.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["policy", "runtime", "client", "shutdown", "device"])
async def test_paid_refresh_rechecks_identity_and_policy_before_cache_fallback(independent_source, mutation):
    hass, entry, vehicle, coordinator = independent_source("teslemetry")
    client = coordinator.api
    allowed = True

    async def invalidate():
        nonlocal allowed
        if mutation == "policy":
            allowed = False
        elif mutation == "runtime":
            entry.runtime_data = types.SimpleNamespace(vehicles=[vehicle])
        elif mutation == "client":
            coordinator.api = object()
        elif mutation == "shutdown":
            coordinator._shutdown_requested = True
        else:
            vehicle.device.identifiers = {("teslemetry", VIN_B)}

    client.before_refresh_return = invalidate
    assert await observation.async_read_independent_tesla_vehicle(
        hass, VIN_A, allow_paid_refresh=True, authorize=lambda: allowed) is None
    assert client.refresh_calls == [True]
    assert client.calls == []


@pytest.mark.asyncio
async def test_refresh_cache_fallback_uses_original_pre_refresh_time_and_class_method(independent_source, monkeypatch):
    hass, _entry, _vehicle, coordinator = independent_source("teslemetry")
    client = coordinator.api
    clock = {"now": 2_000_000_000.0}
    monkeypatch.setattr(observation, "time", types.SimpleNamespace(time=lambda: clock["now"]))

    async def refresh_cache():
        client.response = {"vin": VIN_A, **_charge(clock["now"] + 0.125, requested=15, limit=70)}
        clock["now"] += 2.5

    async def forbidden_instance_router(*_args, **_kwargs):
        raise AssertionError("instance command router was called")

    client.before_refresh_return = refresh_cache
    client.data_refresh = forbidden_instance_router
    client.vehicle_data = forbidden_instance_router
    result = await observation.async_read_independent_tesla_vehicle(
        hass, VIN_A, allow_paid_refresh=True, authorize=lambda: True)
    assert result is not None
    assert result.source_charge_timestamp == 2_000_000_000.125
    assert result.requested_current == 15 and result.charge_limit_soc == 70
    assert client.refresh_calls == [True]
    assert client.calls == [["charge_state"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [None, True, {"response": True}, {"response": {"result": True}}, {"response": {"result": False}}])
async def test_refresh_acknowledgement_never_qualifies_with_stale_cached_data(independent_source, reply):
    hass, _entry, _vehicle, coordinator = independent_source("teslemetry")
    client = coordinator.api
    client.refresh_response = reply
    client.response["charge_state"]["timestamp"] -= 30
    assert await observation.async_read_independent_tesla_vehicle(
        hass, VIN_A, allow_paid_refresh=True, authorize=lambda: True) is None
    assert client.refresh_calls == [True]
    assert client.calls == [["charge_state"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["wrong_vin", "missing_requested", "missing_limit", "missing_power", "missing_actual", "same_timestamp"])
async def test_paid_refresh_requires_complete_exact_fresh_document(independent_source, fault):
    hass, _entry, _vehicle, coordinator = independent_source("teslemetry")
    client = coordinator.api
    document = deepcopy(client.response)
    if fault == "wrong_vin":
        document["vin"] = VIN_B
    elif fault == "same_timestamp":
        document["charge_state"]["timestamp"] = 1_999_999_990
    else:
        document["charge_state"].pop({"missing_requested": "charge_current_request",
            "missing_limit": "charge_limit_soc", "missing_power": "charger_power",
            "missing_actual": "charger_actual_current"}[fault])
    client.refresh_response = {"response": document}
    client.response["charge_state"]["timestamp"] -= 30
    assert await observation.async_read_independent_tesla_vehicle(
        hass, VIN_A, allow_paid_refresh=True, authorize=lambda: True) is None
    assert len(client.refresh_calls) == len(client.calls) == 1


@pytest.mark.asyncio
async def test_paid_refresh_rechecks_policy_after_fallback_read(independent_source):
    hass, _entry, _vehicle, coordinator = independent_source("teslemetry")
    allowed = True

    async def revoke():
        nonlocal allowed
        allowed = False

    coordinator.api.before_return = revoke
    assert await observation.async_read_independent_tesla_vehicle(
        hass, VIN_A, allow_paid_refresh=True, authorize=lambda: allowed) is None
    assert coordinator.api.refresh_calls == [True]
    assert len(coordinator.api.calls) == 1
    assert not observation._read_state(hass).independent_timestamps


@pytest.mark.asyncio
async def test_paid_refresh_failure_does_not_retry_or_read_old_cache(independent_source):
    hass, _entry, _vehicle, coordinator = independent_source("teslemetry")

    async def failed():
        raise RuntimeError("refresh refused")

    coordinator.api.before_refresh_return = failed
    assert await observation.async_read_independent_tesla_vehicle(
        hass, VIN_A, allow_paid_refresh=True, authorize=lambda: True) is None
    assert coordinator.api.refresh_calls == [True]
    assert coordinator.api.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("same_authorization", [False, True])
async def test_paid_refresh_coalesces_only_matching_authorization_scope(independent_source, same_authorization):
    hass, _entry, _vehicle, coordinator = independent_source("teslemetry")
    client = coordinator.api

    async def fresh_document():
        client.refresh_response = {"response": {"vin": VIN_A,
            **_charge(2_000_000_000 + len(client.refresh_calls))}}

    client.before_refresh_return = fresh_document
    authorize = lambda: True
    await coordinator.lock.acquire()
    first = asyncio.create_task(observation.async_read_independent_tesla_vehicle(
        hass, VIN_A, allow_paid_refresh=True, authorize=authorize))
    second = asyncio.create_task(observation.async_read_independent_tesla_vehicle(
        hass, VIN_A, allow_paid_refresh=True,
        authorize=authorize if same_authorization else lambda: True))
    await asyncio.sleep(0)
    coordinator.lock.release()
    results = await asyncio.wait_for(asyncio.gather(first, second), 1)
    assert all(result is not None for result in results)
    assert len(client.refresh_calls) == (1 if same_authorization else 2)
    assert client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("paid_first", [False, True])
async def test_paid_and_default_in_flight_reads_are_separate_with_shared_freshness(independent_source, paid_first):
    hass, _entry, _vehicle, coordinator = independent_source("teslemetry")
    client = coordinator.api
    client.response["charge_state"]["timestamp"] -= 30

    async def refresh_cache():
        client.response = {"vin": VIN_A, **_charge(2_000_000_001)}
        client.refresh_response = {"response": deepcopy(client.response)}

    client.before_refresh_return = refresh_cache
    authorize = lambda: True
    await coordinator.lock.acquire()
    modes = [True, False] if paid_first else [False, True]
    tasks = [asyncio.create_task(observation.async_read_independent_tesla_vehicle(
        hass, VIN_A, allow_paid_refresh=paid, authorize=authorize)) for paid in modes]
    await asyncio.sleep(0)
    coordinator.lock.release()
    results = await asyncio.wait_for(asyncio.gather(*tasks), 1)
    by_mode = dict(zip(modes, results))
    assert by_mode[True] is not None
    assert by_mode[False] is None
    assert client.refresh_calls == [True]
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_paid_refresh_unknown_sdk_method_refuses_without_provider_io(independent_source, monkeypatch):
    hass, _entry, _vehicle, coordinator = independent_source("teslemetry")
    monkeypatch.delattr(type(coordinator.api), "data_refresh")
    assert await observation.async_read_independent_tesla_vehicle(
        hass, VIN_A, allow_paid_refresh=True, authorize=lambda: True) is None
    assert coordinator.api.refresh_calls == coordinator.api.calls == []


@pytest.mark.asyncio
async def test_paid_refresh_cancellation_preserves_other_authorized_waiter(independent_source):
    hass, _entry, _vehicle, coordinator = independent_source("teslemetry")
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold():
        entered.set()
        await release.wait()
        coordinator.api.refresh_response = {"response": deepcopy(coordinator.api.response)}

    coordinator.api.before_refresh_return = hold
    authorize = lambda: True
    first = asyncio.create_task(observation.async_read_independent_tesla_vehicle(
        hass, VIN_A, allow_paid_refresh=True, authorize=authorize))
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(observation.async_read_independent_tesla_vehicle(
        hass, VIN_A, allow_paid_refresh=True, authorize=authorize))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert await asyncio.wait_for(second, 1) is not None
    assert coordinator.api.refresh_calls == [True]
    assert coordinator.api.calls == []


@pytest.mark.asyncio
async def test_independent_read_waits_for_native_coordinator_lock_and_shares_one_request(independent_source):
    hass, _entry, _vehicle, coordinator = independent_source("teslemetry")
    await coordinator.lock.acquire()
    first = asyncio.create_task(observation.async_read_independent_tesla_vehicle(hass, VIN_A))
    second = asyncio.create_task(observation.async_read_independent_tesla_vehicle(hass, VIN_A))
    await asyncio.sleep(0)
    assert coordinator.api.calls == []
    coordinator.lock.release()
    results = await asyncio.wait_for(asyncio.gather(first, second), 1)
    assert all(result is not None for result in results)
    assert len(coordinator.api.calls) == 1


@pytest.mark.asyncio
async def test_independent_read_rejects_client_replacement_during_cloud_call(independent_source):
    hass, _entry, _vehicle, coordinator = independent_source()

    async def replace_client():
        coordinator.api = object()

    coordinator.api.before_return = replace_client
    assert await observation.async_read_independent_tesla_vehicle(hass, VIN_A) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["coordinator", "router", "missing_lock", "shutdown", "disabled", "unknown_state", "bool_requested"])
async def test_independent_read_requires_supported_cloud_source(independent_source, fault):
    hass, _entry, vehicle, coordinator = independent_source("teslemetry")
    client = coordinator.api
    preferred = None
    if fault == "coordinator":
        vehicle.coordinator = _Coordinator(_charge(1_999_999_990))
    elif fault == "router":
        coordinator.api = types.SimpleNamespace(vehicle_data=client.vehicle_data)
    elif fault == "missing_lock":
        coordinator._debounced_refresh = None
    elif fault == "shutdown":
        coordinator._shutdown_requested = True
    elif fault == "disabled":
        preferred = "switch.native_charge"
        hass.entity_registry = types.SimpleNamespace(async_get=lambda _entity: types.SimpleNamespace(
            platform="teslemetry", config_entry_id="native-entry", device_id="device-a",
            unique_id="native-charge", disabled_by="user"))
    elif fault == "unknown_state":
        client.response["charge_state"]["charging_state"] = "Unknown"
    else:
        client.response["charge_state"]["charge_current_request"] = True
    result = await observation.async_read_independent_tesla_vehicle(hass, VIN_A, preferred_entity_id=preferred)
    assert result is None
    if fault not in {"unknown_state", "bool_requested"}:
        assert client.calls == []


@pytest.mark.asyncio
async def test_independent_read_pins_preferred_provider_and_exact_vin(independent_source):
    hass, entry, vehicle, coordinator = independent_source("teslemetry")
    other = type(coordinator)()
    other.api.response["vin"] = VIN_B
    entry.runtime_data.vehicles.append(_vehicle(VIN_B, other, "device-b", "teslemetry"))
    preferred = "switch.native_b_charge"
    hass.entity_registry = types.SimpleNamespace(async_get=lambda _entity: types.SimpleNamespace(
        platform="teslemetry", config_entry_id="native-entry", device_id="device-b",
        unique_id="native-b-charge", disabled_by=None))
    result = await observation.async_read_independent_tesla_vehicle(hass, VIN_B, preferred_entity_id=preferred)
    assert result is not None
    assert result.vin == VIN_B
    assert result.device_id == "device-b"
    assert coordinator.api.calls == []
    assert len(other.api.calls) == 1


@pytest.mark.asyncio
async def test_independent_read_rechecks_registry_after_cloud_await(independent_source):
    hass, _entry, _vehicle, coordinator = independent_source()
    registration = types.SimpleNamespace(platform="tesla_fleet", config_entry_id="native-entry",
        device_id="device-a", unique_id="native-charge", disabled_by=None)
    hass.entity_registry = types.SimpleNamespace(async_get=lambda _entity: registration)

    async def rebind():
        registration.unique_id = "another-vehicle"

    coordinator.api.before_return = rebind
    assert await observation.async_read_independent_tesla_vehicle(
        hass, VIN_A, preferred_entity_id="switch.native_charge") is None


@pytest.mark.asyncio
async def test_independent_cancellation_does_not_cancel_other_cloud_waiter(independent_source):
    hass, _entry, _vehicle, coordinator = independent_source()
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold():
        entered.set()
        await release.wait()

    coordinator.api.before_return = hold
    first = asyncio.create_task(observation.async_read_independent_tesla_vehicle(hass, VIN_A))
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(observation.async_read_independent_tesla_vehicle(hass, VIN_A))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert await asyncio.wait_for(second, 1) is not None
    assert len(coordinator.api.calls) == 1


@pytest.mark.asyncio
async def test_independent_timeout_includes_waiting_for_existing_refresh(independent_source):
    hass, _entry, _vehicle, coordinator = independent_source()
    await coordinator.lock.acquire()
    try:
        assert await observation.async_read_independent_tesla_vehicle(hass, VIN_A, timeout=0.01) is None
        await asyncio.sleep(0)
        assert coordinator.api.calls == []
    finally:
        coordinator.lock.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["tesla_fleet", "teslemetry"])
async def test_native_vehicle_deviceinfo_mapping_retains_exact_registered_identity(domain):
    coordinator = _Coordinator(_charge(time_value := datetime.now(timezone.utc).timestamp() - 10))
    vehicle = _vehicle(VIN_A, coordinator, "device-a", domain)
    # HA's TeslaFleetVehicleData and TeslemetryVehicleData carry DeviceInfo
    # dictionaries, whereas DeviceEntry is an object in the device registry.
    vehicle.device = {"identifiers": {(domain, VIN_A)}}
    entry = _entry("native-entry", [vehicle])
    entity = "switch.native_car_charge"
    registration = types.SimpleNamespace(platform=domain, config_entry_id="native-entry",
        device_id="device-a", unique_id=f"{VIN_A}-charge", disabled_by=None)
    hass = _Hass({domain: [entry]}, registrations={entity: registration})
    hass.device_registry.devices["device-a"] = types.SimpleNamespace(
        id="device-a", identifiers={(domain, VIN_A)})

    async def refresh(coord):
        coord.data = _charge(datetime.now(timezone.utc).timestamp())
        coord.data["state"] = "online"

    coordinator.refresh = refresh
    result = await observation.async_read_tesla_vehicle(hass, VIN_A, preferred_entity_id=entity)
    assert result is not None and result.domain == domain and result.device_id == "device-a"
    assert result.source_charge_timestamp > time_value


@pytest.mark.asyncio
@pytest.mark.parametrize("rebind", [False, True])
async def test_fleet_command_preparation_supports_real_deviceinfo_mapping(rebind):
    coordinator = _Coordinator(_fleet_data("ONLINE", datetime.now(timezone.utc).timestamp() - 300))
    hass, entity = _fleet_command_hass(coordinator)
    source = hass.config_entries.async_entries("tesla_fleet")[0].runtime_data.vehicles[0]
    source.device = {"identifiers": {("tesla_fleet", VIN_A)}}

    async def refresh(coord):
        coord.data = _fleet_data("ASLEEP")
        if rebind:
            source.device["identifiers"] = {("tesla_fleet", VIN_B)}

    coordinator.refresh = refresh
    result = await observation.async_prepare_tesla_fleet_command(hass, VIN_A, entity)
    assert result is (not rebind)
    assert coordinator.refreshes == 1


@pytest.mark.parametrize("domain", ["tesla_fleet", "teslemetry"])
def test_independent_binding_pins_runtime_without_provider_calls(independent_source, domain):
    hass, _entry, vehicle, coordinator = independent_source(domain)
    binding = observation.independent_tesla_source_binding(hass, VIN_A)
    assert binding is not None
    assert binding["domain"] == domain
    assert binding["config_entry_id"] == "native-entry"
    assert binding["device_id"] == "device-a"
    assert binding == observation.independent_tesla_source_binding(hass, VIN_A)
    assert coordinator.api.calls == []

    coordinator.api = type(coordinator.api)()
    replacement = observation.independent_tesla_source_binding(hass, VIN_A)
    assert replacement is not None
    assert replacement["producer_identity"] == binding["producer_identity"]
    assert replacement["source_token"] != binding["source_token"]
    assert coordinator.api.calls == []
    vehicle.vin = VIN_B
    assert observation.independent_tesla_source_binding(hass, VIN_A) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["tesla_fleet", "teslemetry"])
async def test_independent_cloud_wake_uses_exact_api_class_and_waits_for_online(
    independent_source, domain, monkeypatch,
):
    hass, _entry, _vehicle, coordinator = independent_source(domain)
    binding = observation.independent_tesla_source_binding(hass, VIN_A)
    assert binding is not None

    async def immediate_sleep(_delay):
        return None

    monkeypatch.setattr(observation.asyncio, "sleep", immediate_sleep)
    coordinator.api.wake_response = {"response": {"state": "asleep"}}
    coordinator.api.vehicle_responses = [{"response": {"state": "online"}}]

    assert await observation.async_wake_independent_tesla_vehicle(
        hass, VIN_A, expected_binding=binding, authorize=lambda: True, timeout=1,
    )
    assert coordinator.api.wake_calls == [(False, 1.0)]
    assert len(coordinator.api.vehicle_responses) == 0
    assert coordinator.api.calls == []


@pytest.mark.asyncio
async def test_independent_cloud_wake_rejects_binding_mismatch_before_post(independent_source):
    hass, _entry, _vehicle, coordinator = independent_source("teslemetry")
    binding = observation.independent_tesla_source_binding(hass, VIN_A)
    assert binding is not None
    mismatched = {**binding, "device_id": "other-device"}

    assert not await observation.async_wake_independent_tesla_vehicle(
        hass, VIN_A, expected_binding=mismatched, authorize=lambda: True,
    )
    assert coordinator.api.wake_calls == []


@pytest.mark.asyncio
async def test_independent_cloud_wake_rejects_source_rebind_after_post(independent_source, monkeypatch):
    hass, _entry, _vehicle, coordinator = independent_source("tesla_fleet")

    async def replace_client(self, *, wait=False, timeout=60):
        coordinator.api = type(self)()
        return {"response": {"state": "online"}}

    monkeypatch.setattr(type(coordinator.api), "wake_up", replace_client)
    binding = observation.independent_tesla_source_binding(hass, VIN_A)
    assert binding is not None
    assert not await observation.async_wake_independent_tesla_vehicle(
        hass, VIN_A, expected_binding=binding, authorize=lambda: True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown_target", ["coordinator", "debouncer"])
async def test_independent_cloud_wake_rejects_shutdown_at_entry(
    independent_source, shutdown_target,
):
    hass, _entry, _vehicle, coordinator = independent_source("teslemetry")
    binding = observation.independent_tesla_source_binding(hass, VIN_A)
    assert binding is not None
    target = coordinator if shutdown_target == "coordinator" else coordinator._debounced_refresh
    target._shutdown_requested = True

    assert not await observation.async_wake_independent_tesla_vehicle(
        hass, VIN_A, expected_binding=binding, authorize=lambda: True,
    )
    assert coordinator.api.wake_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown_target", ["coordinator", "debouncer"])
async def test_independent_cloud_wake_rechecks_shutdown_after_lock_wait(
    independent_source, shutdown_target,
):
    hass, _entry, _vehicle, coordinator = independent_source("teslemetry")
    binding = observation.independent_tesla_source_binding(hass, VIN_A)
    assert binding is not None
    await coordinator.lock.acquire()
    task = asyncio.create_task(observation.async_wake_independent_tesla_vehicle(
        hass, VIN_A, expected_binding=binding, authorize=lambda: True,
    ))
    await asyncio.sleep(0)
    target = coordinator if shutdown_target == "coordinator" else coordinator._debounced_refresh
    target._shutdown_requested = True
    coordinator.lock.release()

    assert await asyncio.wait_for(task, 1) is False
    assert coordinator.api.wake_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown_target", ["coordinator", "debouncer"])
async def test_independent_cloud_wake_rechecks_shutdown_before_readiness_poll(
    independent_source, monkeypatch, shutdown_target,
):
    hass, _entry, _vehicle, coordinator = independent_source("teslemetry")
    binding = observation.independent_tesla_source_binding(hass, VIN_A)
    assert binding is not None
    target = coordinator if shutdown_target == "coordinator" else coordinator._debounced_refresh

    async def shutdown_wake(self, *, wait=False, timeout=60):
        self.wake_calls.append((wait, timeout))
        target._shutdown_requested = True
        return {"response": {"state": "asleep"}}

    monkeypatch.setattr(type(coordinator.api), "wake_up", shutdown_wake)
    coordinator.api.vehicle_responses = [{"response": {"state": "online"}}]
    assert not await observation.async_wake_independent_tesla_vehicle(
        hass, VIN_A, expected_binding=binding, authorize=lambda: True,
    )
    assert coordinator.api.wake_calls == [(False, 60.0)]
    assert len(coordinator.api.vehicle_responses) == 1


@pytest.mark.asyncio
async def test_independent_cloud_wake_is_exact_vin_scoped(independent_source):
    hass, entry, vehicle_a, coordinator_a = independent_source("teslemetry")
    coordinator_b = type(coordinator_a)()
    coordinator_b.api.response["vin"] = VIN_B
    entry.runtime_data.vehicles.append(
        type(vehicle_a)(vin=VIN_B, coordinator=coordinator_b,
                        device=types.SimpleNamespace(
                            id="device-b", identifiers={("teslemetry", VIN_B)}))
    )
    binding_b = observation.independent_tesla_source_binding(hass, VIN_B)
    assert binding_b is not None

    assert await observation.async_wake_independent_tesla_vehicle(
        hass, VIN_B, expected_binding=binding_b, authorize=lambda: True,
    )
    assert coordinator_a.api.wake_calls == []
    assert coordinator_b.api.wake_calls == [(False, 60.0)]


@pytest.mark.asyncio
async def test_independent_cloud_wake_cancellation_does_not_release_as_success(
    independent_source, monkeypatch,
):
    hass, _entry, _vehicle, coordinator = independent_source("teslemetry")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_wake(self, *, wait=False, timeout=60):
        entered.set()
        await release.wait()
        return {"response": {"state": "online"}}

    monkeypatch.setattr(type(coordinator.api), "wake_up", blocked_wake)
    binding = observation.independent_tesla_source_binding(hass, VIN_A)
    assert binding is not None
    task = asyncio.create_task(observation.async_wake_independent_tesla_vehicle(
        hass, VIN_A, expected_binding=binding, authorize=lambda: True,
    ))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()


@pytest.mark.asyncio
async def test_tessie_recovery_reads_source_state_without_coordinator_cache_or_wake(independent_source):
    hass, entry, vehicle, coordinator = independent_source("tessie")
    assert not hasattr(vehicle, "coordinator") # HA uses data_coordinator here
    first = await observation.async_read_independent_tesla_vehicle(hass, VIN_A)
    assert first is not None and first.domain == "tessie" and first.origin == "native_cloud"
    assert coordinator.api.calls == [{"use_cache": False}]
    assert coordinator.refreshes == 0 and not coordinator.api.wake_calls
    assert await observation.async_read_independent_tesla_vehicle(hass, VIN_A) is None
    coordinator.api.response["charge_state"]["timestamp"] += 1
    second = await observation.async_read_independent_tesla_vehicle(hass, VIN_A, updated_after=first.source_charge_timestamp)
    assert second is not None and second.source_charge_timestamp > first.source_charge_timestamp
    assert first.source_charge_timestamp == 2_000_000_000


@pytest.mark.asyncio
async def test_tessie_coordinator_refresh_uses_advancing_charge_timestamp(independent_source):
    hass, entry, vehicle, coordinator = independent_source("tessie")
    assert await observation.async_read_tesla_vehicle(hass, VIN_A) is None
    async def refresh(coord): coord.data = deepcopy(coord.api.response)
    coordinator.refresh = refresh
    result = await observation.async_read_tesla_vehicle(hass, VIN_A)
    assert result is not None and result.domain == "tessie"
    assert await observation.async_read_tesla_vehicle(hass, VIN_A) is None
