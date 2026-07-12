"""Regression tests for Fronius load-following capacity detection."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import types


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"


def _install_package_stubs() -> None:
    pymodbus = types.ModuleType("pymodbus")
    pymodbus_client = types.ModuleType("pymodbus.client")
    pymodbus_exceptions = types.ModuleType("pymodbus.exceptions")

    class AsyncModbusTcpClient:
        pass

    class ModbusException(Exception):
        pass

    pymodbus_client.AsyncModbusTcpClient = AsyncModbusTcpClient
    pymodbus_exceptions.ModbusException = ModbusException
    sys.modules["pymodbus"] = pymodbus
    sys.modules["pymodbus.client"] = pymodbus_client
    sys.modules["pymodbus.exceptions"] = pymodbus_exceptions

    power_sync = types.ModuleType("power_sync")
    power_sync.__path__ = [str(COMPONENT_ROOT)]
    sys.modules["power_sync"] = power_sync

    inverters = types.ModuleType("power_sync.inverters")
    inverters.__path__ = [str(COMPONENT_ROOT / "inverters")]
    sys.modules["power_sync.inverters"] = inverters


_install_package_stubs()

from power_sync.inverters.fronius import FroniusController  # noqa: E402


def _model_registers(model: str) -> list[int]:
    raw = model.encode("ascii").ljust(32, b"\x00")
    return [int.from_bytes(raw[index : index + 2], "big") for index in range(0, 32, 2)]


def _controller_with_model(model: str) -> FroniusController:
    controller = FroniusController("192.0.2.1", load_following=True)

    async def connect() -> bool:
        return True

    async def read_register(address: int, count: int = 1):
        assert address == controller.REG_MODEL
        assert count == 16
        return _model_registers(model)

    controller.connect = connect
    controller._read_register = read_register
    return controller


def test_gen24_model_uses_power_rating_not_generation_number():
    cases = {
        "Primo GEN24 10.0 Plus": 10_000,
        "Symo GEN24 6.0 Plus": 6_000,
    }

    for model, expected_capacity in cases.items():
        controller = _controller_with_model(model)
        assert asyncio.run(controller.get_rated_capacity()) == expected_capacity


def test_legacy_model_capacity_parsing_is_preserved():
    controller = _controller_with_model("Symo 8.2-3-M")

    assert asyncio.run(controller.get_rated_capacity()) == 8_200
