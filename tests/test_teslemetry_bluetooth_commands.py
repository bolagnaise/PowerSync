"""Focused registry tests for the Teslemetry Bluetooth Commands adapter."""

from dataclasses import dataclass
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


_SOURCE = (
    Path(__file__).parents[1]
    / "custom_components"
    / "power_sync"
    / "teslemetry_bluetooth_commands.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "teslemetry_bluetooth_commands_probe", _SOURCE
)
assert _SPEC is not None and _SPEC.loader is not None
MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = MODULE
_SPEC.loader.exec_module(MODULE)


VIN_A = "5YJ3E1EA7KF000001"
VIN_B = "5YJSA1E2XLF000002"


@dataclass
class Entity:
    entity_id: str
    unique_id: str
    platform: str
    config_entry_id: str
    device_id: str
    disabled_by: str | None = None


class Registry:
    def __init__(self, entities: list[Entity]) -> None:
        self.entities = {entity.entity_id: entity for entity in entities}


class State:
    def __init__(self, value: str = "unknown") -> None:
        self.state = value


class States:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = {entity_id: State(value) for entity_id, value in values.items()}

    def get(self, entity_id: str):
        return self._values.get(entity_id)


class DeviceRegistry:
    def __init__(self, devices: dict[str, object]) -> None:
        self._devices = devices

    def async_get(self, device_id: str):
        return self._devices.get(device_id)


class ConfigEntries:
    def __init__(self, entries: list[object]) -> None:
        self._entries = entries

    def async_entries(self, domain: str) -> list[object]:
        return [entry for entry in self._entries if entry.domain == domain]


@pytest.fixture
def harness(monkeypatch):
    entries = [
        SimpleNamespace(entry_id="native-a", domain="teslemetry"),
        SimpleNamespace(entry_id="native-b", domain="teslemetry"),
        SimpleNamespace(entry_id="commands-a", domain=MODULE.DOMAIN),
        SimpleNamespace(entry_id="commands-b", domain=MODULE.DOMAIN),
    ]
    devices = {
        "device-a": SimpleNamespace(
            identifiers={("teslemetry", VIN_A)},
            config_entries={"native-a", "commands-a"},
        ),
        "device-b": SimpleNamespace(
            identifiers={("teslemetry", VIN_B)},
            config_entries={"native-b", "commands-b"},
        ),
    }
    entities: list[Entity] = []
    state_values: dict[str, str] = {}

    def add_vehicle(vin: str, device_id: str, native_entry: str, command_entry: str):
        suffix = vin[-4:].lower()
        for operation, (domain, key) in MODULE._COMMANDS.items():
            entity_id = f"{domain}.renamed_{operation}_{suffix}"
            entities.append(
                Entity(
                    entity_id=entity_id,
                    unique_id=f"{vin}-bluetooth-{key}",
                    platform=MODULE.DOMAIN,
                    config_entry_id=command_entry,
                    device_id=device_id,
                )
            )
            # Buttons and command numbers are write-only.  Unknown is valid.
            state_values[entity_id] = "unknown"
        native_entity_id = f"number.native_current_{suffix}"
        entities.append(
            Entity(
                entity_id=native_entity_id,
                unique_id=f"{vin}-charge_state_charge_current_request",
                platform=MODULE.TESLEMETRY_DOMAIN,
                config_entry_id=native_entry,
                device_id=device_id,
            )
        )
        state_values[native_entity_id] = "10"
        for native_entity_id, native_key, value in (
            (
                f"switch.native_charging_state_{suffix}",
                "charge_state_user_charge_enable_request",
                "off",
            ),
            (
                f"number.native_charge_limit_{suffix}",
                "charge_state_charge_limit_soc",
                "80",
            ),
        ):
            entities.append(
                Entity(
                    entity_id=native_entity_id,
                    unique_id=f"{vin}-{native_key}",
                    platform=MODULE.TESLEMETRY_DOMAIN,
                    config_entry_id=native_entry,
                    device_id=device_id,
                )
            )
            state_values[native_entity_id] = value

    add_vehicle(VIN_A, "device-a", "native-a", "commands-a")
    add_vehicle(VIN_B, "device-b", "native-b", "commands-b")

    entity_registry = Registry(entities)
    device_registry = DeviceRegistry(devices)
    homeassistant = ModuleType("homeassistant")
    homeassistant.__path__ = []
    helpers = ModuleType("homeassistant.helpers")
    helpers.__path__ = []
    entity_registry_module = ModuleType("homeassistant.helpers.entity_registry")
    entity_registry_module.async_get = lambda hass: entity_registry
    device_registry_module = ModuleType("homeassistant.helpers.device_registry")
    device_registry_module.async_get = lambda hass: device_registry
    monkeypatch.setitem(sys.modules, "homeassistant", homeassistant)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers)
    monkeypatch.setitem(
        sys.modules, "homeassistant.helpers.entity_registry", entity_registry_module
    )
    monkeypatch.setitem(
        sys.modules, "homeassistant.helpers.device_registry", device_registry_module
    )

    return SimpleNamespace(
        hass=SimpleNamespace(
            config_entries=ConfigEntries(entries),
            states=States(state_values),
        ),
        entities=entities,
        states=state_values,
        devices=devices,
        registry=entity_registry,
    )


def test_all_commands_resolve_renamed_entities_and_unknown_numbers_are_valid(harness):
    for vin, native_entry in ((VIN_A, "native-a"), (VIN_B, "native-b")):
        for operation in ("start", "stop", "rate", "limit"):
            target = MODULE.resolve_command(harness.hass, vin.lower(), operation)
            assert target is not None
            assert target.vin == vin
            assert target.operation == operation
            assert target.entity_id.startswith(
                "button." if operation in ("start", "stop") else "number."
            )
            assert target.native_config_entry_ids == (native_entry,)


def test_exact_vin_and_operation_are_required(harness):
    assert MODULE.resolve_command(harness.hass, "ble_" + VIN_A, "start") is None
    assert MODULE.resolve_command(harness.hass, "not-a-vin", "start") is None
    assert MODULE.resolve_command(harness.hass, VIN_A, "unknown") is None
    assert MODULE.resolve_command(harness.hass, VIN_A, 1) is None
    assert MODULE.resolve_command(harness.hass, VIN_B, "stop").device_id == "device-b"


def test_duplicate_unique_id_fails_closed(harness):
    source = next(
        entity for entity in harness.entities
        if entity.unique_id == f"{VIN_A}-bluetooth-charge_start"
    )
    harness.entities.append(
        Entity(
            entity_id="button.duplicate_charge_start",
            unique_id=source.unique_id,
            platform=source.platform,
            config_entry_id=source.config_entry_id,
            device_id=source.device_id,
        )
    )
    harness.registry.entities["button.duplicate_charge_start"] = harness.entities[-1]
    assert MODULE.resolve_command(harness.hass, VIN_A, "start") is None


@pytest.mark.parametrize("mode", ("disabled", "unavailable"))
def test_disabled_or_unavailable_entity_is_rejected(harness, mode):
    source = next(
        entity for entity in harness.entities
        if entity.unique_id == f"{VIN_A}-bluetooth-set_charging_amps"
    )
    if mode == "disabled":
        source.disabled_by = "user"
    else:
        harness.hass.states._values[source.entity_id].state = "unavailable"
    assert MODULE.resolve_command(harness.hass, VIN_A, "rate") is None


def test_wrong_platform_or_device_identity_is_rejected(harness):
    source = next(
        entity for entity in harness.entities
        if entity.unique_id == f"{VIN_A}-bluetooth-charge_stop"
    )
    source.platform = "teslemetry"
    assert MODULE.resolve_command(harness.hass, VIN_A, "stop") is None
    source.platform = MODULE.DOMAIN
    source.config_entry_id = "native-a"
    assert MODULE.resolve_command(harness.hass, VIN_A, "stop") is None
    source.config_entry_id = "commands-a"
    harness.devices[source.device_id].identifiers = {("teslemetry", VIN_B)}
    assert MODULE.resolve_command(harness.hass, VIN_A, "stop") is None


def test_native_current_helper_uses_same_producer_device(harness):
    target = MODULE.resolve_native_current(harness.hass, VIN_A.lower())
    assert target is not None
    assert target.operation == "current"
    assert target.entity_id == "number.native_current_0001"
    assert target.device_id == "device-a"
    assert target.native_config_entry_ids == ("native-a",)
    command = MODULE.resolve_command(harness.hass, VIN_A, "rate")
    assert command is not None
    assert MODULE.native_current_entity(harness.hass, command) == target.entity_id
    assert MODULE.native_current_entity(harness.hass, command.entity_id) == target.entity_id


def test_native_current_duplicate_fails_closed(harness):
    source = next(
        entity for entity in harness.entities
        if entity.unique_id == f"{VIN_A}-charge_state_charge_current_request"
    )
    duplicate = Entity(
        entity_id="number.native_current_duplicate",
        unique_id=source.unique_id,
        platform=source.platform,
        config_entry_id=source.config_entry_id,
        device_id=source.device_id,
    )
    harness.entities.append(duplicate)
    harness.registry.entities[duplicate.entity_id] = duplicate
    assert MODULE.resolve_native_current(harness.hass, VIN_A) is None


def test_native_current_alias_or_other_device_fails_closed(harness):
    source = next(
        entity for entity in harness.entities
        if entity.unique_id == f"{VIN_A}-charge_state_charge_current_request"
    )
    source.unique_id = f"{VIN_A}-charging_amps"
    assert MODULE.resolve_native_current(harness.hass, VIN_A) is None
    source.unique_id = f"{VIN_A}-charge_state_charge_current_request"
    source.device_id = "device-b"
    assert MODULE.native_current_entity(harness.hass, VIN_A) is None


@pytest.mark.parametrize(
    ("operation", "expected"),
    (
        ("start", "switch.native_charging_state_0001"),
        ("stop", "switch.native_charging_state_0001"),
        ("rate", "number.native_current_0001"),
        ("limit", "number.native_charge_limit_0001"),
    ),
)
def test_native_command_entity_requires_shared_vehicle_identity(harness, operation, expected):
    command = MODULE.resolve_command(harness.hass, VIN_A, operation)
    assert command is not None
    assert MODULE.native_command_entity(harness.hass, command) == expected
    harness.devices[command.device_id].identifiers = {("teslemetry", VIN_B)}
    assert MODULE.native_command_entity(harness.hass, command) is None


def test_native_command_entity_resolves_exact_provider_anchor_for_all_operations(harness):
    fleet_entry = SimpleNamespace(entry_id="fleet-a", domain="tesla_fleet")
    harness.hass.config_entries._entries.append(fleet_entry)
    fleet_device = SimpleNamespace(
        identifiers={("tesla_fleet", VIN_A)}, config_entries={"fleet-a"}
    )
    harness.devices["fleet-device"] = fleet_device
    harness.hass.states._values.update({
        "switch.fleet_charge": State("off"),
        "number.fleet_charge_current": State("16"),
        "number.fleet_charge_limit": State("80"),
    })
    fleet_entities = [
        Entity("switch.fleet_charge", "fleet-charge", "tesla_fleet", "fleet-a", "fleet-device"),
        Entity("number.fleet_charge_current", "fleet-current", "tesla_fleet", "fleet-a", "fleet-device"),
        Entity("number.fleet_charge_limit", "fleet-limit", "tesla_fleet", "fleet-a", "fleet-device"),
    ]
    harness.entities.extend(fleet_entities)
    harness.registry.entities.update({entity.entity_id: entity for entity in fleet_entities})

    assert MODULE.native_command_entity(harness.hass, "switch.fleet_charge", "start") == "switch.fleet_charge"
    assert MODULE.native_command_entity(harness.hass, "switch.fleet_charge", "stop") == "switch.fleet_charge"
    assert MODULE.native_command_entity(harness.hass, "number.fleet_charge_current", "rate") == "number.fleet_charge_current"
    assert MODULE.native_command_entity(harness.hass, "number.fleet_charge_limit", "limit") == "number.fleet_charge_limit"
    assert MODULE.native_command_entity(harness.hass, "number.native_current_0001", "rate") == "number.native_current_0001"
