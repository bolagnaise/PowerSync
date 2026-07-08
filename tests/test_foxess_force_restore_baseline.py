"""Regression tests for FoxESS force_charge/force_discharge baseline capture.

Bug OB-16: force_charge/force_discharge captured `_original_work_mode` (and
`_original_min_soc`) unconditionally on every call. The optimizer re-issues
force every cycle to keep the ~600s hardware timeout alive, so cycle 2 would
read back the *temporary* work mode (Feed-in for discharge / Backup for
charge) and clobber the real baseline. `restore_normal()` would then restore
the temporary mode, stranding H1/H3/KH inverters in Feed-in/Backup instead
of their pre-force mode.

Fix: mirror `set_backup_mode`'s `if self._original_work_mode is None` guard
in both force paths, and the equivalent guard for `_original_min_soc`.
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"


def _snapshot_modules() -> dict[str, types.ModuleType]:
    return {
        name: module
        for name, module in sys.modules.items()
        if name == "pymodbus"
        or name.startswith("pymodbus.")
        or name == "power_sync"
        or name.startswith("power_sync.")
    }


def _restore_modules(snapshot: dict[str, types.ModuleType]) -> None:
    for name in list(sys.modules):
        if (
            name == "pymodbus"
            or name.startswith("pymodbus.")
            or name == "power_sync"
            or name.startswith("power_sync.")
        ):
            sys.modules.pop(name, None)
    sys.modules.update(snapshot)


def _clear_test_modules() -> None:
    _restore_modules({})


def _install_power_sync_package() -> None:
    power_sync = types.ModuleType("power_sync")
    power_sync.__path__ = [str(COMPONENT_ROOT)]
    sys.modules["power_sync"] = power_sync

    inverters = types.ModuleType("power_sync.inverters")
    inverters.__path__ = [str(COMPONENT_ROOT / "inverters")]
    sys.modules["power_sync.inverters"] = inverters


def _write_fake_pymodbus(root: Path) -> None:
    package = root / "pymodbus"
    client = package / "client"
    framer = package / "framer"
    client.mkdir(parents=True)
    framer.mkdir(parents=True)

    (package / "__init__.py").write_text('__version__ = "3.10.0"\n', encoding="utf-8")
    (client / "__init__.py").write_text(
        "from pymodbus.client.tcp import AsyncModbusTcpClient\n"
        "from pymodbus.client.serial import AsyncModbusSerialClient\n",
        encoding="utf-8",
    )
    (framer / "__init__.py").write_text(
        "class FramerType:\n"
        "    SOCKET = 'socket'\n",
        encoding="utf-8",
    )
    (package / "exceptions.py").write_text(
        "class ModbusException(Exception):\n"
        "    pass\n",
        encoding="utf-8",
    )
    (client / "tcp.py").write_text(
        "from pymodbus.framer import FramerType\n"
        "\n"
        "class AsyncModbusTcpClient:\n"
        "    def __init__(self, **kwargs):\n"
        "        self.kwargs = kwargs\n"
        "\n"
        "    async def connect(self):\n"
        "        return True\n"
        "\n"
        "    def close(self):\n"
        "        pass\n"
        "\n"
        "    async def read_input_registers(self, address, count=1, slave=None):\n"
        "        return None\n",
        encoding="utf-8",
    )
    (client / "serial.py").write_text(
        "from pymodbus.framer import FramerType\n"
        "\n"
        "class AsyncModbusSerialClient:\n"
        "    def __init__(self, **kwargs):\n"
        "        self.kwargs = kwargs\n"
        "\n"
        "    async def connect(self):\n"
        "        return True\n"
        "\n"
        "    def close(self):\n"
        "        pass\n",
        encoding="utf-8",
    )


@pytest.fixture()
def foxess_module(tmp_path: Path):
    """Import a fresh power_sync.inverters.foxess module against a fake pymodbus."""
    snapshot = _snapshot_modules()
    original_path = list(sys.path)
    try:
        _clear_test_modules()
        _write_fake_pymodbus(tmp_path)
        sys.path.insert(0, str(tmp_path))
        _install_power_sync_package()
        module = importlib.import_module("power_sync.inverters.foxess")
        yield module
    finally:
        sys.path[:] = original_path
        _restore_modules(snapshot)


def _make_controller(module, holding: dict[int, int], model_family: str = "H3"):
    """Build a controller with holding-register reads/writes backed by `holding`."""
    controller = module.FoxESSController(
        host="192.0.2.1",
        model_family=model_family,
    )

    async def fake_read_holding(address: int, count: int = 1):
        if count == 1:
            return [holding.get(address, 0)]
        return [holding.get(address + i, 0) for i in range(count)]

    async def fake_write_holding(address: int, value: int):
        holding[address] = value
        return True

    async def fake_write_remote_control(*args, **kwargs):
        # Remote control verification (readback/retry/sleep) is out of scope
        # for this baseline-capture bug — stub it out so the test is fast
        # and isolated to the work_mode/min_soc snapshot logic.
        return True

    controller._read_holding_registers = fake_read_holding
    controller._write_holding_register = fake_write_holding
    controller._write_remote_control = fake_write_remote_control
    return controller


def test_force_discharge_does_not_reclobber_baseline_on_reissue(foxess_module):
    """Cycle 2 of a re-issued force_discharge must not overwrite the saved baseline."""
    reg = foxess_module.REGISTER_MAPS[foxess_module.FoxESSModelFamily.H3]
    holding = {
        reg.work_mode: reg.work_mode_self_use,  # inverter starts in Self Use
        reg.min_soc: 20,
    }
    controller = _make_controller(foxess_module, holding)

    # Cycle 1: optimizer issues force_discharge for the first time.
    result1 = asyncio.run(controller.force_discharge(duration_minutes=5, power_w=3000, min_timeout_seconds=60))
    assert result1 is True
    assert controller._original_work_mode == reg.work_mode_self_use
    assert holding[reg.work_mode] == reg.work_mode_feed_in

    # Cycle 2: optimizer re-issues force_discharge to keep the hardware
    # timeout alive. The register now reads back the TEMPORARY Feed-in mode.
    result2 = asyncio.run(controller.force_discharge(duration_minutes=5, power_w=3000, min_timeout_seconds=60))
    assert result2 is True

    # The baseline must still be the original Self Use mode, not the
    # temporary Feed-in mode read back on cycle 2.
    assert controller._original_work_mode == reg.work_mode_self_use

    # restore_normal must target Self Use, never the stale Feed-in reading.
    asyncio.run(controller.restore_normal())
    assert holding[reg.work_mode] == reg.work_mode_self_use


def test_force_charge_does_not_reclobber_baseline_on_reissue(foxess_module):
    """Same guard applies to force_charge (temporary mode is Backup)."""
    reg = foxess_module.REGISTER_MAPS[foxess_module.FoxESSModelFamily.H3]
    holding = {
        reg.work_mode: reg.work_mode_self_use,
        reg.min_soc: 20,
    }
    controller = _make_controller(foxess_module, holding)

    asyncio.run(controller.force_charge(duration_minutes=5, power_w=3000, min_timeout_seconds=60))
    assert controller._original_work_mode == reg.work_mode_self_use
    assert holding[reg.work_mode] == reg.work_mode_backup

    # Re-issue: register now reads the temporary Backup mode.
    asyncio.run(controller.force_charge(duration_minutes=5, power_w=3000, min_timeout_seconds=60))
    assert controller._original_work_mode == reg.work_mode_self_use

    asyncio.run(controller.restore_normal())
    assert holding[reg.work_mode] == reg.work_mode_self_use


def test_force_discharge_does_not_reclobber_min_soc_baseline_on_reissue(foxess_module):
    """The equivalent guard must apply to _original_min_soc as well."""
    reg = foxess_module.REGISTER_MAPS[foxess_module.FoxESSModelFamily.H3]
    holding = {
        reg.work_mode: reg.work_mode_self_use,
        reg.min_soc: 20,
    }
    controller = _make_controller(foxess_module, holding)

    asyncio.run(controller.force_discharge(duration_minutes=5, power_w=3000, min_timeout_seconds=60))
    assert controller._original_min_soc == 20

    # Simulate the min_soc register having a different value by the time the
    # optimizer re-issues the force call (e.g. any code path that touches it
    # mid-force). The saved baseline must not be re-captured from this.
    holding[reg.min_soc] = 100

    asyncio.run(controller.force_discharge(duration_minutes=5, power_w=3000, min_timeout_seconds=60))
    assert controller._original_min_soc == 20

    asyncio.run(controller.restore_normal())
    assert holding[reg.min_soc] == 20


def test_h3_pro_smart_skips_work_mode_change_but_still_guards_baseline(foxess_module):
    """H3-Pro/Smart intentionally skip the work-mode write during force, but the
    baseline read (used by restore_normal/set_backup_mode) must still be
    captured only once."""
    reg = foxess_module.REGISTER_MAPS[foxess_module.FoxESSModelFamily.H3_PRO]
    holding = {
        reg.work_mode: reg.work_mode_self_use,
        reg.min_soc: 30,
    }
    controller = _make_controller(foxess_module, holding, model_family="H3-Pro")

    asyncio.run(controller.force_discharge(duration_minutes=5, power_w=3000, min_timeout_seconds=60))
    assert controller._original_work_mode == reg.work_mode_self_use
    # H3-Pro/Smart must NOT change the work_mode register during force.
    assert holding[reg.work_mode] == reg.work_mode_self_use

    # Simulate something external changing the mode register between cycles
    # (e.g. a manual app change) to prove the guard, not the register write,
    # is what's under test here.
    holding[reg.work_mode] = reg.work_mode_feed_in

    asyncio.run(controller.force_discharge(duration_minutes=5, power_w=3000, min_timeout_seconds=60))
    assert controller._original_work_mode == reg.work_mode_self_use
