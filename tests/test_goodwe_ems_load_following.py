"""GoodWe EMS BuyPower 0 W load-following curtailment (direct controller)."""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

from test_goodwe_battery_controller import _load_goodwe_controller_module


class _Frame:
    def __init__(self, offset: int, values: bytes) -> None:
        self.offset = offset
        self.values = values


class _EmsInverter:
    """Models 47505/47509-47512 with GoodWe's 0x10 multi-register write."""

    def __init__(
        self,
        *,
        ems_mode: int = 1,
        ems_power: int = 16384,
        fallback: int | None = 10,
        accept_writes: bool = True,
    ) -> None:
        self.registers: dict[int, int] = {
            47509: 1,
            47510: 0,
            47511: ems_mode,
            47512: ems_power,
        }
        if fallback is not None:
            self.registers[47505] = fallback
        self.accept_writes = accept_writes
        self.frames: list[tuple[int, list[int]]] = []
        self.setting_writes: list[tuple[str, int]] = []

    _NAMES = {
        "grid_export": 47509,
        "grid_export_limit": 47510,
        "ems_mode": 47511,
        "ems_power_limit": 47512,
    }

    async def read_setting(self, setting_id: str) -> int:
        if setting_id.startswith("modbus_"):
            register = int(setting_id[7:])
        else:
            register = self._NAMES[setting_id]
        if register not in self.registers:
            raise ValueError(f"illegal address {register}")
        value = self.registers[register]
        # goodwe decodes Integer settings as signed 16-bit.
        return value - 0x10000 if value >= 0x8000 else value

    async def write_setting(self, setting_id: str, value: int) -> None:
        self.setting_writes.append((setting_id, value))
        if self.accept_writes:
            self.registers[self._NAMES[setting_id]] = value

    async def set_grid_export_limit(self, value: int) -> None:
        self.setting_writes.append(("grid_export_limit", value))
        self.registers[47510] = value

    def _write_multi_command(self, offset: int, values: bytes) -> _Frame:
        return _Frame(offset, values)

    async def _read_from_socket(self, frame: _Frame) -> None:
        words = [
            int.from_bytes(frame.values[i : i + 2], "big")
            for i in range(0, len(frame.values), 2)
        ]
        self.frames.append((frame.offset, words))
        if self.accept_writes:
            for index, word in enumerate(words):
                self.registers[frame.offset + index] = word


class _SingleWriteEmsInverter(_EmsInverter):
    """A library build without the private multi-register helpers."""

    _write_multi_command = None  # type: ignore[assignment]


@pytest.fixture
def goodwe_module():
    module, restore_module = _load_goodwe_controller_module()
    try:
        yield module
    finally:
        restore_module()


def _controller(module, inverter, method="ems_load_following"):
    controller = module.GoodWeBatteryController(
        "192.0.2.10", port=502, curtailment_method=method
    )
    controller._inverter = inverter

    async def connect() -> bool:
        return True

    controller.connect = connect
    return controller


def test_curtail_writes_buy_power_zero_in_one_documented_frame(goodwe_module):
    inverter = _EmsInverter()
    controller = _controller(goodwe_module, inverter)

    assert asyncio.run(controller.curtail()) is True

    assert inverter.frames == [(47511, [9, 0])]
    assert inverter.registers[47511] == 9
    assert inverter.registers[47512] == 0
    # The export limiter is not part of this method.
    assert inverter.setting_writes == []
    assert controller.get_grid_export_restore_state() == {
        "method": "ems_load_following",
        "ems_mode": 1,
        "ems_power_limit": 16384,
    }


def test_restore_returns_saved_ems_baseline(goodwe_module):
    inverter = _EmsInverter()
    controller = _controller(goodwe_module, inverter)

    assert asyncio.run(controller.curtail())
    assert asyncio.run(controller.curtail())  # re-apply keeps first baseline
    assert asyncio.run(controller.restore())

    assert inverter.frames[-1] == (47511, [1, 16384])
    assert inverter.registers[47511] == 1
    assert inverter.registers[47512] == 16384
    assert controller.get_grid_export_restore_state() is None
    assert inverter.registers[47509] == 1 and inverter.registers[47510] == 0


@pytest.mark.parametrize("fallback", [2, 3, None])
def test_curtail_refuses_unsafe_or_unreadable_fallback(goodwe_module, fallback):
    inverter = _EmsInverter(fallback=fallback)
    controller = _controller(goodwe_module, inverter)

    assert asyncio.run(controller.curtail()) is False
    assert inverter.frames == []
    assert controller.get_grid_export_restore_state() is None


def test_curtail_reports_unconfirmed_readback(goodwe_module):
    inverter = _EmsInverter(accept_writes=False)
    controller = _controller(goodwe_module, inverter)

    assert asyncio.run(controller.curtail()) is False
    assert inverter.frames == [(47511, [9, 0])]


@pytest.mark.parametrize("baseline_mode,baseline_power", [(0, 0), (9, 0), (255, 0)])
def test_unset_stopped_or_leftover_hold_baseline_restores_self_use(
    goodwe_module, baseline_mode, baseline_power
):
    inverter = _EmsInverter(ems_mode=baseline_mode, ems_power=baseline_power)
    controller = _controller(goodwe_module, inverter)

    assert asyncio.run(controller.curtail())
    assert asyncio.run(controller.restore())

    assert inverter.registers[47511] == 1


def test_restore_does_not_overwrite_a_foreign_ems_command(goodwe_module):
    inverter = _EmsInverter()
    controller = _controller(goodwe_module, inverter)
    assert asyncio.run(controller.curtail())

    # SEMS / an EMS entity command / a force mode replaced the hold.
    inverter.registers[47511] = 11
    inverter.registers[47512] = 3000

    assert asyncio.run(controller.restore()) is True
    assert inverter.frames == [(47511, [9, 0])]
    assert inverter.registers[47511] == 11
    assert controller.get_grid_export_restore_state() is None


def test_restart_snapshot_restores_ems_even_if_method_was_switched_back(goodwe_module):
    first = _EmsInverter(ems_power=2500)
    owner = _controller(goodwe_module, first)
    assert asyncio.run(owner.curtail())
    snapshot = owner.get_grid_export_restore_state()

    restarted = _controller(goodwe_module, first, method="export_limit")
    assert restarted.restore_grid_export_restore_state(snapshot)
    assert restarted.uses_ems_load_following is True
    assert asyncio.run(restarted.restore())

    assert first.registers[47511] == 1
    assert first.registers[47512] == 2500
    assert first.setting_writes == []
    assert restarted.uses_ems_load_following is False


def test_legacy_export_limit_snapshot_still_restores_export_limit(goodwe_module):
    inverter = _EmsInverter()
    controller = _controller(goodwe_module, inverter)
    assert controller.restore_grid_export_restore_state(
        {"grid_export": 0, "grid_export_limit": 5000}
    )
    assert controller.uses_ems_load_following is False
    assert asyncio.run(controller.restore())
    assert ("grid_export", 0) in inverter.setting_writes
    assert inverter.frames == []


def test_single_register_fallback_writes_set_point_before_mode(goodwe_module):
    inverter = _SingleWriteEmsInverter()
    controller = _controller(goodwe_module, inverter)

    assert asyncio.run(controller.curtail())
    assert inverter.setting_writes == [("ems_power_limit", 0), ("ems_mode", 9)]


def test_force_charge_releases_load_following_hold_first(goodwe_module):
    inverter = _EmsInverter()
    controller = _controller(goodwe_module, inverter)
    assert asyncio.run(controller.curtail())

    calls: list[str] = []

    async def apply(mode, **kwargs):
        calls.append(f"mode={int(mode)} ems={inverter.registers[47511]}")
        return True

    controller._apply_operation_mode = apply
    fake_goodwe = types.ModuleType("goodwe")

    class _OperationMode:
        ECO_CHARGE = 98
        ECO_DISCHARGE = 99

    fake_goodwe.OperationMode = _OperationMode
    saved = sys.modules.get("goodwe")
    sys.modules["goodwe"] = fake_goodwe
    try:
        assert asyncio.run(controller.force_charge())
    finally:
        if saved is None:
            sys.modules.pop("goodwe", None)
        else:
            sys.modules["goodwe"] = saved

    assert calls == ["mode=98 ems=1"]
    assert controller.get_grid_export_restore_state() is None


def test_default_method_keeps_existing_export_limit_path(goodwe_module):
    inverter = _EmsInverter()
    controller = goodwe_module.GoodWeBatteryController("192.0.2.10")
    controller._inverter = inverter

    async def connect() -> bool:
        return True

    controller.connect = connect
    assert controller.uses_ems_load_following is False
    assert asyncio.run(controller.curtail())
    assert inverter.frames == []
    assert ("grid_export", 1) in inverter.setting_writes


def test_unknown_method_falls_back_to_export_limit(goodwe_module):
    controller = goodwe_module.GoodWeBatteryController(
        "192.0.2.10", curtailment_method="off_grid"
    )
    assert controller.curtailment_method == "export_limit"
