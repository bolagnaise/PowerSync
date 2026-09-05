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

from power_sync.inverters.solaredge import (
    SolarEdgeController,
    SolarEdgeEnergyController,
)


class _MemoryStore:
    def __init__(self):
        self.data = None
        self.history = []

    async def async_load(self):
        import copy

        return copy.deepcopy(self.data)

    async def async_save(self, data):
        import copy

        self.data = copy.deepcopy(data)
        self.history.append(self.data)


@pytest.fixture(autouse=True)
def control_journal(monkeypatch):
    stores = {}

    def create(controller, identity):
        return stores.setdefault((id(controller.hass), identity), _MemoryStore())

    monkeypatch.setattr(SolarEdgeEnergyController, "_create_store", create)
    return stores


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


@pytest.mark.parametrize("entity_prefix", ["custom", "custom_*"])
def test_solaredge_entity_fallback_prefers_configured_prefix(entity_prefix: str):
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
        entity_prefix=entity_prefix,
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
                "sensor.solaredge_i1_dc_power": State(
                    "sensor.solaredge_i1_dc_power",
                    "3200",
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
    assert status["solar_power"] == pytest.approx(1.4)
    assert status["load_power"] == pytest.approx(3.7)


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


@pytest.mark.parametrize(
    ("inverter_dc", "battery_dc", "expected_solar"),
    [
        (1.25, -1.25, 0.0),  # nighttime battery discharge
        (4.0, 1.0, 5.0),  # daylight battery charging
        (4.0, -1.0, 3.0),  # daylight battery discharging
    ],
)
def test_solaredge_energy_bridge_reconstructs_pv_from_dc_and_battery(
    inverter_dc: float,
    battery_dc: float,
    expected_solar: float,
):
    class Hass:
        def __init__(self) -> None:
            self.states = _SEStates(
                {
                    "sensor.solaredge_b1_state_of_energy": _SEState(
                        "sensor.solaredge_b1_state_of_energy", "55", {"unit_of_measurement": "%"}
                    ),
                    "sensor.solaredge_i1_ac_power": _SEState(
                        "sensor.solaredge_i1_ac_power", "1.2", {"unit_of_measurement": "kW"}
                    ),
                    "sensor.solaredge_i1_dc_power": _SEState(
                        "sensor.solaredge_i1_dc_power", str(inverter_dc), {"unit_of_measurement": "kW"}
                    ),
                    "sensor.solaredge_i1_b1_dc_power": _SEState(
                        "sensor.solaredge_i1_b1_dc_power", str(battery_dc), {"unit_of_measurement": "kW"}
                    ),
                }
            )

    controller = SolarEdgeEnergyController(Hass(), entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    status = controller.get_status()

    assert status["solar_power"] == pytest.approx(expected_solar)


def test_solaredge_energy_bridge_prefers_pv_strings_and_sums_battery_channels():
    class Hass:
        def __init__(self) -> None:
            self.states = _SEStates(
                {
                    "sensor.solaredge_b1_state_of_energy": _SEState(
                        "sensor.solaredge_b1_state_of_energy", "55", {"unit_of_measurement": "%"}
                    ),
                    "sensor.solaredge_i1_ac_power": _SEState(
                        "sensor.solaredge_i1_ac_power", "9.0", {"unit_of_measurement": "kW"}
                    ),
                    "sensor.solaredge_i1_dc_power": _SEState(
                        "sensor.solaredge_i1_dc_power", "8.0", {"unit_of_measurement": "kW"}
                    ),
                    "sensor.solaredge_i1_b1_dc_power": _SEState(
                        "sensor.solaredge_i1_b1_dc_power", "-1.0", {"unit_of_measurement": "kW"}
                    ),
                    "sensor.solaredge_i1_b2_dc_power": _SEState(
                        "sensor.solaredge_i1_b2_dc_power", "-0.5", {"unit_of_measurement": "kW"}
                    ),
                    "sensor.solaredge_i1_pv1_power": _SEState(
                        "sensor.solaredge_i1_pv1_power", "0.6", {"unit_of_measurement": "kW"}
                    ),
                    "sensor.solaredge_i1_pv2_power": _SEState(
                        "sensor.solaredge_i1_pv2_power", "0.4", {"unit_of_measurement": "kW"}
                    ),
                }
            )

    controller = SolarEdgeEnergyController(Hass(), entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    status = controller.get_status()

    assert status["solar_power"] == pytest.approx(1.0)
    assert status["battery_power"] == pytest.approx(1.5)


def test_solaredge_energy_bridge_rejects_pv_strings_with_incomplete_battery_channels():
    class Hass:
        def __init__(self) -> None:
            self.states = _SEStates(
                {
                    "sensor.solaredge_b1_state_of_energy": _SEState(
                        "sensor.solaredge_b1_state_of_energy", "55", {"unit_of_measurement": "%"}
                    ),
                    "sensor.solaredge_i1_dc_power": _SEState(
                        "sensor.solaredge_i1_dc_power", "2.0", {"unit_of_measurement": "kW"}
                    ),
                    "sensor.solaredge_i1_b1_dc_power": _SEState(
                        "sensor.solaredge_i1_b1_dc_power", "-0.5", {"unit_of_measurement": "kW"}
                    ),
                    "sensor.solaredge_i1_b2_dc_power": _SEState(
                        "sensor.solaredge_i1_b2_dc_power", "unavailable", {"unit_of_measurement": "kW"}
                    ),
                    "sensor.solaredge_i1_pv1_power": _SEState(
                        "sensor.solaredge_i1_pv1_power", "1.0", {"unit_of_measurement": "kW"}
                    ),
                    "sensor.solaredge_i1_pv2_power": _SEState(
                        "sensor.solaredge_i1_pv2_power", "0.5", {"unit_of_measurement": "kW"}
                    ),
                }
            )

    controller = SolarEdgeEnergyController(Hass(), entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    status = controller.get_status()

    assert status["solar_power"] == 0.0
    assert status["telemetry_ready"] is False


def test_solaredge_energy_bridge_uses_complete_pv_strings_without_inverter_dc():
    class Hass:
        def __init__(self) -> None:
            self.states = _SEStates(
                {
                    "sensor.solaredge_b1_state_of_energy": _SEState(
                        "sensor.solaredge_b1_state_of_energy", "55", {"unit_of_measurement": "%"}
                    ),
                    "sensor.solaredge_i1_dc_power": _SEState(
                        "sensor.solaredge_i1_dc_power", "unknown", {"unit_of_measurement": "kW"}
                    ),
                    "sensor.solaredge_i1_b1_dc_power": _SEState(
                        "sensor.solaredge_i1_b1_dc_power", "-0.5", {"unit_of_measurement": "kW"}
                    ),
                    "sensor.solaredge_i1_pv1_power": _SEState(
                        "sensor.solaredge_i1_pv1_power", "1.0", {"unit_of_measurement": "kW"}
                    ),
                    "sensor.solaredge_i1_pv2_power": _SEState(
                        "sensor.solaredge_i1_pv2_power", "0.5", {"unit_of_measurement": "kW"}
                    ),
                }
            )

    controller = SolarEdgeEnergyController(Hass(), entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    status = controller.get_status()

    assert status["solar_power"] == pytest.approx(1.5)
    assert status["telemetry_ready"] is True


@pytest.mark.parametrize("invalid_value", ["nan", "inf", "-inf"])
def test_solaredge_energy_bridge_rejects_non_finite_power(invalid_value: str):
    class Hass:
        def __init__(self) -> None:
            self.states = _SEStates(
                {
                    "sensor.solaredge_b1_state_of_energy": _SEState(
                        "sensor.solaredge_b1_state_of_energy", "55", {"unit_of_measurement": "%"}
                    ),
                    "sensor.solaredge_i1_dc_power": _SEState(
                        "sensor.solaredge_i1_dc_power", "2.0", {"unit_of_measurement": "kW"}
                    ),
                    "sensor.solaredge_i1_b1_dc_power": _SEState(
                        "sensor.solaredge_i1_b1_dc_power", invalid_value, {"unit_of_measurement": "kW"}
                    ),
                }
            )

    controller = SolarEdgeEnergyController(Hass(), entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    status = controller.get_status()

    assert status["battery_power"] == 0.0
    assert status["solar_power"] == 0.0
    assert status["telemetry_ready"] is False


@pytest.mark.parametrize(
    ("inverter_dc", "battery_dc"),
    [(None, -1.0), (2.0, None)],
)
def test_solaredge_energy_bridge_fails_closed_when_dc_is_unavailable(
    inverter_dc: float | None,
    battery_dc: float | None,
):
    def state(entity_id: str, value: float | None) -> _SEState:
        return _SEState(
            entity_id,
            "unavailable" if value is None else str(value),
            {"unit_of_measurement": "kW"},
        )

    states = {
        "sensor.solaredge_b1_state_of_energy": _SEState(
            "sensor.solaredge_b1_state_of_energy", "55", {"unit_of_measurement": "%"}
        ),
        "sensor.solaredge_i1_ac_power": state("sensor.solaredge_i1_ac_power", 2.2),
        "sensor.solaredge_i1_dc_power": state("sensor.solaredge_i1_dc_power", inverter_dc),
        "sensor.solaredge_i1_b1_dc_power": state(
            "sensor.solaredge_i1_b1_dc_power", battery_dc
        ),
    }

    class Hass:
        def __init__(self) -> None:
            self.states = _SEStates(states)

    controller = SolarEdgeEnergyController(Hass(), entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    status = controller.get_status()
    assert status["solar_power"] == 0.0
    assert status["telemetry_ready"] is False


def test_solaredge_energy_bridge_preserves_ac_priority_without_battery_source():
    class Hass:
        def __init__(self) -> None:
            self.states = _SEStates(
                {
                    "sensor.solaredge_b1_state_of_energy": _SEState(
                        "sensor.solaredge_b1_state_of_energy", "55", {"unit_of_measurement": "%"}
                    ),
                    "sensor.solaredge_i1_ac_power": _SEState(
                        "sensor.solaredge_i1_ac_power", "1.2", {"unit_of_measurement": "kW"}
                    ),
                    "sensor.solaredge_i1_dc_power": _SEState(
                        "sensor.solaredge_i1_dc_power", "2.5", {"unit_of_measurement": "kW"}
                    ),
                }
            )

    controller = SolarEdgeEnergyController(Hass(), entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    assert controller.get_status()["solar_power"] == pytest.approx(1.2)


def test_solaredge_energy_bridge_maps_generic_dc_as_inverter_without_battery():
    class Hass:
        def __init__(self) -> None:
            self.states = _SEStates(
                {
                    "sensor.solaredge_b1_state_of_energy": _SEState(
                        "sensor.solaredge_b1_state_of_energy", "55", {"unit_of_measurement": "%"}
                    ),
                    "sensor.solaredge_dc_power": _SEState(
                        "sensor.solaredge_dc_power", "2.5", {"unit_of_measurement": "kW"}
                    ),
                }
            )

    controller = SolarEdgeEnergyController(Hass(), entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    status = controller.get_status()

    assert controller._entity_map["inverter_dc_power"] == "sensor.solaredge_dc_power"
    assert "battery_power" not in controller._entity_map
    assert status["solar_power"] == pytest.approx(2.5)
    assert status["telemetry_ready"] is True


def test_solaredge_energy_bridge_rejects_unavailable_discovered_battery_channel():
    class Hass:
        def __init__(self) -> None:
            self.states = _SEStates(
                {
                    "sensor.solaredge_b1_state_of_energy": _SEState(
                        "sensor.solaredge_b1_state_of_energy", "55", {"unit_of_measurement": "%"}
                    ),
                    "sensor.solaredge_i1_ac_power": _SEState(
                        "sensor.solaredge_i1_ac_power", "2.2", {"unit_of_measurement": "kW"}
                    ),
                    "sensor.solaredge_i1_dc_power": _SEState(
                        "sensor.solaredge_i1_dc_power", "2.0", {"unit_of_measurement": "kW"}
                    ),
                    "sensor.solaredge_i1_b1_dc_power": _SEState(
                        "sensor.solaredge_i1_b1_dc_power", "unavailable", {"unit_of_measurement": "kW"}
                    ),
                    "sensor.solaredge_i1_b2_dc_power": _SEState(
                        "sensor.solaredge_i1_b2_dc_power", "-0.5", {"unit_of_measurement": "kW"}
                    ),
                }
            )

    controller = SolarEdgeEnergyController(Hass(), entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    status = controller.get_status()

    assert status["battery_power"] == 0.0
    assert status["solar_power"] == 0.0
    assert status["telemetry_ready"] is False


def test_solaredge_energy_bridge_allows_ac_fallback_without_battery_source():
    class Hass:
        def __init__(self) -> None:
            self.states = _SEStates(
                {
                    "sensor.solaredge_b1_state_of_energy": _SEState(
                        "sensor.solaredge_b1_state_of_energy", "55", {"unit_of_measurement": "%"}
                    ),
                    "sensor.solaredge_i1_ac_power": _SEState(
                        "sensor.solaredge_i1_ac_power", "2.2", {"unit_of_measurement": "kW"}
                    ),
                }
            )

    controller = SolarEdgeEnergyController(Hass(), entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    status = controller.get_status()

    assert status["solar_power"] == pytest.approx(2.2)
    assert status["telemetry_ready"] is True


def test_solaredge_ac_daily_energy_is_not_a_pv_total_when_battery_is_present():
    class Hass:
        def __init__(self) -> None:
            self.states = _SEStates(
                {
                    "sensor.solaredge_b1_state_of_energy": _SEState(
                        "sensor.solaredge_b1_state_of_energy", "55", {"unit_of_measurement": "%"}
                    ),
                    "sensor.solaredge_b1_dc_power": _SEState(
                        "sensor.solaredge_b1_dc_power", "-1.25", {"unit_of_measurement": "kW"}
                    ),
                    "sensor.solaredge_i1_ac_energy_today": _SEState(
                        "sensor.solaredge_i1_ac_energy_today", "12.5", {"unit_of_measurement": "kWh"}
                    ),
                }
            )

    controller = SolarEdgeEnergyController(Hass(), entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    assert controller.get_status()["daily_solar_energy_kwh"] is None


def test_solaredge_explicit_solar_daily_energy_is_preserved_with_battery():
    class Hass:
        def __init__(self) -> None:
            self.states = _SEStates(
                {
                    "sensor.solaredge_b1_state_of_energy": _SEState(
                        "sensor.solaredge_b1_state_of_energy", "55", {"unit_of_measurement": "%"}
                    ),
                    "sensor.solaredge_b1_dc_power": _SEState(
                        "sensor.solaredge_b1_dc_power", "-1.25", {"unit_of_measurement": "kW"}
                    ),
                    "sensor.solaredge_i1_ac_energy_today": _SEState(
                        "sensor.solaredge_i1_ac_energy_today", "12.5", {"unit_of_measurement": "kWh"}
                    ),
                    "sensor.solaredge_solar_energy_today": _SEState(
                        "sensor.solaredge_solar_energy_today", "7.5", {"unit_of_measurement": "kWh"}
                    ),
                }
            )

    controller = SolarEdgeEnergyController(Hass(), entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    assert controller.get_status()["daily_solar_energy_kwh"] == pytest.approx(7.5)


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
    assert status["solar_power"] == 0.0
    assert status["load_power"] == pytest.approx(4.419)


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
        reflect_delay: float | None = None,
    ) -> None:
        super().__init__(states)
        self._failing_entity_id = failing_entity_id
        self._reflect_delay = reflect_delay

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
        if self._reflect_delay is not None:
            async def reflect_state():
                await asyncio.sleep(self._reflect_delay)
                state = self._states.get(self._failing_entity_id)
                if state and domain == "select":
                    state.state = str(data["option"])
                elif state and domain == "number":
                    state.state = str(data["value"])

            asyncio.create_task(reflect_state())
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
            "sensor.solaredge_i1_dc_power": _SEState(
                "sensor.solaredge_i1_dc_power",
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
    for entity_id in list(hass.states._states):
        if entity_id.startswith(
            ("select.solaredge_", "number.solaredge_", "switch.solaredge_")
        ) and "_i1_" not in entity_id:
            state = hass.states._states.pop(entity_id)
            state.entity_id = entity_id.replace("solaredge_", "solaredge_i1_", 1)
            hass.states._states[state.entity_id] = state
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


def test_solaredge_control_discovery_accepts_trailing_wildcard_prefix():
    hass = _SEHass()
    for entity_id in list(hass.states._states):
        if entity_id.startswith(("select.solaredge_", "number.solaredge_", "switch.solaredge_")):
            state = hass.states._states.pop(entity_id)
            state.entity_id = entity_id.replace("solaredge_", "solaredge_i1_", 1)
            hass.states._states[state.entity_id] = state

    controller = SolarEdgeEnergyController(
        hass,
        entity_prefix="solaredge_i1_*",
    )

    assert asyncio.run(controller.connect())
    assert controller.control_available()
    assert controller._control_entity_map["storage_control_mode"] == (
        "select.solaredge_i1_storage_control_mode"
    )
    assert controller._control_entity_map["charge_power_limit"] == (
        "number.solaredge_i1_storage_charge_limit"
    )


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


def test_solaredge_control_discovery_continues_after_unusable_candidate():
    hass = _SEHass()
    hass.states.get("number.solaredge_storage_charge_limit").attributes["max"] = 0
    hass.states._states["number.solaredge_battery_charge_power_limit"] = _SEState(
        "number.solaredge_battery_charge_power_limit",
        "0",
        {"min": 0, "max": 6000},
    )
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    assert controller._control_entity_map["charge_power_limit"] == (
        "number.solaredge_battery_charge_power_limit"
    )


def test_solaredge_control_discovery_keeps_unavailable_remote_controls():
    hass = _SEHass()
    for entity_id in (
        "select.solaredge_storage_control_mode",
        "select.solaredge_storage_command_mode",
        "number.solaredge_storage_charge_limit",
        "number.solaredge_storage_discharge_limit",
        "number.solaredge_storage_command_timeout",
    ):
        hass.states.get(entity_id).state = "unavailable"
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    assert controller.control_available()
    assert controller.missing_control_entities() == []


def test_solaredge_control_discovery_rejects_ac_charge_energy_limit():
    hass = _SEHass()
    hass.states._states.pop("number.solaredge_storage_charge_limit")
    hass.states._states["number.solaredge_i1_storage_ac_charge_limit"] = _SEState(
        "number.solaredge_i1_storage_ac_charge_limit",
        "1000",
        {"unit_of_measurement": "kWh", "min": 0, "max": 100000000},
    )
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    assert "charge_power_limit" not in controller._control_entity_map


def test_solaredge_control_discovery_keeps_usable_ac_charge_limit_fallback():
    hass = _SEHass()
    hass.states._states.pop("number.solaredge_storage_charge_limit")
    hass.states._states["number.solaredge_ac_charge_limit"] = _SEState(
        "number.solaredge_ac_charge_limit",
        "0",
        {"min": 0, "max": 6000},
    )
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    assert controller._control_entity_map["charge_power_limit"] == (
        "number.solaredge_ac_charge_limit"
    )


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


def test_solaredge_force_charge_halts_when_optional_ac_policy_fails():
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
    assert not asyncio.run(controller.force_charge(duration_minutes=30, power_w=4200))
    assert hass.services.calls[-1][2]["entity_id"] == policy_entity_id
    assert controller.control_health == "reconciliation_required"
    assert (
        hass.states.get("select.solaredge_storage_command_mode").state
        == "Maximize Self Consumption"
    )


def test_solaredge_force_charge_halts_when_required_ac_policy_fails():
    hass = _SEHass()
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
    assert not asyncio.run(controller.force_charge(duration_minutes=30, power_w=4200))
    assert (
        hass.states.get("select.solaredge_storage_control_mode").state
        == "Remote Control"
    )
    assert hass.services.calls[-1][2]["entity_id"] == policy_entity_id
    assert (
        controller._saved_control_state["storage_control_mode"]
        == "Maximize Self Consumption"
    )


def test_solaredge_force_charge_rejects_timeout_even_if_state_is_reflected():
    hass = _SEHass()
    command_entity_id = "select.solaredge_storage_command_mode"
    hass.services = _SEFailingServices(
        hass.states,
        command_entity_id,
        reflect_delay=3.1,
    )
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    assert not asyncio.run(controller.force_charge(duration_minutes=30, power_w=4200))
    assert controller.last_mutation["outcome"] == "unknown"
    assert hass.services.calls[-1][2]["entity_id"] == command_entity_id


def test_solaredge_force_charge_rejects_number_timeout_even_if_state_is_reflected():
    hass = _SEHass()
    timeout_entity_id = "number.solaredge_storage_command_timeout"
    hass.services = _SEFailingServices(
        hass.states,
        timeout_entity_id,
        reflect_delay=0.01,
    )
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    assert not asyncio.run(controller.force_charge(duration_minutes=30, power_w=4200))
    assert controller.last_mutation["outcome"] == "unknown"
    assert hass.services.calls[-1][2]["entity_id"] == timeout_entity_id


def test_solaredge_stale_dispatch_is_rejected_before_mutation():
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
    assert not asyncio.run(controller.force_charge(duration_minutes=30, power_w=4200))
    assert hass.services.calls == []
    assert controller.last_mutation["outcome"] == "rejected"


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


def test_solaredge_restore_normal_retains_and_rejects_active_dispatch_snapshot():
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

    assert not asyncio.run(controller.restore_normal())
    assert hass.services.calls == []
    assert controller._saved_control_state["storage_command_mode"] == "Charge"


def test_solaredge_force_discharge_writes_remote_discharge_entities():
    hass = _SEHass()
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")

    assert asyncio.run(controller.connect())
    assert asyncio.run(controller.force_discharge(duration_minutes=15, power_w=3500))

    assert not any(
        call[2]["entity_id"] == "number.solaredge_storage_charge_limit"
        for call in hass.services.calls
    )
    assert (
        "number",
        "set_value",
        {
            "entity_id": "number.solaredge_storage_discharge_limit",
            "value": 3500.0,
        },
    ) in hass.services.calls
    assert (
        "select",
        "select_option",
        {
            "entity_id": "select.solaredge_storage_command_mode",
            "option": "Discharge",
        },
    ) in hass.services.calls


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
        "command_timeout",
    ]
    assert not asyncio.run(controller.force_charge(duration_minutes=30, power_w=4200))
    assert hass.services.calls == []


def test_solaredge_benign_msc_timeout_is_not_active_dispatch():
    controller = SolarEdgeEnergyController(_SEHass(), entity_prefix="solaredge")
    controller._saved_control_state = {
        "storage_control_mode": "Remote Control",
        "storage_command_mode": "Maximize Self Consumption",
        "charge_power_limit": "11400",
        "discharge_power_limit": "11400",
        "command_timeout": "3600",
    }
    assert not controller._saved_control_state_contains_active_dispatch()


def test_solaredge_unknown_first_write_halts_without_rollback():
    hass = _SEHass()
    entity = "select.solaredge_storage_control_mode"
    hass.services = _SEFailingServices(hass.states, entity)
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
    controller.WRITE_CONFIRM_TIMEOUT_SECONDS = 0
    assert not asyncio.run(controller.force_discharge(15, 2000))
    assert len(hass.services.calls) == 1
    assert controller.control_health == "reconciliation_required"
    assert controller.last_mutation["outcome"] == "unknown"
    assert controller._saved_control_state["storage_command_mode"] == "Stop"
    assert not asyncio.run(controller.restore_normal())
    assert len(hass.services.calls) == 1


@pytest.mark.parametrize(
    "key,value",
    [
        ("number.solaredge_storage_discharge_limit", "nan"),
        ("select.solaredge_storage_command_mode", "unavailable"),
    ],
)
def test_solaredge_complete_preflight_rejects_before_any_write(key, value):
    hass = _SEHass()
    hass.states.get(key).state = value
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
    assert not asyncio.run(controller.force_discharge(15, 2000))
    assert hass.services.calls == []
    assert controller.last_mutation["outcome"] == "rejected"
    assert controller.control_health == "ready"


@pytest.mark.parametrize("power", [7000, float("nan"), -1])
def test_solaredge_out_of_range_power_rejected_before_write(power):
    hass = _SEHass()
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
    assert not asyncio.run(controller.force_discharge(15, power))
    assert hass.services.calls == []


def test_solaredge_no_substring_grid_charge_fallback():
    hass = _SEHass()
    state = hass.states.get("select.solaredge_storage_command_mode")
    state.attributes["options"] = [
        "Stop",
        "Charge from Clipped Solar Power",
        "Charge from Solar Power",
    ]
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
    assert not asyncio.run(controller.force_charge(15, 2000))
    assert hass.services.calls == []


def test_solaredge_unknown_middle_stops_all_waiting_mutators():
    async def scenario():
        hass = _SEHass()
        controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
        entered, release = asyncio.Event(), asyncio.Event()
        service = hass.services.async_call

        async def pause(domain, action, data, blocking=False):
            if data["entity_id"].endswith("command_timeout"):
                hass.services.calls.append((domain, action, data))
                entered.set()
                await release.wait()
                raise TimeoutError("transaction_id mismatch")
            return await service(domain, action, data, blocking)

        hass.services.async_call = pause
        first = asyncio.create_task(controller.force_discharge(15, 2000))
        await entered.wait()
        queued = [
            asyncio.create_task(method())
            for method in [
                controller.restore_normal,
                controller.set_backup_mode,
                lambda: controller.set_backup_reserve(22),
                lambda: controller.force_charge(15, 3000),
            ]
        ]
        await asyncio.sleep(0)
        assert len(hass.services.calls) == 2
        assert not await controller.force_charge(15, 3000, automatic=True)
        release.set()
        assert not await first
        assert not any(await asyncio.gather(*queued))
        assert len(hass.services.calls) == 2
        assert not controller.mutation_active

    asyncio.run(scenario())


@pytest.mark.parametrize("during_confirmation", [False, True])
def test_solaredge_cancellation_latches_and_releases_lock(during_confirmation):
    async def scenario():
        hass = _SEHass()
        controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
        entered = asyncio.Event()
        if during_confirmation:

            async def wait(*args):
                entered.set()
                await asyncio.Future()

            controller._wait_for_reflected_state = wait
        else:

            async def call(domain, service, data, blocking=False):
                hass.services.calls.append((domain, service, data))
                entered.set()
                await asyncio.Future()

            hass.services.async_call = call
        task = asyncio.create_task(controller.force_discharge(15, 2000))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert controller.control_health == "reconciliation_required"
        assert not controller.mutation_active
        assert not await controller.restore_normal()
        assert len(hass.services.calls) == 1

    asyncio.run(scenario())


def test_solaredge_same_inverter_shares_lock_health_baseline():
    async def scenario():
        hass = _SEHass()
        first = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
        second = SolarEdgeEnergyController(hass, entity_prefix="solaredge_*")
        assert first._coordinator() is second._coordinator()
        hass.services = _SEFailingServices(
            hass.states, "select.solaredge_storage_control_mode"
        )
        assert not await first.force_discharge(15, 2000)
        assert second.control_health == "reconciliation_required"
        assert second._saved_control_state == first._saved_control_state
        assert not await second.set_backup_reserve(25)
        assert len(hass.services.calls) == 1

    asyncio.run(scenario())


def test_solaredge_separate_inverters_do_not_share_lock():
    async def scenario():
        hass = _SEHass()
        other = _SEHass()
        for entity, state in list(other.states._states.items()):
            state.entity_id = entity.replace("solaredge", "other")
            hass.states._states[state.entity_id] = state
        first = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
        second = SolarEdgeEnergyController(hass, entity_prefix="other")
        async with first._coordinator().lock:
            assert await second.force_discharge(15, 2000)
        assert first.generation == 0

    asyncio.run(scenario())


def test_solaredge_reflection_failure_is_unknown():
    hass = _SEHass()

    async def no_reflection(domain, service, data, blocking=False):
        hass.services.calls.append((domain, service, data))

    hass.services.async_call = no_reflection
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
    controller.WRITE_CONFIRM_TIMEOUT_SECONDS = 0
    assert not asyncio.run(controller.force_discharge(15, 2000))
    assert len(hass.services.calls) == 1
    assert controller.last_mutation["outcome"] == "unknown"


def test_solaredge_restore_only_owned_fields_and_renew_equal_timeout():
    hass = _SEHass()
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
    assert asyncio.run(controller.force_discharge(15, 2000))
    first = len(hass.services.calls)
    assert asyncio.run(controller.force_discharge(15, 2000))
    assert [call[2]["entity_id"] for call in hass.services.calls[first:]] == [
        "number.solaredge_storage_command_timeout"
    ]
    first = len(hass.services.calls)
    assert asyncio.run(controller.restore_normal())
    restored = {call[2]["entity_id"] for call in hass.services.calls[first:]}
    assert "number.solaredge_backup_reserve" not in restored
    assert "switch.solaredge_allow_grid_charge" not in restored
    assert "number.solaredge_storage_charge_limit" not in restored
    first = len(hass.services.calls)
    assert asyncio.run(controller.restore_normal())
    assert len(hass.services.calls) == first


def test_solaredge_restore_preserves_kw_native_baseline():
    hass = _SEHass()
    state = hass.states.get("number.solaredge_storage_discharge_limit")
    state.state = "5.5"
    state.attributes.update(unit_of_measurement="kW", max=6)
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
    assert asyncio.run(controller.force_discharge(15, 2000))
    assert state.state == "2.0"
    assert asyncio.run(controller.restore_normal())
    assert state.state == "5.5"


def test_solaredge_restore_external_change_rejected_without_writes():
    hass = _SEHass()
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
    assert asyncio.run(controller.force_discharge(15, 2000))
    hass.states.get("number.solaredge_storage_discharge_limit").state = "3000"
    count = len(hass.services.calls)
    assert not asyncio.run(controller.restore_normal())
    assert len(hass.services.calls) == count
    assert controller._saved_control_state


def test_solaredge_write_ahead_record_precedes_service_and_restart_blocks():
    async def scenario():
        hass = _SEHass()
        controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
        original = hass.services.async_call

        async def call(domain, service, data, blocking=False):
            store = controller._coordinator().store
            assert store.data["in_progress"] is True
            assert store.data["baseline"]
            return await original(domain, service, data, blocking)

        hass.services.async_call = call
        assert await controller.force_discharge(15, 2000)
        store = controller._coordinator().store
        hass._powersync_solaredge_controls.clear()
        restarted = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
        restarted._create_store = lambda identity: store
        await restarted.connect()
        assert restarted.control_health == "reconciliation_required"
        before = len(hass.services.calls)
        assert not await restarted.force_charge(15, 2000)
        assert len(hass.services.calls) == before

    asyncio.run(scenario())


def test_solaredge_journal_failure_prevents_first_write():
    hass = _SEHass()
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
    store = _MemoryStore()

    async def fail(data):
        raise OSError("disk full")

    store.async_save = fail
    controller._create_store = lambda identity: store
    assert not asyncio.run(controller.force_discharge(15, 2000))
    assert hass.services.calls == []
    assert controller.control_health == "reconciliation_required"


def test_solaredge_unknown_survives_restart_without_replay():
    hass = _SEHass()
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
    hass.services = _SEFailingServices(
        hass.states, "number.solaredge_storage_command_timeout"
    )
    assert not asyncio.run(controller.force_discharge(15, 2000))
    store = controller._coordinator().store
    baseline = controller._saved_control_state.copy()
    hass._powersync_solaredge_controls.clear()
    restarted = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
    restarted._create_store = lambda identity: store
    assert asyncio.run(restarted.connect())
    assert restarted.control_health == "reconciliation_required"
    assert restarted._saved_control_state == baseline
    count = len(hass.services.calls)
    assert not asyncio.run(restarted.restore_normal())
    assert len(hass.services.calls) == count


def test_solaredge_restore_already_baseline_clears_owned_fields():
    hass = _SEHass()
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
    assert asyncio.run(controller.force_discharge(15, 2000))
    for key, value in controller._saved_control_state.items():
        hass.states.get(controller._control_entity_map[key]).state = value
    count = len(hass.services.calls)
    assert asyncio.run(controller.restore_normal())
    assert controller._coordinator().owned == {}
    assert controller._saved_control_state is None
    assert len(hass.services.calls) == count
    assert asyncio.run(controller.restore_normal())


def test_solaredge_reserve_rejects_unsaved_active_dispatch():
    hass = _SEHass()
    hass.states.get("select.solaredge_storage_command_mode").state = "Charge"
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
    assert not asyncio.run(controller.set_backup_reserve(25))
    assert hass.services.calls == []


def test_solaredge_explicit_grid_charge_omits_unsupported_optional_policy():
    hass = _SEHass()
    command = hass.states.get("select.solaredge_storage_command_mode")
    command.attributes["options"].append("Charge from Solar Power and Grid")
    hass.states._states.pop("switch.solaredge_allow_grid_charge")
    entity = "select.solaredge_storage_ac_charge_policy"
    hass.states._states[entity] = _SEState(
        entity, "Disabled", {"options": ["Disabled", "Fixed Energy Limit"]}
    )
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
    assert asyncio.run(controller.force_charge(15, 2000))
    assert not any(call[2]["entity_id"] == entity for call in hass.services.calls)
    assert command.state == "Charge from Solar Power and Grid"


def test_solaredge_restore_journal_failure_retains_baseline():
    async def scenario():
        hass = _SEHass()
        controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
        assert await controller.force_discharge(15, 2000)
        baseline = dict(controller._saved_control_state)
        store = controller._coordinator().store
        save = store.async_save

        async def fail_final(data):
            if not data["in_progress"]:
                raise OSError("journal failure")
            await save(data)

        store.async_save = fail_final
        assert not await controller.restore_normal()
        assert controller._saved_control_state == baseline
        assert controller.control_health == "reconciliation_required"

    asyncio.run(scenario())


def test_solaredge_external_unknown_blocks_storage_control():
    async def scenario():
        hass = _SEHass()
        controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")

        async def callback():
            return False

        assert not await controller.run_external_mutation(callback)
        assert controller.control_health == "reconciliation_required"
        assert not await controller.force_discharge(15, 2000)
        assert hass.services.calls == []

    asyncio.run(scenario())


def test_solaredge_direct_write_typeerror_is_not_retried():
    class Client:
        connected = True

        def __init__(self):
            self.calls = 0

        async def write_register(self, address, value, slave=1):
            self.calls += 1
            raise TypeError("failure after possible transmission")

    controller = SolarEdgeController(host="test")
    controller._client = Client()
    controller._connected = True
    assert not asyncio.run(controller.restore())
    assert controller._client.calls == 1


def test_solaredge_entity_rename_keeps_physical_journal_identity():
    hass = _SEHass()
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
    entry = types.SimpleNamespace(
        config_entry_id="hub", unique_id="inverter_serial_storage_command_mode"
    )
    controller._registry_entry = lambda entity: entry
    first = controller._coordinator()
    first.health = "reconciliation_required"
    controller._control_entity_map["storage_command_mode"] = (
        "select.renamed_storage_command"
    )
    assert controller._coordinator() is first
    assert controller.control_health == "reconciliation_required"


def test_solaredge_cross_inverter_control_map_rejected():
    hass = _SEHass()
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")

    def registry(entity):
        return types.SimpleNamespace(
            config_entry_id="hub",
            unique_id=entity,
            device_id="i2" if entity and entity.endswith("discharge_limit") else "i1",
            platform="test",
        )

    controller._registry_entry = registry
    assert not asyncio.run(controller.force_discharge(15, 2000))
    assert hass.services.calls == []


def test_solaredge_newly_owned_field_captures_current_baseline():
    hass = _SEHass()
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
    assert asyncio.run(controller.set_backup_reserve(20))
    hass.states.get("number.solaredge_storage_discharge_limit").state = "4500"
    assert asyncio.run(controller.force_discharge(15, 2000))
    assert asyncio.run(controller.restore_normal())
    assert hass.states.get("number.solaredge_storage_discharge_limit").state == "4500.0"
    assert hass.states.get("number.solaredge_backup_reserve").state == "15.0"


@pytest.mark.parametrize("readback", [None, "active", "mismatch", "baseline"])
def test_solaredge_reconcile_requires_fresh_matching_benign_baseline(readback):
    async def scenario():
        hass = _SEHass()
        controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
        hass.services = _SEFailingServices(
            hass.states, "select.solaredge_storage_control_mode"
        )
        assert not await controller.force_discharge(15, 2000)
        snapshot = dict(controller._saved_control_state)
        if readback == "active":
            snapshot["storage_command_mode"] = "Charge"
        elif readback == "mismatch":
            snapshot["backup_reserve"] = "30"

        async def fresh():
            return None if readback is None else snapshot

        controller._fresh_storage_state = fresh
        count = len(hass.services.calls)
        assert await controller.reconcile() is (readback == "baseline")
        assert len(hass.services.calls) == count
        assert controller.control_health == (
            "ready" if readback == "baseline" else "reconciliation_required"
        )

    asyncio.run(scenario())


def test_solaredge_storage_reconcile_cannot_clear_external_unknown():
    async def scenario():
        hass = _SEHass()
        controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")

        async def callback():
            return False

        assert not await controller.run_external_mutation(callback)

        async def fresh():
            raise AssertionError("Storage read cannot resolve active power")

        controller._fresh_storage_state = fresh
        assert not await controller.reconcile()

    asyncio.run(scenario())


def test_solaredge_native_readback_conversion():
    hass = _SEHass()
    hass.states.get("number.solaredge_storage_discharge_limit").attributes[
        "unit_of_measurement"
    ] = "kW"
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
    controller._ensure_entity_map()
    assert controller._native_readback(
        {
            "charge_power_limit": 2000,
            "discharge_power_limit": 5500,
            "allow_grid_charge": "Disabled",
        }
    ) == {
        "charge_power_limit": 2000,
        "discharge_power_limit": 5.5,
        "allow_grid_charge": "off",
    }


def test_solaredge_restore_without_baseline_cannot_claim_external_dispatch_stopped():
    hass = _SEHass()
    hass.states.get("select.solaredge_storage_command_mode").state = "Charge"
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
    assert not asyncio.run(controller.restore_normal())
    assert hass.services.calls == []


def test_solaredge_stale_timer_generation_does_not_restore_new_dispatch():
    async def scenario():
        hass = _SEHass()
        controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
        assert await controller.force_discharge(15, 2000)
        previous = controller.generation
        assert await controller.force_charge(15, 3000)
        count = len(hass.services.calls)
        assert not await controller.restore_normal(expected_generation=previous)
        assert len(hass.services.calls) == count

    asyncio.run(scenario())


def test_solaredge_successful_dispatches_are_contiguous():
    async def scenario():
        hass = _SEHass()
        controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
        entered, release = asyncio.Event(), asyncio.Event()
        service = hass.services.async_call

        async def pause(domain, action, data, blocking=False):
            if not entered.is_set():
                entered.set()
                await release.wait()
            return await service(domain, action, data, blocking)

        hass.services.async_call = pause
        first = asyncio.create_task(controller.force_discharge(15, 2000))
        await entered.wait()
        second = asyncio.create_task(controller.force_charge(15, 3000))
        await asyncio.sleep(0)
        assert hass.services.calls == []
        release.set()
        assert all(await asyncio.gather(first, second))
        commands = [
            call[2].get("option")
            for call in hass.services.calls
            if call[2]["entity_id"].endswith("storage_command_mode")
        ]
        assert commands == ["Discharge", "Charge"]
        first_command_index = next(
            index
            for index, call in enumerate(hass.services.calls)
            if call[2].get("option") == "Discharge"
        )
        assert all(
            call[2].get("value") != 3000
            for call in hass.services.calls[:first_command_index]
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["preflight", "confirmation"])
def test_solaredge_supported_multi_requires_fresh_register_reads(failure):
    async def scenario():
        hass = _SEHass()
        controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
        def registry(entity):
            return types.SimpleNamespace(config_entry_id="hub", unique_id=entity, device_id="i1", platform="solaredge_modbus_multi")
        controller._registry_entry = registry
        controller._ensure_entity_map()
        snapshot = {key: hass.states.get(entity).state for key, entity in controller._control_entity_map.items()}
        reads = 0
        async def fresh():
            nonlocal reads
            reads += 1
            return snapshot if reads == 1 and failure == "confirmation" else None
        controller._fresh_storage_state = fresh
        assert not await controller.force_discharge(15, 2000)
        assert len(hass.services.calls) == (1 if failure == "confirmation" else 0)
        assert controller.last_mutation["outcome"] == ("unknown" if failure == "confirmation" else "rejected")
    asyncio.run(scenario())


def test_solaredge_stale_unchanged_mode_rejected_before_write():
    async def scenario():
        hass = _SEHass()
        hass.states.get("select.solaredge_storage_control_mode").state = "Remote Control"
        controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
        controller._registry_entry = lambda entity: types.SimpleNamespace(config_entry_id="hub", unique_id=entity, device_id="i1", platform="solaredge_modbus_multi")
        controller._ensure_entity_map()
        snapshot = {key: hass.states.get(entity).state for key, entity in controller._control_entity_map.items()}
        snapshot["storage_control_mode"] = "Maximize Self Consumption"
        async def fresh():
            return snapshot
        controller._fresh_storage_state = fresh
        assert not await controller.force_discharge(15, 2000)
        assert hass.services.calls == []
        assert controller.last_mutation["outcome"] == "rejected"
    asyncio.run(scenario())


def test_solaredge_physical_identity_survives_entry_recreation(monkeypatch):
    hass = _SEHass()
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
    device = types.SimpleNamespace(identifiers={("solaredge_modbus_multi", "model_serial")})
    dr = types.ModuleType("homeassistant.helpers.device_registry")
    dr.async_get = lambda hass: types.SimpleNamespace(async_get=lambda device_id: device)
    helpers = types.ModuleType("homeassistant.helpers")
    helpers.device_registry = dr
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.device_registry", dr)
    entry = types.SimpleNamespace(config_entry_id="old", unique_id="model_serial_storage_command_mode", device_id="old_device", platform="solaredge_modbus_multi")
    controller._registry_entry = lambda entity: entry
    session = controller._coordinator()
    session.health = "reconciliation_required"
    entry.config_entry_id = "new"
    entry.device_id = "new_device"
    replacement = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
    replacement._registry_entry = lambda entity: entry
    assert replacement._coordinator() is session


def test_solaredge_cancelled_reconciliation_save_keeps_containment():
    async def scenario():
        hass = _SEHass()
        controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
        hass.services = _SEFailingServices(hass.states, "select.solaredge_storage_control_mode")
        assert not await controller.force_discharge(15, 2000)
        baseline = dict(controller._saved_control_state)
        async def fresh():
            return baseline
        controller._fresh_storage_state = fresh
        saving = asyncio.Event()
        async def save(data):
            saving.set()
            await asyncio.Future()
        controller._coordinator().store.async_save = save
        task = asyncio.create_task(controller.reconcile())
        await saving.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert controller.control_health == "reconciliation_required"
        assert controller.last_mutation["outcome"] == "unknown"
        assert controller._saved_control_state == baseline
        assert not controller.mutation_active
    asyncio.run(scenario())


def test_solaredge_missing_timeout_disables_dispatch_capability():
    hass = _SEHass()
    hass.states._states.pop("number.solaredge_storage_command_timeout")
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
    assert asyncio.run(controller.connect())
    assert not controller.control_available()
    assert controller.missing_control_entities() == ["command_timeout"]
    assert not asyncio.run(controller.force_discharge(15, 500))
    assert hass.services.calls == []


def _native_controller_with_optional_readback(hass, omitted):
    controller = SolarEdgeEnergyController(hass, entity_prefix="solaredge")
    controller._ensure_entity_map()
    controller._registry_entry = lambda entity: types.SimpleNamespace(
        platform="solaredge_modbus_multi", config_entry_id="hub",
        device_id="inverter", unique_id=entity,
    )

    async def fresh():
        return {
            key: hass.states.get(entity).state
            for key, entity in controller._control_entity_map.items()
            if key not in omitted
        }

    controller._fresh_storage_state = fresh
    return controller


def test_solaredge_discharge_omits_unreadable_optional_baseline():
    hass = _SEHass()
    controller = _native_controller_with_optional_readback(
        hass, {"allow_grid_charge", "backup_reserve"}
    )
    assert asyncio.run(controller.force_discharge(15, 500))
    assert "allow_grid_charge" not in controller._saved_control_state
    assert "backup_reserve" not in controller._saved_control_state
    assert asyncio.run(controller.restore_normal())


def test_solaredge_optional_write_requires_fresh_baseline_before_any_write():
    hass = _SEHass()
    controller = _native_controller_with_optional_readback(hass, {"allow_grid_charge"})
    assert not asyncio.run(controller.force_charge(15, 500))
    assert hass.services.calls == []
    assert controller.last_mutation["outcome"] == "rejected"


def test_solaredge_optional_readback_lost_after_write_remains_unknown():
    hass = _SEHass()
    omitted = set()
    controller = _native_controller_with_optional_readback(hass, omitted)
    original_call = hass.services.async_call

    async def call(domain, service, data, blocking=False):
        await original_call(domain, service, data, blocking)
        if data["entity_id"] == "switch.solaredge_allow_grid_charge":
            omitted.add("allow_grid_charge")

    hass.services.async_call = call
    assert not asyncio.run(controller.force_charge(15, 500))
    assert hass.services.calls[-1][2]["entity_id"] == "switch.solaredge_allow_grid_charge"
    assert controller.control_health == "reconciliation_required"
    assert controller.last_mutation["outcome"] == "unknown"
    assert controller.last_mutation["possibly_transmitted"] is True
    assert "allow_grid_charge" in controller._saved_control_state
    calls = len(hass.services.calls)
    assert not asyncio.run(controller.restore_normal())
    assert len(hass.services.calls) == calls


def test_solaredge_unknown_grid_policy_is_not_coerced_to_switch_off():
    controller = SolarEdgeEnergyController(_SEHass(), entity_prefix="solaredge")
    controller._ensure_entity_map()
    assert controller._native_readback({}) == {}
    assert controller._native_readback({"allow_grid_charge": "Unknown policy"}) == {}


def test_solaredge_missing_owned_optional_readback_rejects_further_writes():
    hass = _SEHass()
    omitted = set()
    controller = _native_controller_with_optional_readback(hass, omitted)
    assert asyncio.run(controller.set_backup_reserve(20))
    omitted.add("backup_reserve")
    calls = len(hass.services.calls)
    assert not asyncio.run(controller.force_discharge(15, 500))
    assert len(hass.services.calls) == calls
    assert controller.last_mutation["outcome"] == "rejected"
