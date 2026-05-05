"""Regression tests for Neovolt entity bridge discovery and dispatch control."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"


def _install_stubs() -> None:
    ha_root = types.ModuleType("homeassistant")
    ha_helpers = types.ModuleType("homeassistant.helpers")
    ha_entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")

    ha_entity_registry.async_get = lambda hass: hass.entity_registry
    ha_entity_registry.async_entries_for_config_entry = (
        lambda registry, entry_id: registry.entries_for(entry_id)
    )

    ha_helpers.entity_registry = ha_entity_registry
    ha_root.helpers = ha_helpers

    sys.modules["homeassistant"] = ha_root
    sys.modules["homeassistant.helpers"] = ha_helpers
    sys.modules["homeassistant.helpers.entity_registry"] = ha_entity_registry

    power_sync = types.ModuleType("power_sync")
    power_sync.__path__ = [str(COMPONENT_ROOT)]
    sys.modules["power_sync"] = power_sync

    inverters = types.ModuleType("power_sync.inverters")
    inverters.__path__ = [str(COMPONENT_ROOT / "inverters")]
    sys.modules["power_sync.inverters"] = inverters


_install_stubs()

from power_sync.inverters.neovolt import NeovoltBatteryController  # noqa: E402


class _FakeState:
    def __init__(self, entity_id: str, state: str = "0", options: list[str] | None = None):
        self.entity_id = entity_id
        self.state = state
        self.attributes = {"options": options or []}


class _FakeStates:
    def __init__(self, states: list[_FakeState]):
        self._states = {state.entity_id: state for state in states}

    def get(self, entity_id: str | None):
        return self._states.get(entity_id or "")

    def async_all(self, domain: str | None = None):
        if domain is None:
            return list(self._states.values())
        prefix = f"{domain}."
        return [state for state in self._states.values() if state.entity_id.startswith(prefix)]


class _FakeServices:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    async def async_call(self, domain: str, service: str, data: dict, blocking: bool = True):
        self.calls.append((domain, service, dict(data)))


class _FakeRegistry:
    def __init__(self, entries: dict[str, list[str]] | None = None):
        self._entries = entries or {}

    def entries_for(self, entry_id: str):
        return [
            SimpleNamespace(
                entity_id=entity_id,
                unique_id=entity_id.split(".", 1)[1],
            )
            for entity_id in self._entries.get(entry_id, [])
        ]


class _FakeHass:
    def __init__(
        self,
        states: list[_FakeState],
        registry_entries: dict[str, list[str]] | None = None,
    ):
        self.states = _FakeStates(states)
        self.services = _FakeServices()
        self.entity_registry = _FakeRegistry(registry_entries)


def _control_states() -> list[_FakeState]:
    return [
        _FakeState(
            "select.neovolt_1_dispatch_mode",
            "Normal",
            ["Normal", "Force Charge", "Force Discharge", "Dynamic Export", "Dynamic Import"],
        ),
        _FakeState("number.neovolt_1_dispatch_power", "3.0"),
        _FakeState("number.neovolt_1_dispatch_duration", "120"),
        _FakeState("number.neovolt_1_dispatch_charge_target_soc", "100"),
        _FakeState("number.neovolt_1_dispatch_discharge_cutoff_soc", "10"),
        _FakeState("number.neovolt_1_discharging_cutoff_soc_default", "20"),
        _FakeState("button.neovolt_1_stop_force_charge_discharge", "unknown"),
    ]


def _combined_states() -> list[_FakeState]:
    return [
        _FakeState("sensor.neovolt_1_combined_battery_power", "-1200"),
        _FakeState("sensor.neovolt_1_combined_battery_soc", "56"),
        _FakeState("sensor.neovolt_1_combined_battery_capacity", "20.1"),
        _FakeState("sensor.neovolt_1_combined_battery_soh", "98"),
        _FakeState("sensor.neovolt_1_combined_house_load", "2500"),
        _FakeState("sensor.neovolt_1_combined_pv_power", "5000"),
        _FakeState("sensor.neovolt_1_battery_power", "800"),
        _FakeState("sensor.neovolt_1_battery_soc", "51"),
        _FakeState("sensor.neovolt_1_battery_capacity", "10.0"),
        _FakeState("sensor.neovolt_1_total_house_load", "1500"),
        _FakeState("sensor.neovolt_1_pv_total_active_power", "2000"),
        _FakeState("sensor.neovolt_1_grid_total_active_power", "800"),
    ]


def _host_states() -> list[_FakeState]:
    return [
        _FakeState("sensor.neovolt_1_battery_power", "1800"),
        _FakeState("sensor.neovolt_1_battery_soc", "63"),
        _FakeState("sensor.neovolt_1_battery_capacity", "20.1"),
        _FakeState("sensor.neovolt_1_battery_soh", "97"),
        _FakeState("sensor.neovolt_1_total_house_load", "3100"),
        _FakeState("sensor.neovolt_1_pv_total_active_power", "2600"),
        _FakeState("sensor.neovolt_1_grid_total_active_power", "-500"),
    ]


def _hass_for(states: list[_FakeState]) -> _FakeHass:
    entity_ids = [state.entity_id for state in states]
    return _FakeHass(states, {"neovolt-entry": entity_ids})


def test_config_entry_discovery_prefers_combined_sensors():
    hass = _hass_for(_combined_states() + _control_states())
    controller = NeovoltBatteryController(hass, "neovolt-entry")

    assert asyncio.run(controller.connect())
    assert controller._entity_map["battery_power"] == "sensor.neovolt_1_combined_battery_power"
    assert controller._entity_map["battery_level"] == "sensor.neovolt_1_combined_battery_soc"
    assert controller._entity_map["battery_capacity_kwh"] == "sensor.neovolt_1_combined_battery_capacity"
    assert controller._entity_map["load_power"] == "sensor.neovolt_1_combined_house_load"
    assert controller._entity_map["solar_power"] == "sensor.neovolt_1_combined_pv_power"

    status = controller.get_status()
    assert status["battery_power"] == -1.2
    assert status["grid_power"] == 0.8
    assert status["load_power"] == 2.5
    assert status["solar_power"] == 5.0
    assert status["battery_level"] == 56.0
    assert status["battery_capacity_kwh"] == 20.1
    assert status["battery_soh"] == 98.0


def test_host_sensor_fallback_and_power_signs():
    hass = _hass_for(_host_states() + _control_states())
    controller = NeovoltBatteryController(hass, "neovolt-entry")

    assert asyncio.run(controller.connect())
    status = controller.get_status()

    assert controller._entity_map["battery_power"] == "sensor.neovolt_1_battery_power"
    assert status["battery_power"] == 1.8
    assert status["grid_power"] == -0.5
    assert status["load_power"] == 3.1
    assert status["solar_power"] == 2.6


def test_force_charge_writes_numbers_before_mode_select():
    hass = _hass_for(_combined_states() + _control_states())
    controller = NeovoltBatteryController(hass, "neovolt-entry")
    assert asyncio.run(controller.connect())

    assert asyncio.run(controller.force_charge(duration_minutes=30, power_w=2500))

    assert hass.services.calls[:4] == [
        ("number", "set_value", {"entity_id": "number.neovolt_1_dispatch_power", "value": 2.5}),
        ("number", "set_value", {"entity_id": "number.neovolt_1_dispatch_duration", "value": 30}),
        ("number", "set_value", {"entity_id": "number.neovolt_1_dispatch_charge_target_soc", "value": 100}),
        ("select", "select_option", {"entity_id": "select.neovolt_1_dispatch_mode", "option": "Force Charge"}),
    ]


def test_force_discharge_and_backup_reserve_service_calls():
    hass = _hass_for(_combined_states() + _control_states())
    controller = NeovoltBatteryController(
        hass,
        "neovolt-entry",
        min_soc_pct=30,
    )
    assert asyncio.run(controller.connect())

    assert asyncio.run(controller.force_discharge(duration_minutes=45, power_w=3200))
    assert asyncio.run(controller.set_backup_reserve(35))

    assert hass.services.calls[:5] == [
        ("number", "set_value", {"entity_id": "number.neovolt_1_dispatch_power", "value": 3.2}),
        ("number", "set_value", {"entity_id": "number.neovolt_1_dispatch_duration", "value": 45}),
        ("number", "set_value", {"entity_id": "number.neovolt_1_dispatch_discharge_cutoff_soc", "value": 30}),
        ("select", "select_option", {"entity_id": "select.neovolt_1_dispatch_mode", "option": "Force Discharge"}),
        ("number", "set_value", {"entity_id": "number.neovolt_1_discharging_cutoff_soc_default", "value": 35}),
    ]


def test_restore_normal_uses_select_without_stop_button():
    hass = _hass_for(_combined_states() + _control_states())
    controller = NeovoltBatteryController(hass, "neovolt-entry")
    assert asyncio.run(controller.connect())

    assert asyncio.run(controller.restore_normal())

    assert hass.services.calls == [
        ("select", "select_option", {"entity_id": "select.neovolt_1_dispatch_mode", "option": "Normal"}),
    ]


def test_missing_dispatch_mode_fails_validation():
    states = [
        state
        for state in (_combined_states() + _control_states())
        if state.entity_id != "select.neovolt_1_dispatch_mode"
    ]
    controller = NeovoltBatteryController(_hass_for(states), "neovolt-entry")

    with pytest.raises(ValueError, match="neovolt_missing_entities"):
        asyncio.run(controller.connect())
