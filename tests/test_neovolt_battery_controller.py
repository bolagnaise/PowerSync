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

from power_sync.inverters.neovolt import (  # noqa: E402
    NeovoltBatteryController,
    NeovoltFleetBatteryController,
)


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


def _control_states_for(index: int, mode: str = "Normal") -> list[_FakeState]:
    prefix = f"neovolt_{index}"
    return [
        _FakeState(
            f"select.{prefix}_dispatch_mode",
            mode,
            [
                "Normal",
                "Force Charge",
                "Force Discharge",
                "Dynamic Export",
                "Dynamic Import",
                "No Battery Charge",
                "Idle (No Dispatch)",
            ],
        ),
        _FakeState(f"number.{prefix}_dispatch_power", "3.0"),
        _FakeState(f"number.{prefix}_dispatch_duration", "120"),
        _FakeState(f"number.{prefix}_dispatch_charge_target_soc", "100"),
        _FakeState(f"number.{prefix}_dispatch_discharge_cutoff_soc", "10"),
        _FakeState(f"number.{prefix}_discharging_cutoff_soc_default", "20"),
        _FakeState(f"button.{prefix}_stop_force_charge_discharge", "unknown"),
    ]


def _combined_states_for(
    index: int,
    *,
    battery_power: str,
    battery_soc: str,
    battery_capacity: str,
    battery_soh: str = "100",
    house_load: str = "0",
    pv_power: str = "0",
    grid_power: str = "0",
) -> list[_FakeState]:
    prefix = f"neovolt_{index}"
    return [
        _FakeState(f"sensor.{prefix}_combined_battery_power", battery_power),
        _FakeState(f"sensor.{prefix}_combined_battery_soc", battery_soc),
        _FakeState(f"sensor.{prefix}_combined_battery_capacity", battery_capacity),
        _FakeState(f"sensor.{prefix}_combined_battery_soh", battery_soh),
        _FakeState(f"sensor.{prefix}_combined_house_load", house_load),
        _FakeState(f"sensor.{prefix}_combined_pv_power", pv_power),
        _FakeState(f"sensor.{prefix}_grid_total_active_power", grid_power),
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


def test_config_entry_discovery_prefers_selected_entry_over_fallback_states():
    entry_1_states = _combined_states_for(
        1,
        battery_power="-3454",
        battery_soc="44",
        battery_capacity="20.1",
        house_load="0",
        pv_power="0",
        grid_power="20",
    ) + _control_states_for(1)
    entry_2_states = _combined_states_for(
        2,
        battery_power="5376",
        battery_soc="20",
        battery_capacity="30.2",
        house_load="5386",
        pv_power="0",
        grid_power="19",
    ) + _control_states_for(2)
    hass = _FakeHass(
        entry_1_states + entry_2_states,
        {
            "neovolt-1": [state.entity_id for state in entry_1_states],
            "neovolt-2": [state.entity_id for state in entry_2_states],
        },
    )
    controller = NeovoltBatteryController(hass, "neovolt-2")

    assert asyncio.run(controller.connect())

    assert controller._entity_map["battery_power"] == "sensor.neovolt_2_combined_battery_power"
    assert controller._entity_map["dispatch_mode"] == "select.neovolt_2_dispatch_mode"
    assert controller.get_status()["battery_level"] == 20.0


def test_fleet_status_aggregates_power_and_capacity_weighted_soc():
    entry_1_states = _combined_states_for(
        1,
        battery_power="-3454",
        battery_soc="44",
        battery_capacity="20.1",
        battery_soh="99",
        house_load="0",
        pv_power="0",
        grid_power="20",
    ) + _control_states_for(1)
    entry_2_states = _combined_states_for(
        2,
        battery_power="5376",
        battery_soc="20",
        battery_capacity="30.2",
        battery_soh="97",
        house_load="5386",
        pv_power="0",
        grid_power="19",
    ) + _control_states_for(2)
    hass = _FakeHass(
        entry_1_states + entry_2_states,
        {
            "neovolt-1": [state.entity_id for state in entry_1_states],
            "neovolt-2": [state.entity_id for state in entry_2_states],
        },
    )
    controller = NeovoltFleetBatteryController(
        hass,
        ["neovolt-1", "neovolt-2"],
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
    )

    assert asyncio.run(controller.connect())
    status = controller.get_status()

    assert status["battery_power"] == pytest.approx(1.922)
    assert status["grid_power"] == pytest.approx(0.039)
    assert status["load_power"] == pytest.approx(1.961)
    assert status["battery_capacity_kwh"] == pytest.approx(50.3)
    assert status["battery_level"] == pytest.approx(29.5904, rel=1e-4)
    assert status["battery_soh"] == pytest.approx(97.7992, rel=1e-4)
    assert status["battery_max_charge_power_w"] == 10000.0
    assert status["battery_max_discharge_power_w"] == 10000.0


def test_fleet_status_derives_load_from_net_power_balance():
    """Dual Neovolt entries can expose gross per-inverter load, not site load."""
    entry_1_states = _combined_states_for(
        1,
        battery_power="4336",
        battery_soc="30",
        battery_capacity="20.1",
        house_load="5968",
        pv_power="1632",
        grid_power="0",
    ) + _control_states_for(1)
    entry_2_states = _combined_states_for(
        2,
        battery_power="-4661",
        battery_soc="20.8",
        battery_capacity="30.2",
        house_load="-4679",
        pv_power="0",
        grid_power="-7",
    ) + _control_states_for(2)
    hass = _FakeHass(
        entry_1_states + entry_2_states,
        {
            "neovolt-1": [state.entity_id for state in entry_1_states],
            "neovolt-2": [state.entity_id for state in entry_2_states],
        },
    )
    controller = NeovoltFleetBatteryController(
        hass,
        ["neovolt-1", "neovolt-2"],
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
    )

    assert asyncio.run(controller.connect())
    status = controller.get_status()

    assert status["solar_power"] == pytest.approx(1.632)
    assert status["grid_power"] == pytest.approx(-0.007)
    assert status["battery_power"] == pytest.approx(-0.325)
    assert status["load_power"] == pytest.approx(1.3)


def test_fleet_status_uses_available_inverter_when_an_entry_has_no_live_states():
    entry_2_states = _combined_states_for(
        2,
        battery_power="9",
        battery_soc="14",
        battery_capacity="30.2",
        battery_soh="100",
        house_load="0",
        pv_power="0",
        grid_power="-8",
    ) + _control_states_for(2)
    hass = _FakeHass(
        entry_2_states,
        {
            "missing-neovolt-1": [
                state.entity_id
                for state in (
                    _combined_states_for(
                        1,
                        battery_power="0",
                        battery_soc="0",
                        battery_capacity="20.1",
                    ) + _control_states_for(1)
                )
            ],
            "neovolt-2": [state.entity_id for state in entry_2_states],
        },
    )
    controller = NeovoltFleetBatteryController(
        hass,
        ["missing-neovolt-1", "neovolt-2"],
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
    )

    status = controller.get_status()

    assert status["battery_power"] == pytest.approx(0.009)
    assert status["grid_power"] == pytest.approx(-0.008)
    assert status["battery_capacity_kwh"] == pytest.approx(30.2)
    assert status["battery_level"] == pytest.approx(14.0)
    assert status["battery_soh"] == pytest.approx(100.0)


def test_fleet_force_charge_writes_all_inverter_dispatch_controls():
    entry_1_states = _combined_states_for(
        1,
        battery_power="0",
        battery_soc="50",
        battery_capacity="20.1",
    ) + _control_states_for(1)
    entry_2_states = _combined_states_for(
        2,
        battery_power="0",
        battery_soc="50",
        battery_capacity="30.2",
    ) + _control_states_for(2)
    hass = _FakeHass(
        entry_1_states + entry_2_states,
        {
            "neovolt-1": [state.entity_id for state in entry_1_states],
            "neovolt-2": [state.entity_id for state in entry_2_states],
        },
    )
    controller = NeovoltFleetBatteryController(
        hass,
        ["neovolt-1", "neovolt-2"],
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
    )
    assert asyncio.run(controller.connect())

    assert asyncio.run(controller.force_charge(duration_minutes=30, power_w=6000))

    assert hass.services.calls == [
        ("number", "set_value", {"entity_id": "number.neovolt_1_dispatch_power", "value": 3.0}),
        ("number", "set_value", {"entity_id": "number.neovolt_1_dispatch_duration", "value": 30}),
        ("number", "set_value", {"entity_id": "number.neovolt_1_dispatch_charge_target_soc", "value": 100}),
        ("select", "select_option", {"entity_id": "select.neovolt_1_dispatch_mode", "option": "Force Charge"}),
        ("number", "set_value", {"entity_id": "number.neovolt_2_dispatch_power", "value": 3.0}),
        ("number", "set_value", {"entity_id": "number.neovolt_2_dispatch_duration", "value": 30}),
        ("number", "set_value", {"entity_id": "number.neovolt_2_dispatch_charge_target_soc", "value": 100}),
        ("select", "select_option", {"entity_id": "select.neovolt_2_dispatch_mode", "option": "Force Charge"}),
    ]


def test_fleet_force_charge_restores_per_inverter_baseline_modes():
    entry_1_states = _combined_states_for(
        1,
        battery_power="0",
        battery_soc="50",
        battery_capacity="20.1",
    ) + _control_states_for(1, mode="Idle (No Dispatch)")
    entry_2_states = _combined_states_for(
        2,
        battery_power="0",
        battery_soc="50",
        battery_capacity="30.2",
    ) + _control_states_for(2, mode="Normal")
    hass = _FakeHass(
        entry_1_states + entry_2_states,
        {
            "neovolt-1": [state.entity_id for state in entry_1_states],
            "neovolt-2": [state.entity_id for state in entry_2_states],
        },
    )
    controller = NeovoltFleetBatteryController(
        hass,
        ["neovolt-1", "neovolt-2"],
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
    )
    assert asyncio.run(controller.connect())

    assert asyncio.run(controller.force_charge(duration_minutes=30, power_w=6000))
    hass.states._states["select.neovolt_1_dispatch_mode"].state = "Force Charge"
    hass.states._states["select.neovolt_2_dispatch_mode"].state = "Force Charge"
    assert asyncio.run(controller.restore_normal())

    assert hass.services.calls[-2:] == [
        ("select", "select_option", {"entity_id": "select.neovolt_1_dispatch_mode", "option": "Idle (No Dispatch)"}),
        ("select", "select_option", {"entity_id": "select.neovolt_2_dispatch_mode", "option": "Normal"}),
    ]


def test_fleet_restore_normal_keeps_existing_stable_modes_without_saved_baseline():
    entry_1_states = _combined_states_for(
        1,
        battery_power="0",
        battery_soc="50",
        battery_capacity="20.1",
    ) + _control_states_for(1, mode="Idle (No Dispatch)")
    entry_2_states = _combined_states_for(
        2,
        battery_power="0",
        battery_soc="50",
        battery_capacity="30.2",
    ) + _control_states_for(2, mode="Normal")
    hass = _FakeHass(
        entry_1_states + entry_2_states,
        {
            "neovolt-1": [state.entity_id for state in entry_1_states],
            "neovolt-2": [state.entity_id for state in entry_2_states],
        },
    )
    controller = NeovoltFleetBatteryController(
        hass,
        ["neovolt-1", "neovolt-2"],
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
    )
    assert asyncio.run(controller.connect())

    assert asyncio.run(controller.restore_normal())

    assert hass.services.calls == []


def test_fleet_surplus_balancer_charges_anti_fighting_stack_from_export():
    entry_1_states = _combined_states_for(
        1,
        battery_power="-4600",
        battery_soc="62",
        battery_capacity="20.1",
        house_load="900",
        pv_power="6000",
        grid_power="-800",
    ) + _control_states_for(1, mode="Normal")
    entry_2_states = _combined_states_for(
        2,
        battery_power="0",
        battery_soc="23",
        battery_capacity="30.2",
        house_load="0",
        pv_power="0",
        grid_power="0",
    ) + _control_states_for(2, mode="No Battery Charge")
    hass = _FakeHass(
        entry_1_states + entry_2_states,
        {
            "neovolt-1": [state.entity_id for state in entry_1_states],
            "neovolt-2": [state.entity_id for state in entry_2_states],
        },
    )
    controller = NeovoltFleetBatteryController(
        hass,
        ["neovolt-1", "neovolt-2"],
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
    )
    assert asyncio.run(controller.connect())

    status = controller.get_status()
    balancer = asyncio.run(controller.balance_solar_surplus(status))

    assert balancer["status"] == "charging"
    assert balancer["active_index"] == 1
    assert balancer["lowest_soc_index"] == 1
    assert balancer["soc_delta_percent"] == 39.0
    assert balancer["base_mode"] == "No Battery Charge"
    assert balancer["last_power_w"] == 800.0
    assert hass.services.calls == [
        ("number", "set_value", {"entity_id": "number.neovolt_2_dispatch_power", "value": 0.8}),
        ("number", "set_value", {"entity_id": "number.neovolt_2_dispatch_duration", "value": 2}),
        ("number", "set_value", {"entity_id": "number.neovolt_2_dispatch_charge_target_soc", "value": 100}),
        ("select", "select_option", {"entity_id": "select.neovolt_2_dispatch_mode", "option": "Force Charge"}),
    ]


def test_fleet_surplus_balancer_blocks_start_when_other_stack_is_discharging():
    entry_1_states = _combined_states_for(
        1,
        battery_power="600",
        battery_soc="62",
        battery_capacity="20.1",
        house_load="900",
        pv_power="6000",
        grid_power="-800",
    ) + _control_states_for(1, mode="Normal")
    entry_2_states = _combined_states_for(
        2,
        battery_power="0",
        battery_soc="23",
        battery_capacity="30.2",
        house_load="0",
        pv_power="0",
        grid_power="0",
    ) + _control_states_for(2, mode="No Battery Charge")
    hass = _FakeHass(
        entry_1_states + entry_2_states,
        {
            "neovolt-1": [state.entity_id for state in entry_1_states],
            "neovolt-2": [state.entity_id for state in entry_2_states],
        },
    )
    controller = NeovoltFleetBatteryController(
        hass,
        ["neovolt-1", "neovolt-2"],
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
    )
    assert asyncio.run(controller.connect())

    balancer = asyncio.run(controller.balance_solar_surplus(controller.get_status()))

    assert balancer["status"] == "blocked_source_discharging"
    assert hass.services.calls == []


def test_fleet_surplus_balancer_does_not_charge_higher_soc_parked_stack():
    entry_1_states = _combined_states_for(
        1,
        battery_power="-4600",
        battery_soc="23",
        battery_capacity="20.1",
        house_load="900",
        pv_power="6000",
        grid_power="-800",
    ) + _control_states_for(1, mode="Normal")
    entry_2_states = _combined_states_for(
        2,
        battery_power="0",
        battery_soc="62",
        battery_capacity="30.2",
        house_load="0",
        pv_power="0",
        grid_power="0",
    ) + _control_states_for(2, mode="No Battery Charge")
    hass = _FakeHass(
        entry_1_states + entry_2_states,
        {
            "neovolt-1": [state.entity_id for state in entry_1_states],
            "neovolt-2": [state.entity_id for state in entry_2_states],
        },
    )
    controller = NeovoltFleetBatteryController(
        hass,
        ["neovolt-1", "neovolt-2"],
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
    )
    assert asyncio.run(controller.connect())

    balancer = asyncio.run(controller.balance_solar_surplus(controller.get_status()))

    assert balancer["status"] == "balancing_low_stack"
    assert balancer["lowest_soc_index"] == 0
    assert balancer["highest_soc_index"] == 1
    assert balancer["soc_delta_percent"] == 39.0
    assert hass.services.calls == []


def test_fleet_surplus_balancer_disabled_mode_reports_without_writes():
    entry_1_states = _combined_states_for(
        1,
        battery_power="-4600",
        battery_soc="62",
        battery_capacity="20.1",
        house_load="900",
        pv_power="6000",
        grid_power="-800",
    ) + _control_states_for(1, mode="Normal")
    entry_2_states = _combined_states_for(
        2,
        battery_power="0",
        battery_soc="23",
        battery_capacity="30.2",
        house_load="0",
        pv_power="0",
        grid_power="0",
    ) + _control_states_for(2, mode="No Battery Charge")
    hass = _FakeHass(
        entry_1_states + entry_2_states,
        {
            "neovolt-1": [state.entity_id for state in entry_1_states],
            "neovolt-2": [state.entity_id for state in entry_2_states],
        },
    )
    controller = NeovoltFleetBatteryController(
        hass,
        ["neovolt-1", "neovolt-2"],
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
        surplus_balancer_mode="disabled",
    )
    assert asyncio.run(controller.connect())

    balancer = asyncio.run(controller.balance_solar_surplus(controller.get_status()))

    assert balancer["status"] == "disabled"
    assert balancer["enabled"] is False
    assert balancer["soc_delta_percent"] == 39.0
    assert hass.services.calls == []


def test_fleet_surplus_balancer_restores_base_mode_and_dispatch_settings_on_import():
    entry_1_states = _combined_states_for(
        1,
        battery_power="-4600",
        battery_soc="62",
        battery_capacity="20.1",
        house_load="900",
        pv_power="6000",
        grid_power="-800",
    ) + _control_states_for(1, mode="Normal")
    entry_2_states = _combined_states_for(
        2,
        battery_power="0",
        battery_soc="23",
        battery_capacity="30.2",
        house_load="0",
        pv_power="0",
        grid_power="0",
    ) + _control_states_for(2, mode="No Battery Charge")
    hass = _FakeHass(
        entry_1_states + entry_2_states,
        {
            "neovolt-1": [state.entity_id for state in entry_1_states],
            "neovolt-2": [state.entity_id for state in entry_2_states],
        },
    )
    controller = NeovoltFleetBatteryController(
        hass,
        ["neovolt-1", "neovolt-2"],
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
    )
    assert asyncio.run(controller.connect())
    asyncio.run(controller.balance_solar_surplus(controller.get_status()))

    hass.services.calls.clear()
    hass.states._states["select.neovolt_2_dispatch_mode"].state = "Force Charge"
    hass.states._states["sensor.neovolt_2_combined_battery_power"].state = "-800"
    hass.states._states["sensor.neovolt_1_grid_total_active_power"].state = "300"
    hass.states._states["sensor.neovolt_2_grid_total_active_power"].state = "0"

    balancer = asyncio.run(controller.balance_solar_surplus(controller.get_status()))

    assert balancer["status"] == "stopped_importing"
    assert balancer["active_index"] is None
    assert hass.services.calls == [
        ("select", "select_option", {"entity_id": "select.neovolt_2_dispatch_mode", "option": "No Battery Charge"}),
        ("number", "set_value", {"entity_id": "number.neovolt_2_dispatch_power", "value": 3.0}),
        ("number", "set_value", {"entity_id": "number.neovolt_2_dispatch_duration", "value": 120}),
        ("number", "set_value", {"entity_id": "number.neovolt_2_dispatch_charge_target_soc", "value": 100}),
    ]


def test_fleet_surplus_balancer_stops_when_other_stack_starts_discharging():
    entry_1_states = _combined_states_for(
        1,
        battery_power="-4600",
        battery_soc="62",
        battery_capacity="20.1",
        house_load="900",
        pv_power="6000",
        grid_power="-800",
    ) + _control_states_for(1, mode="Normal")
    entry_2_states = _combined_states_for(
        2,
        battery_power="0",
        battery_soc="23",
        battery_capacity="30.2",
        house_load="0",
        pv_power="0",
        grid_power="0",
    ) + _control_states_for(2, mode="No Battery Charge")
    hass = _FakeHass(
        entry_1_states + entry_2_states,
        {
            "neovolt-1": [state.entity_id for state in entry_1_states],
            "neovolt-2": [state.entity_id for state in entry_2_states],
        },
    )
    controller = NeovoltFleetBatteryController(
        hass,
        ["neovolt-1", "neovolt-2"],
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
    )
    assert asyncio.run(controller.connect())
    asyncio.run(controller.balance_solar_surplus(controller.get_status()))

    hass.services.calls.clear()
    hass.states._states["select.neovolt_2_dispatch_mode"].state = "Force Charge"
    hass.states._states["sensor.neovolt_2_combined_battery_power"].state = "-800"
    hass.states._states["sensor.neovolt_1_combined_battery_power"].state = "600"
    hass.states._states["sensor.neovolt_1_grid_total_active_power"].state = "-500"

    balancer = asyncio.run(controller.balance_solar_surplus(controller.get_status()))

    assert balancer["status"] == "stopped_source_discharging"
    assert balancer["active_index"] is None
    assert hass.services.calls[0] == (
        "select",
        "select_option",
        {"entity_id": "select.neovolt_2_dispatch_mode", "option": "No Battery Charge"},
    )


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
    states = [
        state
        for state in (_combined_states() + _control_states())
        if state.entity_id != "select.neovolt_1_dispatch_mode"
    ]
    states.insert(
        0,
        _FakeState(
            "select.neovolt_1_dispatch_mode",
            "Force Charge",
            ["Normal", "Force Charge", "Force Discharge", "Dynamic Export", "Dynamic Import"],
        ),
    )
    hass = _hass_for(states)
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
