"""Regression tests for Sigenergy Modbus dispatch controls."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"
_SENTINEL = object()
_STUB_MODULE_NAMES = (
    "power_sync",
    "power_sync.inverters",
    "pymodbus",
    "pymodbus.client",
    "pymodbus.exceptions",
)


@pytest.fixture()
def sigenergy_module():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    saved_modules = {
        name: sys.modules.get(name, _SENTINEL)
        for name in _STUB_MODULE_NAMES
    }
    for name in _STUB_MODULE_NAMES:
        sys.modules.pop(name, None)

    power_sync = types.ModuleType("power_sync")
    power_sync.__path__ = [str(COMPONENT_ROOT)]
    sys.modules["power_sync"] = power_sync

    inverters = types.ModuleType("power_sync.inverters")
    inverters.__path__ = [str(COMPONENT_ROOT / "inverters")]
    sys.modules["power_sync.inverters"] = inverters

    pymodbus = types.ModuleType("pymodbus")
    pymodbus.__version__ = "3.8.0"
    pymodbus_client = types.ModuleType("pymodbus.client")
    pymodbus_exceptions = types.ModuleType("pymodbus.exceptions")

    class _AsyncModbusTcpClient:
        connected = False

        def __init__(self, *args, **kwargs) -> None:
            pass

    pymodbus_client.AsyncModbusTcpClient = _AsyncModbusTcpClient
    pymodbus_exceptions.ModbusException = type("ModbusException", (Exception,), {})
    sys.modules["pymodbus"] = pymodbus
    sys.modules["pymodbus.client"] = pymodbus_client
    sys.modules["pymodbus.exceptions"] = pymodbus_exceptions

    try:
        yield importlib.import_module("power_sync.inverters.sigenergy")
    finally:
        sys.modules.pop("power_sync.inverters.sigenergy", None)
        sys.modules.pop("power_sync.inverters.base", None)
        for name in _STUB_MODULE_NAMES:
            if saved_modules[name] is _SENTINEL:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved_modules[name]
        loop.close()
        asyncio.set_event_loop(None)


def test_force_charge_applies_requested_charge_limit_before_mode(sigenergy_module):
    controller = sigenergy_module.SigenergyController(host="127.0.0.1")
    writes: list[tuple[int, list[int]]] = []

    async def connect():
        return True

    async def write(address, values, slave_id=None):
        writes.append((address, list(values)))
        return True

    controller.connect = connect
    controller._write_holding_registers = write

    assert asyncio.run(controller.force_charge(power_kw=1.0))

    assert writes == [
        (
            controller.REG_ESS_MAX_CHARGE_LIMIT,
            controller._from_unsigned32(1000),
        ),
        (controller.REG_REMOTE_EMS_ENABLE, [1]),
        (
            controller.REG_REMOTE_EMS_CONTROL_MODE,
            [controller.REMOTE_EMS_MODE_CHARGE_PV],
        ),
    ]


def test_force_charge_does_not_enter_charge_mode_when_limit_write_fails(sigenergy_module):
    controller = sigenergy_module.SigenergyController(host="127.0.0.1")
    writes: list[tuple[int, list[int]]] = []

    async def connect():
        return True

    async def write(address, values, slave_id=None):
        writes.append((address, list(values)))
        return False

    controller.connect = connect
    controller._write_holding_registers = write

    assert not asyncio.run(controller.force_charge(power_kw=1.0))
    assert writes == [
        (
            controller.REG_ESS_MAX_CHARGE_LIMIT,
            controller._from_unsigned32(1000),
        ),
    ]


def test_force_discharge_uses_pv_first_mode(sigenergy_module):
    controller = sigenergy_module.SigenergyController(host="127.0.0.1")
    writes: list[tuple[int, list[int]]] = []

    async def connect():
        return True

    async def write(address, values, slave_id=None):
        writes.append((address, list(values)))
        return True

    controller.connect = connect
    controller._write_holding_registers = write

    assert asyncio.run(controller.force_discharge(power_kw=5.0))

    assert writes == [
        (controller.REG_REMOTE_EMS_ENABLE, [1]),
        (
            controller.REG_REMOTE_EMS_CONTROL_MODE,
            [controller.REMOTE_EMS_MODE_DISCHARGE_PV],
        ),
        (
            controller.REG_GRID_EXPORT_LIMIT,
            controller._from_unsigned32(5000),
        ),
    ]


def test_force_charge_holds_shared_host_lock_until_all_writes_finish(sigenergy_module):
    async def run_test():
        first = sigenergy_module.SigenergyController(host="192.0.2.44")
        second = sigenergy_module.SigenergyController(host="192.0.2.44")
        events: list[tuple[str, int | bool | None]] = []
        pending_disconnect: asyncio.Task | None = None

        async def connect():
            return True

        class FakeClient:
            def close(self):
                events.append(("disconnect", None))

        second._client = FakeClient()
        second._connected = True

        async def write(address, values, slave_id=None):
            nonlocal pending_disconnect
            events.append(("write", address))
            if address == first.REG_ESS_MAX_CHARGE_LIMIT:
                pending_disconnect = asyncio.create_task(second.disconnect())
                await asyncio.sleep(0)
                events.append(("disconnect_done_early", pending_disconnect.done()))
            return True

        first.connect = connect
        first._write_holding_registers = write

        assert await first.force_charge(power_kw=1.0)
        assert pending_disconnect is not None
        await pending_disconnect
        return events

    events = asyncio.run(run_test())

    assert ("disconnect_done_early", False) in events
    write_indexes = [
        index for index, event in enumerate(events)
        if event[0] == "write"
    ]
    disconnect_index = events.index(("disconnect", None))
    assert disconnect_index > max(write_indexes)


def test_write_holding_registers_reconnects_once_after_not_connected(sigenergy_module):
    async def run_test():
        controller = sigenergy_module.SigenergyController(host="192.0.2.55")
        connects = 0

        class SuccessResult:
            def isError(self):
                return False

        class FakeClient:
            connected = True
            calls = 0

            async def write_registers(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    self.connected = False
                    raise RuntimeError("Cancel send, because not connected!")
                return SuccessResult()

            def close(self):
                self.connected = False

        client = FakeClient()
        controller._client = client

        async def connect():
            nonlocal connects
            connects += 1
            client.connected = True
            controller._client = client
            return True

        controller.connect = connect

        success = await controller._write_holding_registers(40029, [1])
        return success, client.calls, connects

    success, calls, connects = asyncio.run(run_test())

    assert success is True
    assert calls == 2
    assert connects == 1
