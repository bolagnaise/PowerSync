from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"


def _load_goodwe_entity_module():
    saved = {
        name: sys.modules.get(name)
        for name in (
            "power_sync",
            "power_sync.inverters",
            "power_sync.inverters.goodwe_entity",
        )
    }

    power_sync = types.ModuleType("power_sync")
    power_sync.__path__ = [str(COMPONENT_ROOT)]
    sys.modules["power_sync"] = power_sync

    inverters = types.ModuleType("power_sync.inverters")
    inverters.__path__ = [str(COMPONENT_ROOT / "inverters")]
    sys.modules["power_sync.inverters"] = inverters
    sys.modules.pop("power_sync.inverters.goodwe_entity", None)

    module = importlib.import_module("power_sync.inverters.goodwe_entity")

    def restore() -> None:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    return module, restore


class _State:
    def __init__(self, state, unit: str | None = None) -> None:
        self.state = state
        self.attributes = {}
        if unit:
            self.attributes["unit_of_measurement"] = unit


class _States:
    def __init__(self, states: dict[str, _State]) -> None:
        self._states = states

    def get(self, entity_id: str | None):
        return self._states.get(entity_id or "")

    def async_entity_ids(self, domain: str | None = None) -> list[str]:
        return sorted(
            entity_id
            for entity_id in self._states
            if domain is None or entity_id.startswith(f"{domain}.")
        )


class _Hass:
    def __init__(self, states: dict[str, _State]) -> None:
        self.states = _States(states)


def _state_map(prefix: str = "goodwe") -> dict[str, _State]:
    return {
        f"sensor.{prefix}_battery_soc": _State(64),
        f"sensor.{prefix}_battery_power": _State(1200, "W"),
        f"sensor.{prefix}_active_power": _State(2000, "W"),
        f"sensor.{prefix}_ppv": _State(5000, "W"),
        f"sensor.{prefix}_house_consumption": _State(4200, "W"),
        f"sensor.{prefix}_battery_temperature": _State(23.4),
        f"sensor.{prefix}_battery_soh": _State(98),
        f"sensor.{prefix}_rated_power": _State(10000, "W"),
        f"sensor.{prefix}_work_mode": _State("General"),
        f"sensor.{prefix}_total_pv_generation_today": _State(12.345, "kWh"),
    }


def test_goodwe_entity_controller_reads_prefixed_runtime_data():
    module, restore_module = _load_goodwe_entity_module()
    try:
        controller = module.GoodWeEntityTelemetryController(
            _Hass(_state_map()),
            entity_prefix="goodwe",
        )

        assert asyncio.run(controller.connect())
        data = controller.get_runtime_data()

        assert data["battery_level"] == 64
        assert data["battery_power"] == 1.2
        assert data["grid_power"] == -2.0
        assert data["solar_power"] == 5.0
        assert data["load_power"] == 4.2
        assert data["battery_temperature"] == 23.4
        assert data["battery_soh"] == 98
        assert data["rated_power_w"] == 10000
        assert data["work_mode"] == "General"
        assert data["daily_solar_energy_kwh"] == 12.345
        assert data["entity_telemetry"] is True
    finally:
        restore_module()


def test_goodwe_entity_controller_autodetects_single_complete_prefix():
    module, restore_module = _load_goodwe_entity_module()
    try:
        controller = module.GoodWeEntityTelemetryController(_Hass(_state_map("gw")))

        assert asyncio.run(controller.connect())

        assert controller.entity_prefix == "gw"
    finally:
        restore_module()


def test_goodwe_entity_controller_computes_load_when_missing():
    module, restore_module = _load_goodwe_entity_module()
    try:
        states = _state_map()
        states.pop("sensor.goodwe_house_consumption")
        controller = module.GoodWeEntityTelemetryController(_Hass(states), "goodwe")

        assert asyncio.run(controller.connect())
        data = controller.get_runtime_data()

        assert data["load_power"] == 4.2
    finally:
        restore_module()


def test_goodwe_entity_controller_rejects_unavailable_required_sensor():
    module, restore_module = _load_goodwe_entity_module()
    try:
        states = _state_map()
        states["sensor.goodwe_battery_soc"] = _State("unavailable")
        controller = module.GoodWeEntityTelemetryController(_Hass(states), "goodwe")

        try:
            asyncio.run(controller.connect())
        except ValueError as err:
            assert "goodwe_entity_missing_entities:" in str(err)
            assert "sensor.goodwe_battery_soc" in str(err)
        else:
            raise AssertionError("expected unavailable SOC to fail validation")
    finally:
        restore_module()


def test_goodwe_entity_controller_reports_missing_required_power():
    module, restore_module = _load_goodwe_entity_module()
    try:
        states = _state_map()
        states.pop("sensor.goodwe_battery_power")
        controller = module.GoodWeEntityTelemetryController(_Hass(states), "goodwe")

        try:
            asyncio.run(controller.connect())
        except ValueError as err:
            assert "goodwe_entity_missing_entities:" in str(err)
            assert "sensor.goodwe_battery_power" in str(err)
        else:
            raise AssertionError("expected missing battery power to fail validation")
    finally:
        restore_module()
