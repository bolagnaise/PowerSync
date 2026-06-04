from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"


def _load_goodwe_module():
    saved = {
        name: sys.modules.get(name)
        for name in (
            "pymodbus",
            "pymodbus.client",
            "pymodbus.exceptions",
            "power_sync",
            "power_sync.inverters",
            "power_sync.inverters.goodwe",
        )
    }

    pymodbus = types.ModuleType("pymodbus")
    pymodbus.__version__ = "3.9.0"
    pymodbus_client = types.ModuleType("pymodbus.client")
    pymodbus_exceptions = types.ModuleType("pymodbus.exceptions")

    class _AsyncModbusTcpClient:
        def __init__(self, *_args, **_kwargs) -> None:
            self.connected = False

        async def connect(self) -> bool:
            self.connected = True
            return True

        def close(self) -> None:
            self.connected = False

        async def read_holding_registers(self, **_kwargs):
            return _FakeResult()

        async def write_register(self, **_kwargs):
            return _FakeResult()

    pymodbus_client.AsyncModbusTcpClient = _AsyncModbusTcpClient
    pymodbus_exceptions.ModbusException = type("ModbusException", (Exception,), {})

    sys.modules["pymodbus"] = pymodbus
    sys.modules["pymodbus.client"] = pymodbus_client
    sys.modules["pymodbus.exceptions"] = pymodbus_exceptions

    power_sync = types.ModuleType("power_sync")
    power_sync.__path__ = [str(COMPONENT_ROOT)]
    sys.modules["power_sync"] = power_sync

    inverters = types.ModuleType("power_sync.inverters")
    inverters.__path__ = [str(COMPONENT_ROOT / "inverters")]
    sys.modules["power_sync.inverters"] = inverters
    sys.modules.pop("power_sync.inverters.goodwe", None)

    module = importlib.import_module("power_sync.inverters.goodwe")

    def restore() -> None:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    return module, restore


class _FakeResult:
    registers = [1, 2]

    def isError(self) -> bool:
        return False


class _ConcurrentTrackingClient:
    def __init__(self, tracker: dict[str, int]) -> None:
        self.connected = False
        self.tracker = tracker

    async def connect(self) -> bool:
        self.connected = True
        return True

    def close(self) -> None:
        self.connected = False

    async def read_holding_registers(self, **_kwargs):
        await self._track_request()
        return _FakeResult()

    async def write_register(self, **_kwargs):
        await self._track_request()
        return _FakeResult()

    async def _track_request(self) -> None:
        self.tracker["active_requests"] += 1
        self.tracker["max_active_requests"] = max(
            self.tracker["max_active_requests"],
            self.tracker["active_requests"],
        )
        await asyncio.sleep(0.01)
        self.tracker["active_requests"] -= 1


class _RegisterBlockResult:
    def __init__(self, registers: list[int]) -> None:
        self.registers = registers

    def isError(self) -> bool:
        return False


class _MappedRegisterClient:
    def __init__(self, values: dict[int, int], calls: list[tuple[int, int]]) -> None:
        self.connected = False
        self.values = values
        self.calls = calls

    async def connect(self) -> bool:
        self.connected = True
        return True

    def close(self) -> None:
        self.connected = False

    async def read_holding_registers(self, **kwargs):
        address = kwargs["address"]
        count = kwargs["count"]
        self.calls.append((address, count))
        return _RegisterBlockResult(
            [
                self.values.get(address + offset, 0)
                for offset in range(count)
            ]
        )


def test_goodwe_modbus_reads_are_serialized_on_one_client():
    module, restore_module = _load_goodwe_module()
    try:
        controller = module.GoodWeController("192.0.2.10")
        tracker = {"active_requests": 0, "max_active_requests": 0}
        module.AsyncModbusTcpClient = (
            lambda *_args, **_kwargs: _ConcurrentTrackingClient(tracker)
        )

        async def run_reads() -> None:
            await asyncio.gather(
                controller._read_register(35103, 1),
                controller._read_register(35104, 1),
            )

        asyncio.run(run_reads())

        assert tracker["max_active_requests"] == 1
    finally:
        restore_module()


def test_goodwe_status_reads_telemetry_in_blocks():
    module, restore_module = _load_goodwe_module()
    try:
        controller = module.GoodWeController("192.0.2.10")
        values = {
            controller.REG_PV1_VOLTAGE: 2301,
            controller.REG_PV1_CURRENT: 52,
            controller.REG_PV1_POWER: 0,
            controller.REG_PV1_POWER + 1: 1200,
            controller.REG_PV2_VOLTAGE: 2297,
            controller.REG_PV2_CURRENT: 48,
            controller.REG_PV2_POWER: 0,
            controller.REG_PV2_POWER + 1: 1100,
            controller.REG_DAILY_PV: 72,
            controller.REG_GRID_POWER: 0xFFF6,
            controller.REG_DAILY_EXPORT: 12,
            controller.REG_DAILY_IMPORT: 5,
            controller.REG_TEMP_AIR: 312,
            controller.REG_BATTERY_VOLTAGE: 512,
            controller.REG_BATTERY_CURRENT: 0xFFF0,
            controller.REG_BATTERY_POWER: 0xFFFF,
            controller.REG_BATTERY_POWER + 1: 0xF830,
            controller.REG_BATTERY_SOC: 83,
            controller.REG_EXPORT_LIMIT_ENABLED: 1,
            controller.REG_EXPORT_LIMIT: 0,
        }
        calls: list[tuple[int, int]] = []
        clients: list[_MappedRegisterClient] = []

        def make_client(*_args, **_kwargs):
            client = _MappedRegisterClient(values, calls)
            clients.append(client)
            return client

        module.AsyncModbusTcpClient = make_client

        attrs = asyncio.run(controller._read_all_registers())

        assert calls == [
            (controller.REG_PV1_VOLTAGE, 16),
            (controller.REG_GRID_POWER, 20),
            (controller.REG_TEMP_AIR, 10),
            (controller.REG_BATTERY_SOC, 1),
            (controller.REG_EXPORT_LIMIT_ENABLED, 2),
        ]
        assert len(clients) == 5
        assert all(not client.connected for client in clients)
        assert attrs["pv1_voltage"] == 230.1
        assert attrs["pv1_current"] == 5.2
        assert attrs["pv1_power"] == 1200
        assert attrs["pv2_voltage"] == 229.7
        assert attrs["pv2_current"] == 4.8
        assert attrs["pv2_power"] == 1100
        assert attrs["daily_pv_generation"] == 7.2
        assert attrs["grid_power"] == -10
        assert attrs["daily_export"] == 1.2
        assert attrs["daily_import"] == 0.5
        assert attrs["inverter_temperature"] == 31.2
        assert attrs["battery_voltage"] == 51.2
        assert attrs["battery_current"] == -1.6
        assert attrs["battery_power"] == -2000
        assert attrs["battery_level"] == 83
        assert attrs["export_limit_enabled"] is True
        assert attrs["export_limit_w"] == 0
    finally:
        restore_module()


def test_goodwe_modbus_reads_and_writes_share_the_same_request_lock():
    module, restore_module = _load_goodwe_module()
    try:
        controller = module.GoodWeController("192.0.2.10")
        tracker = {"active_requests": 0, "max_active_requests": 0}
        module.AsyncModbusTcpClient = (
            lambda *_args, **_kwargs: _ConcurrentTrackingClient(tracker)
        )

        async def run_requests() -> None:
            await asyncio.gather(
                controller._read_register(35103, 1),
                controller._write_register(47550, 0),
            )

        asyncio.run(run_requests())

        assert tracker["max_active_requests"] == 1
    finally:
        restore_module()
