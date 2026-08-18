"""Regression tests for Fronius load-following capacity detection."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import types

import pytest


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


def _controller_with_control_readback(
    *,
    enabled: int,
    limit_value: int,
    ac_power: int = 2_078,
) -> tuple[FroniusController, list[tuple[int, int]]]:
    controller = FroniusController("192.0.2.1", load_following=True)
    writes: list[tuple[int, int]] = []

    async def connect() -> bool:
        return True

    async def write_register(address: int, value: int) -> bool:
        writes.append((address, value))
        return True

    async def read_register(address: int, count: int = 1):
        if address == controller.REG_WMAXLIM_ENA:
            return [enabled]
        if address == controller.REG_WMAXLIMPCT:
            return [limit_value]
        if address == controller.REG_WMAXLIMPCT_RVRT:
            return [0]
        if address == controller.REG_AC_POWER:
            assert count == 2
            return [ac_power, 0]
        if address == controller.REG_DC_POWER:
            return [0]
        if address == controller.REG_TEMPERATURE:
            return [250]
        if address == controller.REG_STATUS:
            return [controller.STATUS_MPPT]
        raise AssertionError(address)

    controller.connect = connect
    controller._write_register = write_register
    controller._read_register = read_register
    return controller, writes


@pytest.fixture(autouse=True)
def _skip_fronius_settle_delays(monkeypatch):
    async def no_sleep(_seconds: float) -> None:
        return None

    fronius_module = sys.modules["power_sync.inverters.fronius"]
    monkeypatch.setattr(fronius_module.asyncio, "sleep", no_sleep)


def test_load_following_requires_matching_limit_readback():
    controller, writes = _controller_with_control_readback(
        enabled=1,
        limit_value=5_000,
    )

    result = asyncio.run(
        controller.curtail(home_load_w=1_440, rated_capacity_w=5_000)
    )

    assert result is False
    assert writes == [
        (controller.REG_WMAXLIM_ENA, 0),
        (controller.REG_WMAXLIMPCT, 2_880),
        (controller.REG_WMAXLIMPCT_RVRT, 0),
        (controller.REG_WMAXLIM_ENA, 1),
    ]


def test_load_following_accepts_matching_enabled_limit_readback():
    controller, writes = _controller_with_control_readback(
        enabled=1,
        limit_value=2_880,
    )

    result = asyncio.run(
        controller.curtail(home_load_w=1_440, rated_capacity_w=5_000)
    )

    assert result is True
    assert writes[-1] == (controller.REG_WMAXLIM_ENA, 1)


def test_fronius_status_exposes_confirmed_limit_percentage():
    controller, _writes = _controller_with_control_readback(
        enabled=1,
        limit_value=2_830,
    )

    state = asyncio.run(controller.get_status())

    assert state.is_curtailed is True
    assert state.power_limit_percent == pytest.approx(28.3)
    assert state.power_output_w == pytest.approx(2_078)


def test_restore_requires_matching_unrestricted_limit_readback():
    controller, writes = _controller_with_control_readback(
        enabled=1,
        limit_value=2_830,
    )

    result = asyncio.run(controller.restore())

    assert result is False
    assert writes == [
        (controller.REG_WMAXLIM_ENA, 0),
        (controller.REG_WMAXLIMPCT, 10_000),
        (controller.REG_WMAXLIM_ENA, 1),
    ]


def test_restore_accepts_matching_unrestricted_limit_readback():
    controller, _writes = _controller_with_control_readback(
        enabled=1,
        limit_value=10_000,
    )

    assert asyncio.run(controller.restore()) is True
