from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"


def _load_anker_module():
    saved = {
        name: sys.modules.get(name)
        for name in (
            "homeassistant",
            "homeassistant.helpers",
            "homeassistant.helpers.entity_registry",
            "pymodbus",
            "pymodbus.client",
            "pymodbus.exceptions",
            "power_sync",
            "power_sync.inverters",
            "power_sync.inverters.anker_solix",
        )
    }

    ha = types.ModuleType("homeassistant")
    ha_helpers = types.ModuleType("homeassistant.helpers")
    ha_entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")
    ha_entity_registry.async_get = lambda hass: hass.entity_registry
    ha_entity_registry.async_entries_for_config_entry = (
        lambda registry, entry_id: registry.entries_by_entry_id.get(entry_id, [])
    )
    ha_helpers.entity_registry = ha_entity_registry
    ha.helpers = ha_helpers

    pymodbus = types.ModuleType("pymodbus")
    pymodbus.__version__ = "3.9.0"
    pymodbus_client = types.ModuleType("pymodbus.client")
    pymodbus_exceptions = types.ModuleType("pymodbus.exceptions")
    pymodbus_client.AsyncModbusTcpClient = _FakeModbusClient
    pymodbus_exceptions.ModbusException = type("ModbusException", (Exception,), {})

    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.helpers"] = ha_helpers
    sys.modules["homeassistant.helpers.entity_registry"] = ha_entity_registry
    sys.modules["pymodbus"] = pymodbus
    sys.modules["pymodbus.client"] = pymodbus_client
    sys.modules["pymodbus.exceptions"] = pymodbus_exceptions

    power_sync = types.ModuleType("power_sync")
    power_sync.__path__ = [str(COMPONENT_ROOT)]
    sys.modules["power_sync"] = power_sync

    inverters = types.ModuleType("power_sync.inverters")
    inverters.__path__ = [str(COMPONENT_ROOT / "inverters")]
    sys.modules["power_sync.inverters"] = inverters
    sys.modules.pop("power_sync.inverters.anker_solix", None)

    module = importlib.import_module("power_sync.inverters.anker_solix")

    def restore() -> None:
        for name, module_obj in saved.items():
            if module_obj is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module_obj

    return module, restore


class _FakeResult:
    def __init__(self, registers=None, error=False):
        self.registers = registers or []
        self._error = error

    def isError(self) -> bool:
        return self._error


class _FakeModbusClient:
    writes: list[dict] = []
    input_registers: dict[int, int] = {}
    holding_registers: dict[int, int] = {}

    def __init__(self, *_args, **_kwargs) -> None:
        self.connected = False

    async def connect(self) -> bool:
        self.connected = True
        return True

    def close(self) -> None:
        self.connected = False

    async def read_input_registers(self, **kwargs):
        address = kwargs["address"]
        count = kwargs["count"]
        return _FakeResult(
            [
                self.input_registers.get(address + offset, 0)
                for offset in range(count)
            ]
        )

    async def read_holding_registers(self, **kwargs):
        address = kwargs["address"]
        count = kwargs["count"]
        return _FakeResult(
            [
                self.holding_registers.get(address + offset, 0)
                for offset in range(count)
            ]
        )

    async def write_registers(self, **kwargs):
        self.writes.append(kwargs)
        return _FakeResult()


class _RegistryEntry:
    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id


class _State:
    def __init__(self, entity_id: str, state: str) -> None:
        self.entity_id = entity_id
        self.state = state


class _States:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, entity_id: str | None):
        if entity_id is None or entity_id not in self._values:
            return None
        return _State(entity_id, self._values[entity_id])

    def async_all(self):
        return [_State(entity_id, state) for entity_id, state in self._values.items()]


class _Services:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    async def async_call(self, domain: str, service: str, data: dict, blocking: bool = True):
        self.calls.append((domain, service, data))


class _Hass:
    def __init__(self, states: dict[str, str], entry_id: str = "entry-1") -> None:
        self.states = _States(states)
        self.services = _Services()
        self.entity_registry = types.SimpleNamespace(
            entries_by_entry_id={
                entry_id: [_RegistryEntry(entity_id) for entity_id in states]
            }
        )


def test_anker_modbus_force_charge_writes_third_party_mode_and_negative_power():
    module, restore_module = _load_anker_module()
    try:
        _FakeModbusClient.writes = []
        controller = module.AnkerSolixX1ModbusController("192.0.2.50", max_charge_kw=5.0)

        result = asyncio.run(controller.force_charge(30, 2500))

        assert result is True
        assert _FakeModbusClient.writes == [
            {"address": controller.REG_OPERATING_MODE, "values": [3], "device_id": 1},
            {
                "address": controller.REG_BATTERY_POWER_SETPOINT,
                "values": [0xFFFF, 0xF63C],
                "device_id": 1,
            },
        ]
    finally:
        restore_module()


def test_anker_modbus_status_decodes_signed_power_and_capacity_scaling():
    module, restore_module = _load_anker_module()
    try:
        controller = module.AnkerSolixX1ModbusController("192.0.2.50")
        _FakeModbusClient.input_registers = {
            controller.REG_PV_POWER: 0,
            controller.REG_PV_POWER + 1: 3200,
            controller.REG_THIRD_PARTY_PV_POWER: 0,
            controller.REG_THIRD_PARTY_PV_POWER + 1: 100,
            controller.REG_BATTERY_POWER: 0xFFFF,
            controller.REG_BATTERY_POWER + 1: 0xF830,
            controller.REG_LOAD_POWER: 0,
            controller.REG_LOAD_POWER + 1: 2100,
            controller.REG_GRID_POWER: 0,
            controller.REG_GRID_POWER + 1: 800,
            controller.REG_BATTERY_SOC: 63,
            controller.REG_RATED_ENERGY: 0,
            controller.REG_RATED_ENERGY + 1: 100,
            controller.REG_MAX_CHARGE_POWER: 0,
            controller.REG_MAX_CHARGE_POWER + 1: 5000,
            controller.REG_MAX_DISCHARGE_POWER: 0,
            controller.REG_MAX_DISCHARGE_POWER + 1: 5000,
        }
        _FakeModbusClient.holding_registers = {controller.REG_OPERATING_MODE: 0}

        status = asyncio.run(controller.get_status())

        assert status["solar_power"] == 3.3
        assert status["battery_power"] == -2.0
        assert status["grid_power"] == 0.8
        assert status["battery_level"] == 63.0
        assert status["battery_capacity_kwh"] == 10.0
    finally:
        restore_module()


def test_official_ha_bridge_discovers_controls_and_dispatches_discharge():
    module, restore_module = _load_anker_module()
    try:
        hass = _Hass(
            {
                "sensor.x1_battery_soc": "72",
                "sensor.x1_pv_power": "4100",
                "number.x1_battery_power_setpoint": "0",
                "select.x1_battery_power_direction": "charge",
                "select.x1_operating_mode": "self_consumption",
            }
        )
        controller = module.AnkerSolixEntityController(
            hass,
            integration_domain="anker_solix_official",
            config_entry_id="entry-1",
        )

        assert asyncio.run(controller.connect()) is True
        assert controller.is_dispatch_supported() is True
        assert asyncio.run(controller.force_discharge(30, 1800)) is True

        assert hass.services.calls == [
            ("select", "select_option", {"entity_id": "select.x1_operating_mode", "option": "third_party_control"}),
            ("select", "select_option", {"entity_id": "select.x1_battery_power_direction", "option": "discharge"}),
            ("number", "set_value", {"entity_id": "number.x1_battery_power_setpoint", "value": 1800.0}),
        ]
    finally:
        restore_module()


def test_cloud_bridge_can_be_telemetry_only_when_write_entities_are_missing():
    module, restore_module = _load_anker_module()
    try:
        hass = _Hass(
            {
                "sensor.solarbank_state_of_charge": "55",
                "sensor.solarbank_input_power": "1200",
                "sensor.solarbank_output_power": "300",
            }
        )
        controller = module.AnkerSolixEntityController(
            hass,
            integration_domain="anker_solix",
            config_entry_id="entry-1",
        )

        assert asyncio.run(controller.connect()) is True
        assert controller.is_dispatch_supported() is False
        status = controller.get_status()
        assert status["battery_level"] == 55.0
        assert status["solar_power"] == 1.2
        assert status["battery_power"] == 0.3
        assert asyncio.run(controller.force_discharge(30, 1000)) is False
    finally:
        restore_module()
