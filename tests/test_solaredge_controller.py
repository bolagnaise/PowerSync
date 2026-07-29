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


def test_solaredge_energy_bridge_does_not_map_battery_dc_power_as_solar():
    class Hass:
        def __init__(self) -> None:
            self.states = _SEStates(
                {
                    "sensor.solaredge_b1_state_of_energy": _SEState(
                        "sensor.solaredge_b1_state_of_energy",
                        "8",
                        {"unit_of_measurement": "%"},
                    ),
                    "sensor.solaredge_b1_dc_power": _SEState(
                        "sensor.solaredge_b1_dc_power",
                        "-1800",
                        {"unit_of_measurement": "W"},
                    ),
                    "sensor.solaredge_m1_ac_power": _SEState(
                        "sensor.solaredge_m1_ac_power",
                        "-1800",
                        {"unit_of_measurement": "W"},
                    ),
                    "sensor.solaredge_load_power": _SEState(
                        "sensor.solaredge_load_power",
                        "1800",
                        {"unit_of_measurement": "W"},
                    ),
                }
            )

    controller = SolarEdgeEnergyController(Hass(), entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    status = controller.get_status()

    assert controller._entity_map["battery_power"] == "sensor.solaredge_b1_dc_power"
    assert "solar_power" not in controller._entity_map
    assert status["battery_power"] == pytest.approx(1.8)
    assert status["solar_power"] == 0.0
    assert status["load_power"] == pytest.approx(1.8)


def test_solaredge_energy_bridge_prefers_battery1_power_over_inverter_dc_power():
    class Hass:
        def __init__(self) -> None:
            self.states = _SEStates(
                {
                    "sensor.solaredge_battery1_state_of_energy": _SEState(
                        "sensor.solaredge_battery1_state_of_energy",
                        "81",
                        {"unit_of_measurement": "%"},
                    ),
                    "sensor.solaredge_dc_power": _SEState(
                        "sensor.solaredge_dc_power",
                        "1898.5",
                        {"unit_of_measurement": "W"},
                    ),
                    "sensor.solaredge_battery1_power": _SEState(
                        "sensor.solaredge_battery1_power",
                        "-4419",
                        {"unit_of_measurement": "W"},
                    ),
                    "sensor.solaredge_ac_power": _SEState(
                        "sensor.solaredge_ac_power",
                        "1930.8",
                        {"unit_of_measurement": "W"},
                    ),
                    "sensor.solaredge_m1_ac_power": _SEState(
                        "sensor.solaredge_m1_ac_power",
                        "0",
                        {"unit_of_measurement": "W"},
                    ),
                }
            )

    controller = SolarEdgeEnergyController(Hass(), entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    status = controller.get_status()

    assert controller._entity_map["battery_power"] == "sensor.solaredge_battery1_power"
    assert status["battery_power"] == pytest.approx(4.419)
    assert status["solar_power"] == pytest.approx(1.9308)
    assert status["load_power"] == pytest.approx(6.3498)


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


def test_solaredge_energy_bridge_maps_ev_charger_power():
    class Hass:
        def __init__(self) -> None:
            self.states = _SEStates(
                {
                    "sensor.solaredge_b1_state_of_energy": _SEState(
                        "sensor.solaredge_b1_state_of_energy",
                        "65",
                        {"unit_of_measurement": "%"},
                    ),
                    "sensor.ev_charger_power": _SEState(
                        "sensor.ev_charger_power",
                        "7.4",
                        {
                            "unit_of_measurement": "kW",
                            "friendly_name": "SolarEdge EV Charger EV Charger Power",
                        },
                    ),
                }
            )

    controller = SolarEdgeEnergyController(Hass(), entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    status = controller.get_status()

    assert controller._entity_map["ev_power"] == "sensor.ev_charger_power"
    assert status["ev_power"] == pytest.approx(7.4)


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


class _SEFailingServices(_SEServices):
    def __init__(
        self,
        states: _SEStates,
        failing_entity_id: str,
        reflect_before_error: bool = False,
    ) -> None:
        super().__init__(states)
        self._failing_entity_id = failing_entity_id
        self._reflect_before_error = reflect_before_error

    async def async_call(
        self,
        domain: str,
        service: str,
        data: dict,
        blocking: bool = False,
    ):
        if data.get("entity_id") != self._failing_entity_id:
            return await super().async_call(domain, service, data, blocking)
        self.calls.append((domain, service, data))
        if self._reflect_before_error:
            state = self._states.get(self._failing_entity_id)
            if state and domain == "select":
                state.state = str(data["option"])
        raise TimeoutError("service call timed out")


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
    assert controller.get_status()["telemetry_ready"] is True


def test_solaredge_control_discovery_prefers_valid_storage_entities():
    hass = _SEHass()
    hass.states._states.update(
        {
            "select.solaredge_i1_storage_control_mode": _SEState(
                "select.solaredge_i1_storage_control_mode",
                "Maximize Self Consumption",
                {"options": ["Maximize Self Consumption", "Remote Control"]},
            ),
            "select.solaredge_i1_limit_control_mode": _SEState(
                "select.solaredge_i1_limit_control_mode",
                "Export Control",
                {"options": ["Export Control", "Production Control"]},
            ),
            "number.solaredge_i1_storage_charge_limit": _SEState(
                "number.solaredge_i1_storage_charge_limit",
                "11400",
                {"unit_of_measurement": "W", "min": 0, "max": 11400},
            ),
            "number.solaredge_i1_ac_charge_limit": _SEState(
                "number.solaredge_i1_ac_charge_limit",
                "unavailable",
                {"unit_of_measurement": "W", "min": 0, "max": 0},
            ),
        }
    )
    hass.states._states.pop("select.solaredge_storage_control_mode")
    hass.states._states.pop("number.solaredge_storage_charge_limit")
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    assert controller._control_entity_map["storage_control_mode"] == (
        "select.solaredge_i1_storage_control_mode"
    )
    assert controller._control_entity_map["charge_power_limit"] == (
        "number.solaredge_i1_storage_charge_limit"
    )
    assert asyncio.run(controller.force_charge(duration_minutes=30, power_w=4200))
    called_entities = {call[2]["entity_id"] for call in hass.services.calls}
    assert "select.solaredge_i1_storage_control_mode" in called_entities
    assert "number.solaredge_i1_storage_charge_limit" in called_entities
    assert "select.solaredge_i1_limit_control_mode" not in called_entities
    assert "number.solaredge_i1_ac_charge_limit" not in called_entities


def test_solaredge_control_discovery_rejects_limit_control_mode():
    hass = _SEHass()
    hass.states._states.pop("select.solaredge_storage_control_mode")
    hass.states._states["select.solaredge_i1_limit_control_mode"] = _SEState(
        "select.solaredge_i1_limit_control_mode",
        "Export Control",
        {"options": ["Export Control", "Production Control"]},
    )
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    assert "storage_control_mode" not in controller._control_entity_map
    assert "storage_control_mode" in controller.missing_control_entities()


def test_solaredge_startup_readiness_rejects_unavailable_and_accepts_zeroes():
    hass = _SEHass()
    hass.states.get("sensor.solaredge_b1_dc_power").state = "unavailable"
    unavailable = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
    assert unavailable.get_status()["telemetry_ready"] is False

    zero_hass = _SEHass()
    zero_hass.states.get("sensor.solaredge_b1_state_of_energy").state = "0"
    zero_hass.states.get("sensor.solaredge_b1_dc_power").state = "0"
    zero = SolarEdgeEnergyController(zero_hass, entity_prefix="solaredge")
    assert zero.get_status()["telemetry_ready"] is True


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


@pytest.mark.parametrize(
    "policy_entity_id",
    [
        "select.solaredge_i1_ac_charge_policy",
        "select.solaredge_storage_ac_charge_policy",
    ],
)
def test_solaredge_force_charge_uses_grid_command_and_ac_policy_entities(
    policy_entity_id,
):
    hass = _SEHass()
    command_mode = hass.states.get("select.solaredge_storage_command_mode")
    command_mode.state = "Solar Power Only (Off)"
    command_mode.attributes["options"] = [
        "Solar Power Only (Off)",
        "Charge from Clipped Solar Power",
        "Charge from Solar Power",
        "Charge from Solar Power and Grid",
        "Discharge to Maximize Export",
        "Discharge to Minimize Import",
        "Maximize Self Consumption",
    ]
    hass.states._states.pop("switch.solaredge_allow_grid_charge")
    hass.states._states[policy_entity_id] = _SEState(
        policy_entity_id,
        "Disabled",
        {
            "options": [
                "Disabled",
                "Always Allowed",
                "Fixed Energy Limit",
                "Percent of Production",
            ]
        },
    )
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    assert asyncio.run(controller.force_charge(duration_minutes=30, power_w=4200))

    assert controller._control_entity_map["allow_grid_charge"] == policy_entity_id
    assert hass.states.get(policy_entity_id).state == "Always Allowed"
    assert hass.states.get("select.solaredge_storage_command_mode").state == (
        "Charge from Solar Power and Grid"
    )

    assert asyncio.run(controller.restore_normal())
    assert hass.states.get(policy_entity_id).state == "Disabled"
    assert hass.states.get("select.solaredge_storage_command_mode").state == (
        "Solar Power Only (Off)"
    )


def test_solaredge_force_charge_does_not_rollback_when_ac_policy_fails():
    hass = _SEHass()
    command_mode = hass.states.get("select.solaredge_storage_command_mode")
    command_mode.state = "Maximize Self Consumption"
    command_mode.attributes["options"] = [
        "Charge from Clipped Solar Power",
        "Charge from Solar Power and Grid",
        "Maximize Self Consumption",
    ]
    hass.states._states.pop("switch.solaredge_allow_grid_charge")
    policy_entity_id = "select.solaredge_i1_ac_charge_policy"
    hass.states._states[policy_entity_id] = _SEState(
        policy_entity_id,
        "Disabled",
        {"options": ["Disabled", "Always Allowed"]},
    )
    hass.services = _SEFailingServices(hass.states, policy_entity_id)
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
    controller.WRITE_CONFIRM_TIMEOUT_SECONDS = 0

    assert asyncio.run(controller.connect())
    assert asyncio.run(controller.force_charge(duration_minutes=30, power_w=4200))
    assert hass.states.get(policy_entity_id).state == "Disabled"
    assert hass.states.get("select.solaredge_storage_command_mode").state == (
        "Charge from Solar Power and Grid"
    )


def test_solaredge_force_charge_accepts_timeout_when_state_is_reflected():
    hass = _SEHass()
    command_entity_id = "select.solaredge_storage_command_mode"
    hass.services = _SEFailingServices(
        hass.states,
        command_entity_id,
        reflect_before_error=True,
    )
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    assert asyncio.run(controller.force_charge(duration_minutes=30, power_w=4200))
    assert hass.states.get(command_entity_id).state == "Charge"


def test_solaredge_stale_dispatch_restore_preserves_saved_ac_policy():
    hass = _SEHass()
    command_mode = hass.states.get("select.solaredge_storage_command_mode")
    command_mode.state = "Charge from Solar Power and Grid"
    command_mode.attributes["options"] = [
        "Solar Power Only (Off)",
        "Charge from Solar Power and Grid",
        "Maximize Self Consumption",
    ]
    hass.states.get("number.solaredge_storage_command_timeout").state = "5400"
    hass.states._states.pop("switch.solaredge_allow_grid_charge")
    policy_entity_id = "select.solaredge_i1_ac_charge_policy"
    hass.states._states[policy_entity_id] = _SEState(
        policy_entity_id,
        "Disabled",
        {"options": ["Disabled", "Always Allowed"]},
    )
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    assert asyncio.run(controller.force_charge(duration_minutes=30, power_w=4200))
    assert hass.states.get(policy_entity_id).state == "Always Allowed"

    assert asyncio.run(controller.restore_normal())
    assert hass.states.get(policy_entity_id).state == "Disabled"
    assert hass.states.get("select.solaredge_storage_control_mode").state == (
        "Maximize Self Consumption"
    )
    assert hass.states.get("select.solaredge_storage_command_mode").state == (
        "Solar Power Only (Off)"
    )


def test_solaredge_charge_alias_does_not_match_discharge_option():
    hass = _SEHass()
    command_mode = hass.states.get("select.solaredge_storage_command_mode")
    command_mode.state = "Discharge to Maximize Export"
    command_mode.attributes["options"] = ["Discharge to Maximize Export"]
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")

    assert (
        controller._match_select_option(
            "select.solaredge_storage_command_mode", ("charge",)
        )
        is None
    )


def test_solaredge_restore_normal_discards_saved_active_dispatch_snapshot():
    hass = _SEHass()
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    controller._saved_control_state = {
        "storage_control_mode": "Remote Control",
        "storage_command_mode": "Charge",
        "charge_power_limit": "4200",
        "discharge_power_limit": "0",
        "command_timeout": "5400",
        "allow_grid_charge": "on",
    }

    assert asyncio.run(controller.restore_normal())

    assert hass.states.get("select.solaredge_storage_control_mode").state == (
        "Maximize Self Consumption"
    )
    assert hass.states.get("select.solaredge_storage_command_mode").state == "Stop"
    assert hass.states.get("number.solaredge_storage_charge_limit").state == "0.0"
    assert hass.states.get("number.solaredge_storage_discharge_limit").state == "0.0"
    assert hass.states.get("number.solaredge_storage_command_timeout").state == "0.0"


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
