from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"


def _load_goodwe_controller_module():
    saved = {
        name: sys.modules.get(name)
        for name in (
            "power_sync",
            "power_sync.inverters",
            "power_sync.inverters.goodwe_battery",
        )
    }

    power_sync = types.ModuleType("power_sync")
    power_sync.__path__ = [str(COMPONENT_ROOT)]
    sys.modules["power_sync"] = power_sync

    inverters = types.ModuleType("power_sync.inverters")
    inverters.__path__ = [str(COMPONENT_ROOT / "inverters")]
    sys.modules["power_sync.inverters"] = inverters
    sys.modules.pop("power_sync.inverters.goodwe_battery", None)

    module = importlib.import_module("power_sync.inverters.goodwe_battery")

    def restore() -> None:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    return module, restore


class _FakeGoodWeInverter:
    def __init__(self) -> None:
        self.export_limits: list[int] = []
        self.settings: dict[str, int] = {
            "grid_export": 0,
            "grid_export_limit": 5000,
        }
        self.setting_writes: list[tuple[str, int]] = []

    async def set_grid_export_limit(self, value: int) -> None:
        if value > 65535:
            raise OverflowError("int too big to convert")
        self.export_limits.append(value)
        self.settings["grid_export_limit"] = value

    async def read_setting(self, setting_id: str) -> int:
        return self.settings[setting_id]

    async def write_setting(self, setting_id: str, value: int) -> None:
        self.setting_writes.append((setting_id, value))
        self.settings[setting_id] = value


def test_restore_uses_goodwe_export_limit_register_maximum():
    module, restore_module = _load_goodwe_controller_module()
    try:
        inverter = _FakeGoodWeInverter()
        controller = module.GoodWeBatteryController("192.0.2.10")
        controller._inverter = inverter

        async def connect() -> bool:
            return True

        controller.connect = connect

        assert asyncio.run(controller.restore())
        assert inverter.export_limits == [65535]
        assert inverter.setting_writes == [("grid_export", 0)]
    finally:
        restore_module()


def test_set_grid_export_limit_clamps_to_goodwe_register_range():
    module, restore_module = _load_goodwe_controller_module()
    try:
        inverter = _FakeGoodWeInverter()
        controller = module.GoodWeBatteryController("192.0.2.10")
        controller._inverter = inverter

        assert asyncio.run(controller.set_grid_export_limit(99999))
        assert inverter.export_limits == [65535]
    finally:
        restore_module()


def test_curtail_enables_goodwe_export_limit_before_setting_zero():
    module, restore_module = _load_goodwe_controller_module()
    try:
        inverter = _FakeGoodWeInverter()
        controller = module.GoodWeBatteryController("192.0.2.10")
        controller._inverter = inverter

        async def connect() -> bool:
            return True

        controller.connect = connect

        assert asyncio.run(controller.curtail())
        assert inverter.setting_writes == [("grid_export", 1)]
        assert inverter.export_limits == [0]
        assert controller._saved_grid_export_enabled == 0
        assert controller._saved_grid_export_limit == 5000
    finally:
        restore_module()


def test_restore_returns_goodwe_export_limit_to_saved_state():
    module, restore_module = _load_goodwe_controller_module()
    try:
        inverter = _FakeGoodWeInverter()
        controller = module.GoodWeBatteryController("192.0.2.10")
        controller._inverter = inverter

        async def connect() -> bool:
            return True

        controller.connect = connect

        assert asyncio.run(controller.curtail())
        assert asyncio.run(controller.restore())
        assert inverter.export_limits == [0, 5000]
        assert inverter.setting_writes == [("grid_export", 1), ("grid_export", 0)]
        assert controller._saved_grid_export_enabled is None
        assert controller._saved_grid_export_limit is None
        assert controller._grid_export_state_saved is False
    finally:
        restore_module()


def test_restore_for_export_command_does_not_keep_saved_zero_export_limit():
    module, restore_module = _load_goodwe_controller_module()
    try:
        inverter = _FakeGoodWeInverter()
        inverter.settings["grid_export"] = 1
        inverter.settings["grid_export_limit"] = 0
        controller = module.GoodWeBatteryController("192.0.2.10")
        controller._inverter = inverter

        async def connect() -> bool:
            return True

        controller.connect = connect

        assert asyncio.run(controller.curtail())
        assert asyncio.run(controller.restore(allow_zero_export_limit=False))
        assert inverter.export_limits == [0, 65535]
        assert inverter.setting_writes == [("grid_export", 1), ("grid_export", 0)]
        assert controller._saved_grid_export_enabled is None
        assert controller._saved_grid_export_limit is None
        assert controller._grid_export_state_saved is False
    finally:
        restore_module()
