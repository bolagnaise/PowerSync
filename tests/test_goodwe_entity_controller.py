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
        self.services = _Services(self.states)
        self.data = {}


class _Services:
    def __init__(self, states: _States) -> None:
        self.states = states
        self.calls: list[tuple[str, str, dict]] = []
        self.fail_service: tuple[str, str] | None = None

    async def async_call(
        self, domain: str, service: str, data: dict, *, blocking: bool
    ) -> None:
        self.calls.append((domain, service, data))
        if self.fail_service == (domain, service):
            self.fail_service = None
            raise RuntimeError("simulated entity write failure")
        entity_id = data["entity_id"]
        if domain == "number":
            self.states._states[entity_id].state = data["value"]
        elif domain == "switch":
            self.states._states[entity_id].state = (
                "on" if service == "turn_on" else "off"
            )


class _Store:
    def __init__(self) -> None:
        self.data = None

    async def async_load(self):
        return self.data

    async def async_save(self, data) -> None:
        self.data = data


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


def _ac_state_map(prefix: str = "goodwe") -> dict[str, _State]:
    return {
        f"sensor.{prefix}_pv_power": _State(5200, "W"),
        f"sensor.{prefix}_pv1_power": _State(1700, "W"),
        f"sensor.{prefix}_pv2_power": _State(1800, "W"),
        f"sensor.{prefix}_pv3_power": _State(1700, "W"),
        f"sensor.{prefix}_today_s_pv_generation": _State(18.75, "kWh"),
        f"number.{prefix}_grid_export_limit": _State(5000, "W"),
        f"switch.{prefix}_grid_export_limit_switch": _State("off"),
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


def test_goodwe_ac_entity_controller_reports_pv_and_export_state():
    module, restore_module = _load_goodwe_entity_module()
    try:
        controller = module.GoodWeEntityInverterController(
            _Hass(_ac_state_map()), "goodwe"
        )

        state = asyncio.run(controller.get_status())

        assert state.status is module.InverterStatus.ONLINE
        assert state.power_output_w == 5200
        assert state.attributes["pv1_power"] == 1700
        assert state.attributes["pv2_power"] == 1800
        assert state.attributes["pv3_power"] == 1700
        assert state.attributes["daily_pv_generation"] == 18.75
        assert state.attributes["export_limit_w"] == 5000
        assert state.attributes["export_limit_enabled"] is False
    finally:
        restore_module()


def test_goodwe_ac_entity_controller_curtails_and_restores_exact_state():
    module, restore_module = _load_goodwe_entity_module()
    try:
        hass = _Hass(_ac_state_map())
        controller = module.GoodWeEntityInverterController(hass, "goodwe")

        assert asyncio.run(controller.curtail()) is True
        assert hass.states.get("number.goodwe_grid_export_limit").state == 0
        assert hass.states.get("switch.goodwe_grid_export_limit_switch").state == "on"

        state = asyncio.run(controller.get_status())
        assert state.is_curtailed is True

        assert asyncio.run(controller.restore()) is True
        assert hass.states.get("number.goodwe_grid_export_limit").state == 5000
        assert hass.states.get("switch.goodwe_grid_export_limit_switch").state == "off"
    finally:
        restore_module()


def test_goodwe_ac_entity_controller_rolls_back_partial_curtailment():
    module, restore_module = _load_goodwe_entity_module()
    try:
        hass = _Hass(_ac_state_map())
        hass.services.fail_service = ("switch", "turn_on")
        controller = module.GoodWeEntityInverterController(hass, "goodwe")

        assert asyncio.run(controller.curtail()) is False
        assert hass.states.get("number.goodwe_grid_export_limit").state == 5000
        assert hass.states.get("switch.goodwe_grid_export_limit_switch").state == "off"
        assert controller._snapshot is None
    finally:
        restore_module()


def test_goodwe_ac_entity_controller_fails_closed_when_required_entity_missing():
    module, restore_module = _load_goodwe_entity_module()
    try:
        states = _ac_state_map()
        states.pop("number.goodwe_grid_export_limit")
        hass = _Hass(states)
        controller = module.GoodWeEntityInverterController(hass, "goodwe")

        assert asyncio.run(controller.curtail()) is False
        assert hass.services.calls == []
    finally:
        restore_module()


def test_goodwe_ac_entity_controller_recovers_saved_state_after_restart():
    module, restore_module = _load_goodwe_entity_module()
    try:
        hass = _Hass(_ac_state_map())
        store = _Store()
        first = module.GoodWeEntityInverterController(hass, "goodwe")
        first._store = store

        assert asyncio.run(first.curtail()) is True
        assert store.data == {"export_limit": 5000.0, "switch_enabled": False}

        restarted = module.GoodWeEntityInverterController(hass, "goodwe")
        restarted._store = store
        assert asyncio.run(restarted.connect()) is True

        assert hass.states.get("number.goodwe_grid_export_limit").state == 5000
        assert hass.states.get("switch.goodwe_grid_export_limit_switch").state == "off"
        assert store.data == {}
    finally:
        restore_module()
