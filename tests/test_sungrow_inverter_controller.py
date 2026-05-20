"""Regression tests for Sungrow SG inverter Modbus controller behavior."""

from __future__ import annotations

import asyncio
import importlib.machinery
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INVERTERS_DIR = ROOT / "custom_components" / "power_sync" / "inverters"


def _load_inverter_module(module_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(
        f"custom_components.power_sync.inverters.{module_name}",
        INVERTERS_DIR / filename,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


sys.modules["custom_components"] = types.ModuleType("custom_components")
sys.modules["custom_components.power_sync"] = types.ModuleType(
    "custom_components.power_sync"
)
sys.modules["custom_components.power_sync.inverters"] = types.ModuleType(
    "custom_components.power_sync.inverters"
)
pymodbus = types.ModuleType("pymodbus")
pymodbus.__spec__ = importlib.machinery.ModuleSpec("pymodbus", loader=None)
pymodbus.__version__ = "3.9.0"
pymodbus_client = types.ModuleType("pymodbus.client")
pymodbus_client.__spec__ = importlib.machinery.ModuleSpec(
    "pymodbus.client",
    loader=None,
)
pymodbus_client.AsyncModbusTcpClient = object
pymodbus_exceptions = types.ModuleType("pymodbus.exceptions")
pymodbus_exceptions.__spec__ = importlib.machinery.ModuleSpec(
    "pymodbus.exceptions",
    loader=None,
)
pymodbus_exceptions.ModbusException = Exception
sys.modules["pymodbus"] = pymodbus
sys.modules["pymodbus.client"] = pymodbus_client
sys.modules["pymodbus.exceptions"] = pymodbus_exceptions

_load_inverter_module("base", "base.py")
SungrowController = _load_inverter_module("sungrow", "sungrow.py").SungrowController
SungrowSHController = _load_inverter_module("sungrow_sh", "sungrow_sh.py").SungrowSHController


class _FakeResult:
    def isError(self) -> bool:
        return False


class _ConcurrentRequestTracker:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    async def run(self) -> _FakeResult:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        return _FakeResult()


class _FakeClient:
    connected = True

    def __init__(self, tracker: _ConcurrentRequestTracker) -> None:
        self._tracker = tracker

    async def write_register(self, **_kwargs) -> _FakeResult:
        return await self._tracker.run()


def test_sungrow_modbus_requests_are_serialized_per_endpoint():
    async def _run() -> None:
        SungrowController._endpoint_request_locks.clear()
        tracker = _ConcurrentRequestTracker()

        controller_a = SungrowController("192.0.2.10", port=502, slave_id=1)
        controller_b = SungrowController("192.0.2.10", port=502, slave_id=1)
        controller_a._client = _FakeClient(tracker)
        controller_b._client = _FakeClient(tracker)

        results = await asyncio.gather(
            controller_a._write_register(5005, controller_a.RUN_MODE_ENABLED),
            controller_b._write_register(5005, controller_b.RUN_MODE_SHUTDOWN),
        )

        assert results == [True, True]
        assert tracker.max_active == 1

    asyncio.run(_run())


def test_sungrow_sg_and_sh_requests_share_endpoint_lock():
    async def _run() -> None:
        SungrowController._endpoint_request_locks.clear()
        tracker = _ConcurrentRequestTracker()

        sg_controller = SungrowController("192.0.2.10", port=502, slave_id=1)
        sh_controller = SungrowSHController("192.0.2.10", port=502, slave_id=1)
        sg_controller._client = _FakeClient(tracker)
        sh_controller._client = _FakeClient(tracker)

        results = await asyncio.gather(
            sg_controller._write_register(5005, sg_controller.RUN_MODE_ENABLED),
            sh_controller._write_register(13050, sh_controller.CMD_STOP),
        )

        assert results == [True, True]
        assert tracker.max_active == 1

    asyncio.run(_run())
