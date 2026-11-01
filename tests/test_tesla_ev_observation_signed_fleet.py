"""Contract tests for signed native Tesla Fleet clients."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import types

import pytest


VIN = "5YJ3E1EA7NF0000A1"
ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"
package = sys.modules.setdefault("power_sync", types.ModuleType("power_sync"))
package.__path__ = [str(ROOT)]


@pytest.fixture
def signed_fleet(monkeypatch):
    """Build one exact Fleet coordinator with a signed SDK client."""
    from conftest import pinned_sys_modules
    from power_sync import tesla_ev_observation as observation

    now = 2_000_000_000.0

    class VehicleFleet:
        def __init__(self):
            self.read_calls = []
            self.wake_calls = []
            self.payload_vin = VIN
            self.payload_state = "ONLINE"

        async def vehicle_data(self, *, endpoints):
            self.read_calls.append(tuple(endpoints))
            response = {
                    "state": self.payload_state,
                    "charge_state": {
                        "timestamp": now + 0.1,
                        "charging_state": "Stopped",
                        "charge_current_request": 16,
                        "charge_limit_soc": 80,
                        "charger_actual_current": 0,
                        "charger_power": 0,
                    },
            }
            if self.payload_vin is not None:
                response["vin"] = self.payload_vin
            return {"response": response}

        async def wake_up(self, *, wait=False, timeout=60):
            self.wake_calls.append((wait, timeout))
            return {"response": {"state": "online"}}

        async def vehicle(self):
            return {"response": {"state": "online"}}

    class VehicleSigned(VehicleFleet):
        pass

    class VehicleRouter(VehicleFleet):
        """A command-router subclass must not satisfy the cloud contract."""

    VehicleSigned.__module__ = "tesla_fleet_api.tesla.vehicle.signed"

    class Debouncer:
        def __init__(self):
            self.lock = asyncio.Lock()

        def async_lock(self):
            return self.lock

    class NativeCoordinator:
        def __init__(self, api):
            self.api = api
            self._debounced_refresh = Debouncer()
            self._shutdown_requested = False
            self.data = {"charge_state": {"timestamp": now - 10}}
            self.last_update_success = True

        async def async_request_refresh(self):
            self.data = (await type(self.api).vehicle_data(
                self.api, endpoints=["charge_state"]
            ))["response"]

    api_module = types.ModuleType("tesla_fleet_api.tesla")
    api_module.VehicleFleet = VehicleFleet
    api_module.VehicleSigned = VehicleSigned
    const_module = types.ModuleType("tesla_fleet_api.const")
    const_module.VehicleDataEndpoint = types.SimpleNamespace(CHARGE_STATE="charge_state")
    coordinator_module = types.ModuleType("homeassistant.components.tesla_fleet.coordinator")
    coordinator_module.TeslaFleetVehicleDataCoordinator = NativeCoordinator

    native_module = types.ModuleType("power_sync.native_rate_telemetry")
    entry = types.SimpleNamespace(entry_id="fleet-entry")
    device = types.SimpleNamespace(id="fleet-device", identifiers={("tesla_fleet", VIN)})
    vehicle = types.SimpleNamespace(vin=VIN, device=device)
    hass = types.SimpleNamespace(
        data={},
        device_registry=types.SimpleNamespace(devices={"fleet-device": device}),
        config_entries=types.SimpleNamespace(async_entries=lambda _domain: [entry]),
        entity_registry=types.SimpleNamespace(),
    )
    registration = types.SimpleNamespace(
        entity_id="switch.fleet_charge",
        platform="tesla_fleet",
        config_entry_id="fleet-entry",
        device_id="fleet-device",
        unique_id="fleet-charge",
        disabled_by=None,
    )
    hass.entity_registry.async_get = lambda entity_id: (
        registration if entity_id == registration.entity_id else None
    )

    def vehicle_sources(_hass, vin):
        if vin == VIN:
            return [("tesla_fleet", entry, vehicle, coordinator)]
        return []

    native_module.vehicle_sources = vehicle_sources
    coordinator = NativeCoordinator(VehicleSigned())
    vehicle.coordinator = coordinator
    entry.runtime_data = types.SimpleNamespace(vehicles=[vehicle])

    # Pin the exact module tree used by lazy production imports.  The helper
    # itself is also pinned to this test's registry shape so collection-order
    # HA stubs cannot redirect source identity.
    modules = {
        "power_sync.native_rate_telemetry": native_module,
        "homeassistant.components.tesla_fleet.coordinator": coordinator_module,
        "tesla_fleet_api.tesla": api_module,
        "tesla_fleet_api.const": const_module,
    }
    monkeypatch.setattr(observation, "time", types.SimpleNamespace(time=lambda: now))
    monkeypatch.setattr(observation, "_device_registry", lambda _hass: hass.device_registry)
    monkeypatch.setattr(observation, "_entity_registration", lambda _hass, entity_id: hass.entity_registry.async_get(entity_id))
    with pinned_sys_modules(modules):
        yield types.SimpleNamespace(
            observation=observation,
            hass=hass,
            source_api=coordinator.api,
            vehicle_fleet=VehicleFleet,
            vehicle_signed=VehicleSigned,
            vehicle_router=VehicleRouter,
            api_module=api_module,
            coordinator=coordinator,
            now=now,
            command_entity=registration.entity_id,
        )


def test_signed_fleet_client_is_accepted_for_exact_independent_read_and_wake(signed_fleet):
    observation = signed_fleet.observation
    assert signed_fleet.vehicle_signed.__module__ == "tesla_fleet_api.tesla.vehicle.signed"
    assert signed_fleet.vehicle_fleet in signed_fleet.vehicle_signed.__mro__[1:]
    binding = observation.independent_tesla_source_binding(signed_fleet.hass, VIN)
    assert binding is not None

    read = asyncio.run(
        observation.async_read_independent_tesla_vehicle(
            signed_fleet.hass, VIN
        )
    )
    assert read is not None
    assert read.domain == "tesla_fleet"
    assert read.charge_state == "Stopped"
    assert signed_fleet.source_api.read_calls == [("charge_state",)]

    assert asyncio.run(
        observation.async_wake_independent_tesla_vehicle(
            signed_fleet.hass, VIN, expected_binding=binding, timeout=1
        )
    ) is True
    assert signed_fleet.source_api.wake_calls == [(False, 1.0)]


def test_arbitrary_fleet_subclass_is_rejected(signed_fleet):
    signed_fleet.coordinator.api = signed_fleet.vehicle_router()
    assert observation_contract(signed_fleet) is None


def test_missing_signed_export_does_not_broaden_to_arbitrary_subclass(signed_fleet, monkeypatch):
    signed_fleet.coordinator.api = signed_fleet.vehicle_router()
    monkeypatch.delattr(signed_fleet.api_module, "VehicleSigned")
    assert observation_contract(signed_fleet) is None

    signed_fleet.coordinator.api = signed_fleet.vehicle_fleet()
    assert observation_contract(signed_fleet) is not None


def test_corrupt_signed_export_is_rejected(signed_fleet, monkeypatch):
    signed_fleet.coordinator.api = signed_fleet.vehicle_router()
    class CorruptSigned(signed_fleet.vehicle_fleet):
        pass

    monkeypatch.setattr(signed_fleet.api_module, "VehicleSigned", CorruptSigned)
    assert observation_contract(signed_fleet) is None


@pytest.mark.parametrize(
    ("payload_vin", "accepted"),
    ((VIN, True), (None, True), ("5YJ3E1EA7NF0000B9", False), ("malformed", False)),
)
def test_ordinary_reader_validates_explicit_payload_vin(signed_fleet, payload_vin, accepted):
    signed_fleet.source_api.payload_vin = payload_vin
    result = asyncio.run(
        signed_fleet.observation.async_read_tesla_vehicle(signed_fleet.hass, VIN)
    )
    assert (result is not None) is accepted


def test_cached_reader_rejects_explicit_payload_vin_for_another_vehicle(signed_fleet):
    signed_fleet.coordinator.data = {
        "vin": "5YJ3E1EA7NF0000B9",
        "charge_state": {
            "timestamp": signed_fleet.now,
            "charging_state": "Stopped",
            "charge_current_request": 16,
            "charge_limit_soc": 80,
            "charger_actual_current": 0,
            "charger_power": 0,
        },
    }
    assert signed_fleet.observation.get_tesla_vehicle_observation(signed_fleet.hass, VIN) is None


def test_fleet_preparation_rejects_wrong_explicit_vin_before_state_admission(signed_fleet):
    signed_fleet.source_api.payload_vin = "5YJ3E1EA7NF0000B9"
    signed_fleet.source_api.payload_state = "ASLEEP"
    assert asyncio.run(
        signed_fleet.observation.async_prepare_tesla_fleet_command(
            signed_fleet.hass, VIN, signed_fleet.command_entity
        )
    ) is False


def observation_contract(harness):
    """Resolve the real source through the production discovery path."""
    source = harness.observation._select_source(harness.hass, VIN, None)
    assert source is not None
    return harness.observation._cloud_contract(source)
