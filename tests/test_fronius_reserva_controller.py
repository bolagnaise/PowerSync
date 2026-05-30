"""Regression tests for Fronius GEN24 storage entity bridge controls."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
import sys
import types


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

from power_sync.inverters.fronius_reserva import FroniusReservaBatteryController  # noqa: E402


class _FakeState:
    def __init__(self, entity_id: str, state: str = "0"):
        self.entity_id = entity_id
        self.state = state
        self.attributes = {}


class _FakeStates:
    def __init__(self, states: list[_FakeState]):
        self._states = {state.entity_id: state for state in states}

    def get(self, entity_id: str | None):
        return self._states.get(entity_id or "")

    def set(self, entity_id: str, state: str) -> None:
        self._states[entity_id] = _FakeState(entity_id, state)

    def async_all(self):
        return list(self._states.values())


class _FakeServices:
    def __init__(self, states: _FakeStates):
        self._states = states
        self.calls: list[tuple[str, str, dict]] = []

    async def async_call(self, domain: str, service: str, data: dict, blocking: bool = True):
        self.calls.append((domain, service, dict(data)))
        entity_id = data.get("entity_id")
        if domain == "select" and service == "select_option":
            self._states.set(entity_id, str(data["option"]))
        elif domain == "number" and service == "set_value":
            self._states.set(entity_id, str(data["value"]))


class _FakeRegistry:
    def __init__(self, entries: dict[str, list[str]]):
        self._entries = entries

    def entries_for(self, entry_id: str):
        return [
            SimpleNamespace(entity_id=entity_id)
            for entity_id in self._entries.get(entry_id, [])
        ]


class _FakeHass:
    def __init__(self, states: list[_FakeState]):
        self.states = _FakeStates(states)
        self.services = _FakeServices(self.states)
        self.entity_registry = _FakeRegistry(
            {"fronius-entry": [state.entity_id for state in states]}
        )


def _reserva_states() -> list[_FakeState]:
    return [
        _FakeState("sensor.reserva_state_of_charge_2", "64"),
        _FakeState("sensor.storage_charging_power", "1200"),
        _FakeState("sensor.storage_discharging_power", "0"),
        _FakeState("sensor.meter_1_power", "500"),
        _FakeState("sensor.pv_power_2", "3200"),
        _FakeState("sensor.load_2", "2500"),
        _FakeState("sensor.reserva_soc_minimum", "20"),
        _FakeState("select.reserva_battery_api_mode", "Auto"),
        _FakeState("select.reserva_storage_control_mode_2", "Auto"),
        _FakeState("number.reserva_grid_charge_power_2", "0"),
        _FakeState("number.reserva_grid_discharge_power_2", "0"),
        _FakeState("number.reserva_pv_charge_limit_2", "0"),
        _FakeState("number.reserva_discharge_limit_2", "0"),
        _FakeState("number.reserva_soc_minimum", "20"),
    ]


def _callifo_byd_states() -> list[_FakeState]:
    return [
        _FakeState("sensor.fronius_battery_storage_state_of_charge", "71"),
        _FakeState("sensor.fronius_battery_storage_storage_charging_power", "0"),
        _FakeState("sensor.fronius_battery_storage_storage_discharging_power", "1800"),
        _FakeState("sensor.fronius_meter_200_power", "-400"),
        _FakeState("sensor.fronius_inverter_pv_power", "4600"),
        _FakeState("sensor.fronius_inverter_load", "3200"),
        _FakeState("sensor.fronius_battery_storage_cell_temperature", "24.5"),
        _FakeState("sensor.fronius_battery_storage_capacity", "10240"),
        _FakeState("sensor.fronius_battery_storage_maximum_charge_rate", "6200"),
        _FakeState("sensor.fronius_battery_storage_maximum_discharge_rate", "6400"),
        _FakeState("sensor.fronius_battery_storage_soc_minimum", "15"),
        _FakeState("sensor.fronius_battery_storage_core_storage_control_mode", "Auto"),
        _FakeState("select.fronius_battery_storage_battery_api_mode", "Auto"),
        _FakeState("select.fronius_battery_storage_storage_control_mode", "Auto"),
        _FakeState("number.fronius_battery_storage_grid_charge_power", "0"),
        _FakeState("number.fronius_battery_storage_grid_discharge_power", "0"),
        _FakeState("number.fronius_battery_storage_pv_charge_limit", "0"),
        _FakeState("number.fronius_battery_storage_discharge_limit", "0"),
        _FakeState("number.fronius_battery_storage_soc_minimum", "15"),
    ]


def _controller(hass: _FakeHass) -> FroniusReservaBatteryController:
    return FroniusReservaBatteryController(
        hass,
        "fronius-entry",
        battery_capacity_kwh=9.6,
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
    )


def test_connect_discovers_reserva_entities_and_reads_status():
    hass = _FakeHass(_reserva_states())
    controller = _controller(hass)

    assert asyncio.run(controller.connect())
    assert controller._entity_map["battery_level"] == "sensor.reserva_state_of_charge_2"
    assert controller._entity_map["storage_control_mode"] == "select.reserva_storage_control_mode_2"
    assert controller._entity_map["grid_charge_power"] == "number.reserva_grid_charge_power_2"

    status = controller.get_status()
    assert status["battery_level"] == 64.0
    assert status["battery_power"] == -1.2
    assert status["grid_power"] == 0.5
    assert status["solar_power"] == 3.2
    assert status["backup_reserve"] == 20.0
    assert status["battery_max_charge_power_w"] == 5000.0


def test_connect_discovers_callifo_byd_entities_and_reads_status():
    hass = _FakeHass(_callifo_byd_states())
    controller = _controller(hass)

    assert asyncio.run(controller.connect())
    assert controller._entity_map["battery_level"] == "sensor.fronius_battery_storage_state_of_charge"
    assert controller._entity_map["storage_control_mode"] == "select.fronius_battery_storage_storage_control_mode"
    assert controller._entity_map["grid_charge_power"] == "number.fronius_battery_storage_grid_charge_power"
    assert controller._entity_map["backup_reserve"] == "number.fronius_battery_storage_soc_minimum"

    status = controller.get_status()
    assert status["battery_level"] == 71.0
    assert status["battery_power"] == 1.8
    assert status["grid_power"] == -0.4
    assert status["solar_power"] == 4.6
    assert status["load_power"] == 3.2
    assert status["battery_temperature"] == 24.5
    assert status["battery_capacity_kwh"] == 10.24
    assert status["battery_max_charge_power_w"] == 6200.0
    assert status["battery_max_discharge_power_w"] == 6400.0
    assert status["backup_reserve"] == 15.0


def test_status_uses_configured_power_fallback_when_callifo_limits_missing():
    states = [
        state
        for state in _callifo_byd_states()
        if state.entity_id
        not in (
            "sensor.fronius_battery_storage_maximum_charge_rate",
            "sensor.fronius_battery_storage_maximum_discharge_rate",
        )
    ]
    hass = _FakeHass(states)
    controller = _controller(hass)

    assert asyncio.run(controller.connect())
    status = controller.get_status()

    assert status["battery_max_charge_power_w"] == 5000.0
    assert status["battery_max_discharge_power_w"] == 5000.0


def test_connect_reports_generic_storage_missing_entity_hint():
    states = [
        state
        for state in _callifo_byd_states()
        if state.entity_id != "select.fronius_battery_storage_storage_control_mode"
    ]
    hass = _FakeHass(states)
    controller = _controller(hass)

    try:
        asyncio.run(controller.connect())
    except ValueError as exc:
        assert str(exc) == "fronius_reserva_missing_entities:select.storage_control_mode"
    else:
        raise AssertionError("Expected missing entity validation error")


def test_force_charge_sets_manual_grid_charge_mode_then_power():
    hass = _FakeHass(_reserva_states())
    controller = _controller(hass)

    assert asyncio.run(controller.force_charge(duration_minutes=30, power_w=4200))
    assert hass.services.calls == [
        (
            "select",
            "select_option",
            {"entity_id": "select.reserva_battery_api_mode", "option": "Manual"},
        ),
        (
            "select",
            "select_option",
            {"entity_id": "select.reserva_storage_control_mode_2", "option": "Charge from Grid"},
        ),
        (
            "number",
            "set_value",
            {"entity_id": "number.reserva_grid_charge_power_2", "value": 4200},
        ),
    ]


def test_force_charge_attempts_power_write_when_entity_stays_unavailable(monkeypatch):
    import power_sync.inverters.fronius_reserva as fronius_reserva

    monkeypatch.setattr(fronius_reserva, "_OPTION_WAIT_SECONDS", -1)

    states = _reserva_states()
    for state in states:
        if state.entity_id == "number.reserva_grid_charge_power_2":
            state.state = "unavailable"
    hass = _FakeHass(states)
    controller = _controller(hass)

    assert asyncio.run(controller.force_charge(duration_minutes=30, power_w=4200))
    assert hass.services.calls[-1] == (
        "number",
        "set_value",
        {"entity_id": "number.reserva_grid_charge_power_2", "value": 4200},
    )


def test_force_discharge_sets_manual_export_mode_then_power():
    hass = _FakeHass(_reserva_states())
    controller = _controller(hass)

    assert asyncio.run(controller.force_discharge(duration_minutes=30, power_w=4300))
    assert hass.services.calls == [
        (
            "select",
            "select_option",
            {"entity_id": "select.reserva_battery_api_mode", "option": "Manual"},
        ),
        (
            "select",
            "select_option",
            {"entity_id": "select.reserva_storage_control_mode_2", "option": "Discharge to Grid"},
        ),
        (
            "number",
            "set_value",
            {"entity_id": "number.reserva_grid_discharge_power_2", "value": 4300},
        ),
    ]


def test_idle_zeroes_pv_charge_and_discharge_limits():
    hass = _FakeHass(_reserva_states())
    controller = _controller(hass)

    assert asyncio.run(controller.set_idle())
    assert hass.services.calls == [
        (
            "select",
            "select_option",
            {"entity_id": "select.reserva_battery_api_mode", "option": "Manual"},
        ),
        (
            "select",
            "select_option",
            {
                "entity_id": "select.reserva_storage_control_mode_2",
                "option": "PV Charge and Discharge Limit",
            },
        ),
        (
            "number",
            "set_value",
            {"entity_id": "number.reserva_pv_charge_limit_2", "value": 0},
        ),
        (
            "number",
            "set_value",
            {"entity_id": "number.reserva_discharge_limit_2", "value": 0},
        ),
    ]


def test_restore_normal_sets_auto_storage_control():
    hass = _FakeHass(_reserva_states())
    controller = _controller(hass)

    assert asyncio.run(controller.restore_normal())
    assert hass.services.calls == [
        (
            "select",
            "select_option",
            {"entity_id": "select.reserva_storage_control_mode_2", "option": "Auto"},
        )
    ]


def test_force_charge_uses_callifo_byd_control_entities():
    hass = _FakeHass(_callifo_byd_states())
    controller = _controller(hass)

    assert asyncio.run(controller.force_charge(duration_minutes=30, power_w=4200))
    assert hass.services.calls == [
        (
            "select",
            "select_option",
            {"entity_id": "select.fronius_battery_storage_battery_api_mode", "option": "Manual"},
        ),
        (
            "select",
            "select_option",
            {"entity_id": "select.fronius_battery_storage_storage_control_mode", "option": "Charge from Grid"},
        ),
        (
            "number",
            "set_value",
            {"entity_id": "number.fronius_battery_storage_grid_charge_power", "value": 4200},
        ),
    ]
