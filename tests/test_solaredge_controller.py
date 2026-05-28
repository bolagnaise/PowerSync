"""Tests for SolarEdge active-power curtailment control."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"

_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules.setdefault("power_sync", _ps)

_inverters = types.ModuleType("power_sync.inverters")
_inverters.__path__ = [str(ROOT / "inverters")]
sys.modules.setdefault("power_sync.inverters", _inverters)

from power_sync.inverters.solaredge import SolarEdgeController, SolarEdgeEnergyController


def test_solaredge_load_following_maps_watts_to_percent():
    controller = SolarEdgeController(
        host="",
        rated_power_w=5000,
    )
    writes: list[int] = []

    async def fake_set(percent: int) -> bool:
        writes.append(percent)
        return True

    controller._set_active_power_limit = fake_set

    assert asyncio.run(controller.curtail(home_load_w=1251))
    assert writes == [26]


def test_solaredge_zero_curtail_and_restore_write_percent_limits():
    controller = SolarEdgeController(
        host="",
        rated_power_w=5000,
    )
    writes: list[int] = []

    async def fake_set(percent: int) -> bool:
        writes.append(percent)
        return True

    controller._set_active_power_limit = fake_set

    assert asyncio.run(controller.curtail())
    assert asyncio.run(controller.restore())
    assert writes == [0, 100]


def test_solaredge_entity_fallback_prefers_configured_prefix():
    class State:
        def __init__(self, state: str) -> None:
            self.state = state

    class States:
        def __init__(self) -> None:
            self._states = {
                "number.custom_active_power_limit": State("100"),
                "number.solaredge_active_power_limit": State("100"),
            }

        def get(self, entity_id: str):
            return self._states.get(entity_id)

    class Services:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict]] = []

        async def async_call(self, domain: str, service: str, data: dict, blocking: bool = False):
            self.calls.append((domain, service, data))

    class Hass:
        def __init__(self) -> None:
            self.states = States()
            self.services = Services()

    hass = Hass()
    controller = SolarEdgeController(
        host="",
        entity_prefix="custom",
        hass=hass,
    )

    assert asyncio.run(controller.connect())
    assert asyncio.run(controller.curtail(home_load_w=1000))
    assert hass.services.calls == [
        (
            "number",
            "set_value",
            {"entity_id": "number.custom_active_power_limit", "value": 20},
        )
    ]


def test_solaredge_energy_bridge_maps_modbus_multi_battery_entities():
    class State:
        def __init__(self, entity_id: str, state: str, attributes: dict | None = None) -> None:
            self.entity_id = entity_id
            self.state = state
            self.attributes = attributes or {}

    class States:
        def __init__(self) -> None:
            self._states = {
                "sensor.solaredge_i1_b1_state_of_energy": State(
                    "sensor.solaredge_i1_b1_state_of_energy",
                    "64",
                    {"unit_of_measurement": "%"},
                ),
                "sensor.solaredge_i1_b1_dc_power": State(
                    "sensor.solaredge_i1_b1_dc_power",
                    "-1800",
                    {"unit_of_measurement": "W"},
                ),
                "sensor.solaredge_m1_ac_power": State(
                    "sensor.solaredge_m1_ac_power",
                    "-500",
                    {"unit_of_measurement": "W"},
                ),
                "sensor.solaredge_i1_ac_power": State(
                    "sensor.solaredge_i1_ac_power",
                    "3200",
                    {"unit_of_measurement": "W"},
                ),
            }

        def get(self, entity_id: str | None):
            return self._states.get(entity_id or "")

        def async_all(self, domain: str | None = None):
            if domain is None:
                return list(self._states.values())
            prefix = f"{domain}."
            return [
                state
                for state in self._states.values()
                if state.entity_id.startswith(prefix)
            ]

    class Hass:
        def __init__(self) -> None:
            self.states = States()

    controller = SolarEdgeEnergyController(Hass(), entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    status = controller.get_status()

    assert controller._entity_map["battery_level"] == (
        "sensor.solaredge_i1_b1_state_of_energy"
    )
    assert status["battery_level"] == 64.0
    assert status["battery_power"] == 1.8
    assert status["grid_power"] == 0.5
    assert status["solar_power"] == 3.2
    assert status["load_power"] == 5.5


def test_solaredge_energy_bridge_uses_import_export_fallbacks():
    class State:
        def __init__(self, entity_id: str, state: str, attributes: dict | None = None) -> None:
            self.entity_id = entity_id
            self.state = state
            self.attributes = attributes or {}

    class States:
        def __init__(self) -> None:
            self._states = {
                "sensor.solaredge_b1_state_of_energy": State(
                    "sensor.solaredge_b1_state_of_energy",
                    "41",
                    {"unit_of_measurement": "%"},
                ),
                "sensor.solaredge_b1_battery_charge_power": State(
                    "sensor.solaredge_b1_battery_charge_power",
                    "700",
                    {"unit_of_measurement": "W"},
                ),
                "sensor.solaredge_b1_battery_discharge_power": State(
                    "sensor.solaredge_b1_battery_discharge_power",
                    "200",
                    {"unit_of_measurement": "W"},
                ),
                "sensor.solaredge_grid_import_power": State(
                    "sensor.solaredge_grid_import_power",
                    "1.4",
                    {"unit_of_measurement": "kW"},
                ),
                "sensor.solaredge_grid_export_power": State(
                    "sensor.solaredge_grid_export_power",
                    "0.3",
                    {"unit_of_measurement": "kW"},
                ),
                "sensor.solaredge_pv_power": State(
                    "sensor.solaredge_pv_power",
                    "2.1",
                    {"unit_of_measurement": "kW"},
                ),
            }

        def get(self, entity_id: str | None):
            return self._states.get(entity_id or "")

        def async_all(self, domain: str | None = None):
            if domain is None:
                return list(self._states.values())
            prefix = f"{domain}."
            return [
                state
                for state in self._states.values()
                if state.entity_id.startswith(prefix)
            ]

    class Hass:
        def __init__(self) -> None:
            self.states = States()

    controller = SolarEdgeEnergyController(Hass(), entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    status = controller.get_status()

    assert status["battery_power"] == pytest.approx(-0.5)
    assert status["grid_power"] == pytest.approx(1.1)
    assert status["load_power"] == pytest.approx(2.7)


class _SEState:
    def __init__(self, entity_id: str, state: str, attributes: dict | None = None) -> None:
        self.entity_id = entity_id
        self.state = state
        self.attributes = attributes or {}


class _SEStates:
    def __init__(self, states: dict[str, _SEState]) -> None:
        self._states = states

    def get(self, entity_id: str | None):
        return self._states.get(entity_id or "")

    def async_all(self, domain: str | None = None):
        if domain is None:
            return list(self._states.values())
        prefix = f"{domain}."
        return [
            state
            for state in self._states.values()
            if state.entity_id.startswith(prefix)
        ]


def test_solaredge_m1_kwh_counters_are_reported_as_lifetime_totals():
    class Hass:
        def __init__(self) -> None:
            self.states = _SEStates(
                {
                    "sensor.solaredge_b1_state_of_energy": _SEState(
                        "sensor.solaredge_b1_state_of_energy",
                        "65",
                        {"unit_of_measurement": "%"},
                    ),
                    "sensor.solaredge_m1_imported_kwh": _SEState(
                        "sensor.solaredge_m1_imported_kwh",
                        "12345.6",
                        {"unit_of_measurement": "kWh"},
                    ),
                    "sensor.solaredge_m1_exported_kwh": _SEState(
                        "sensor.solaredge_m1_exported_kwh",
                        "6543.2",
                        {"unit_of_measurement": "kWh"},
                    ),
                }
            )

    controller = SolarEdgeEnergyController(Hass(), entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    status = controller.get_status()

    assert status["daily_grid_import_kwh"] is None
    assert status["daily_grid_export_kwh"] is None
    assert status["total_grid_import_kwh"] == pytest.approx(12345.6)
    assert status["total_grid_export_kwh"] == pytest.approx(6543.2)


class _SEServices:
    def __init__(self, states: _SEStates) -> None:
        self._states = states
        self.calls: list[tuple[str, str, dict]] = []

    async def async_call(self, domain: str, service: str, data: dict, blocking: bool = False):
        self.calls.append((domain, service, data))
        entity_id = data.get("entity_id")
        state = self._states.get(entity_id)
        if not state:
            return
        if domain == "number" and service == "set_value":
            state.state = str(data["value"])
        elif domain == "select" and service == "select_option":
            state.state = str(data["option"])
        elif domain == "switch" and service in {"turn_on", "turn_off"}:
            state.state = "on" if service == "turn_on" else "off"


class _SEHass:
    def __init__(self, include_control: bool = True) -> None:
        states = {
            "sensor.solaredge_b1_state_of_energy": _SEState(
                "sensor.solaredge_b1_state_of_energy",
                "55",
                {"unit_of_measurement": "%"},
            ),
            "sensor.solaredge_b1_dc_power": _SEState(
                "sensor.solaredge_b1_dc_power",
                "0",
                {"unit_of_measurement": "W"},
            ),
        }
        if include_control:
            states.update(
                {
                    "select.solaredge_storage_control_mode": _SEState(
                        "select.solaredge_storage_control_mode",
                        "Maximize Self Consumption",
                        {"options": ["Maximize Self Consumption", "Remote Control"]},
                    ),
                    "select.solaredge_storage_command_mode": _SEState(
                        "select.solaredge_storage_command_mode",
                        "Stop",
                        {"options": ["Stop", "Charge", "Discharge"]},
                    ),
                    "number.solaredge_storage_charge_limit": _SEState(
                        "number.solaredge_storage_charge_limit",
                        "0",
                        {"unit_of_measurement": "W", "max": 6000},
                    ),
                    "number.solaredge_storage_discharge_limit": _SEState(
                        "number.solaredge_storage_discharge_limit",
                        "0",
                        {"unit_of_measurement": "W", "max": 6000},
                    ),
                    "number.solaredge_storage_command_timeout": _SEState(
                        "number.solaredge_storage_command_timeout",
                        "0",
                        {"unit_of_measurement": "s"},
                    ),
                    "number.solaredge_backup_reserve": _SEState(
                        "number.solaredge_backup_reserve",
                        "15",
                        {"unit_of_measurement": "%"},
                    ),
                    "switch.solaredge_allow_grid_charge": _SEState(
                        "switch.solaredge_allow_grid_charge",
                        "off",
                    ),
                }
            )
        self.states = _SEStates(states)
        self.services = _SEServices(self.states)


def test_solaredge_energy_bridge_discovers_control_entities():
    hass = _SEHass()
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    assert controller.control_available()
    assert controller.missing_control_entities() == []
    assert controller._control_entity_map["storage_control_mode"] == (
        "select.solaredge_storage_control_mode"
    )
    assert controller._control_entity_map["backup_reserve"] == (
        "number.solaredge_backup_reserve"
    )


def test_solaredge_energy_bridge_discovers_remote_command_mode_alias():
    hass = _SEHass()
    state = hass.states._states.pop("select.solaredge_storage_command_mode")
    state.entity_id = "select.solaredge_remote_command_mode"
    hass.states._states[state.entity_id] = state
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    assert controller.control_available()
    assert controller._control_entity_map["storage_command_mode"] == (
        "select.solaredge_remote_command_mode"
    )

    assert asyncio.run(controller.force_charge(duration_minutes=30, power_w=4200))
    assert ("select", "select_option", {
        "entity_id": "select.solaredge_remote_command_mode",
        "option": "Charge",
    }) in hass.services.calls


def test_solaredge_force_charge_writes_remote_charge_entities_and_restores():
    hass = _SEHass()
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    assert asyncio.run(controller.force_charge(duration_minutes=30, power_w=4200))

    assert ("select", "select_option", {
        "entity_id": "select.solaredge_storage_control_mode",
        "option": "Remote Control",
    }) in hass.services.calls
    assert ("number", "set_value", {
        "entity_id": "number.solaredge_storage_command_timeout",
        "value": 1800.0,
    }) in hass.services.calls
    assert ("number", "set_value", {
        "entity_id": "number.solaredge_storage_charge_limit",
        "value": 4200.0,
    }) in hass.services.calls
    assert ("select", "select_option", {
        "entity_id": "select.solaredge_storage_command_mode",
        "option": "Charge",
    }) in hass.services.calls

    assert asyncio.run(controller.restore_normal())
    assert hass.states.get("select.solaredge_storage_control_mode").state == (
        "Maximize Self Consumption"
    )
    assert hass.states.get("select.solaredge_storage_command_mode").state == "Stop"


def test_solaredge_force_discharge_writes_remote_discharge_entities():
    hass = _SEHass()
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    assert asyncio.run(controller.force_discharge(duration_minutes=15, power_w=3500))

    assert ("number", "set_value", {
        "entity_id": "number.solaredge_storage_charge_limit",
        "value": 0.0,
    }) in hass.services.calls
    assert ("number", "set_value", {
        "entity_id": "number.solaredge_storage_discharge_limit",
        "value": 3500.0,
    }) in hass.services.calls
    assert ("select", "select_option", {
        "entity_id": "select.solaredge_storage_command_mode",
        "option": "Discharge",
    }) in hass.services.calls


def test_solaredge_backup_reserve_and_hold_soc_use_writable_reserve():
    hass = _SEHass()
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    assert asyncio.run(controller.set_backup_reserve(22))
    assert asyncio.run(controller.get_backup_reserve()) == 22
    assert asyncio.run(controller.set_backup_mode())

    assert ("number", "set_value", {
        "entity_id": "number.solaredge_backup_reserve",
        "value": 55.0,
    }) in hass.services.calls


def test_solaredge_missing_control_entities_keeps_telemetry_but_rejects_dispatch():
    hass = _SEHass(include_control=False)
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    assert not controller.control_available()
    assert controller.missing_control_entities() == [
        "storage_control_mode",
        "storage_command_mode",
        "charge_power_limit",
        "discharge_power_limit",
    ]
    assert not asyncio.run(controller.force_charge(duration_minutes=30, power_w=4200))
    assert hass.services.calls == []
