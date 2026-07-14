"""Regression tests for Sigenergy Modbus dispatch controls."""

from __future__ import annotations

import asyncio
import ast
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


def test_force_discharge_uses_pv_first_mode_when_solar_can_cover_target(sigenergy_module):
    controller = sigenergy_module.SigenergyController(host="127.0.0.1")
    writes: list[tuple[int, list[int]]] = []

    async def connect():
        return True

    async def get_status():
        return types.SimpleNamespace(attributes={"pv_power_kw": 5.2})

    async def write(address, values, slave_id=None):
        writes.append((address, list(values)))
        return True

    controller.connect = connect
    controller.get_status = get_status
    controller._write_holding_registers = write

    assert asyncio.run(controller.force_discharge(power_kw=5.0))

    assert writes == [
        (controller.REG_REMOTE_EMS_ENABLE, [1]),
        (
            controller.REG_REMOTE_EMS_CONTROL_MODE,
            [controller.REMOTE_EMS_MODE_DISCHARGE_PV],
        ),
        (
            controller.REG_ACTIVE_POWER_FIXED_TARGET,
            controller._from_signed32(-5000),
        ),
        (
            controller.REG_GRID_EXPORT_LIMIT,
            controller._from_unsigned32(5000),
        ),
    ]


def test_force_discharge_uses_ess_first_mode_when_target_needs_battery(sigenergy_module):
    controller = sigenergy_module.SigenergyController(host="127.0.0.1")
    writes: list[tuple[int, list[int]]] = []

    async def connect():
        return True

    async def get_status():
        return types.SimpleNamespace(
            attributes={
                "pv_power_kw": 2.0,
                "third_party_pv_power_kw": 0.5,
            }
        )

    async def write(address, values, slave_id=None):
        writes.append((address, list(values)))
        return True

    controller.connect = connect
    controller.get_status = get_status
    controller._write_holding_registers = write

    assert asyncio.run(controller.force_discharge(power_kw=5.0))

    assert writes == [
        (controller.REG_REMOTE_EMS_ENABLE, [1]),
        (
            controller.REG_REMOTE_EMS_CONTROL_MODE,
            [controller.REMOTE_EMS_MODE_DISCHARGE_ESS],
        ),
        (
            controller.REG_ACTIVE_POWER_FIXED_TARGET,
            controller._from_signed32(-5000),
        ),
        (
            controller.REG_GRID_EXPORT_LIMIT,
            controller._from_unsigned32(5000),
        ),
    ]


def test_force_discharge_leaves_ess_limit_unchanged_with_site_load(sigenergy_module):
    controller = sigenergy_module.SigenergyController(host="127.0.0.1")
    writes: list[tuple[int, list[int]]] = []

    async def connect():
        return True

    async def get_status():
        return types.SimpleNamespace(
            attributes={
                "pv_power_kw": 0.0,
                "grid_power_kw": -0.1,
                "battery_power_kw": 1.06,
            }
        )

    async def write(address, values, slave_id=None):
        writes.append((address, list(values)))
        return True

    controller.connect = connect
    controller.get_status = get_status
    controller._write_holding_registers = write

    assert asyncio.run(controller.force_discharge(power_kw=0.1))

    assert writes == [
        (controller.REG_REMOTE_EMS_ENABLE, [1]),
        (
            controller.REG_REMOTE_EMS_CONTROL_MODE,
            [controller.REMOTE_EMS_MODE_DISCHARGE_ESS],
        ),
        (
            controller.REG_ACTIVE_POWER_FIXED_TARGET,
            controller._from_signed32(-100),
        ),
        (
            controller.REG_GRID_EXPORT_LIMIT,
            controller._from_unsigned32(100),
        ),
    ]


def test_force_discharge_mode_selection_uses_configured_export_cap(sigenergy_module):
    controller = sigenergy_module.SigenergyController(
        host="127.0.0.1",
        max_export_limit_kw=5.0,
    )
    writes: list[tuple[int, list[int]]] = []

    async def connect():
        return True

    async def get_status():
        return types.SimpleNamespace(attributes={"pv_power_kw": 4.5})

    async def write(address, values, slave_id=None):
        writes.append((address, list(values)))
        return True

    controller.connect = connect
    controller.get_status = get_status
    controller._write_holding_registers = write

    assert asyncio.run(controller.force_discharge(power_kw=24.0))

    assert writes == [
        (controller.REG_REMOTE_EMS_ENABLE, [1]),
        (
            controller.REG_REMOTE_EMS_CONTROL_MODE,
            [controller.REMOTE_EMS_MODE_DISCHARGE_PV],
        ),
        (
            controller.REG_ACTIVE_POWER_FIXED_TARGET,
            controller._from_signed32(-5000),
        ),
        (
            controller.REG_GRID_EXPORT_LIMIT,
            controller._from_unsigned32(5000),
        ),
    ]


def test_force_discharge_continues_when_active_power_target_write_fails(sigenergy_module):
    controller = sigenergy_module.SigenergyController(host="127.0.0.1")
    writes: list[tuple[int, list[int]]] = []

    async def connect():
        return True

    async def get_status():
        return types.SimpleNamespace(attributes={"pv_power_kw": 0.0})

    async def write(address, values, slave_id=None):
        writes.append((address, list(values)))
        return address != controller.REG_ACTIVE_POWER_FIXED_TARGET

    controller.connect = connect
    controller.get_status = get_status
    controller._write_holding_registers = write

    assert asyncio.run(controller.force_discharge(power_kw=5.0))

    assert writes == [
        (controller.REG_REMOTE_EMS_ENABLE, [1]),
        (
            controller.REG_REMOTE_EMS_CONTROL_MODE,
            [controller.REMOTE_EMS_MODE_DISCHARGE_ESS],
        ),
        (
            controller.REG_ACTIVE_POWER_FIXED_TARGET,
            controller._from_signed32(-5000),
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


def test_restore_normal_keeps_remote_ems_for_powersync_control(sigenergy_module):
    controller = sigenergy_module.SigenergyController(host="127.0.0.1")
    controller._restore_backup_reserve_pct = 25
    events: list[tuple[str, int | str, list[int] | None]] = []

    async def connect():
        return True

    async def restore_export_limit():
        events.append(("restore_export_limit", "called", None))
        return True

    async def restore_ess_limits():
        events.append(("restore_ess_limits", "called", None))

    async def write(address, values, slave_id=None):
        events.append(("write", address, list(values)))
        return True

    async def set_backup_reserve(percent):
        events.append(("set_backup_reserve", percent, None))
        return True

    controller.connect = connect
    controller.restore_export_limit = restore_export_limit
    controller._restore_ess_max_limits_to_rated = restore_ess_limits
    controller._write_holding_registers = write
    controller.set_backup_reserve = set_backup_reserve

    assert asyncio.run(controller.restore_normal())

    assert events == [
        ("write", controller.REG_REMOTE_EMS_ENABLE, [1]),
        (
            "write",
            controller.REG_REMOTE_EMS_CONTROL_MODE,
            [controller.REMOTE_EMS_MODE_SELF_CONSUMPTION],
        ),
        ("restore_export_limit", "called", None),
        ("restore_ess_limits", "called", None),
        ("set_backup_reserve", 25, None),
    ]


def test_restore_normal_native_control_disables_remote_ems(sigenergy_module):
    controller = sigenergy_module.SigenergyController(host="127.0.0.1")
    controller._restore_backup_reserve_pct = 25
    events: list[tuple[str, int | str, list[int] | None]] = []

    async def connect():
        return True

    async def restore_export_limit():
        events.append(("restore_export_limit", "called", None))
        return True

    async def restore_ess_limits():
        events.append(("restore_ess_limits", "called", None))

    async def write(address, values, slave_id=None):
        events.append(("write", address, list(values)))
        return True

    async def set_backup_reserve(percent):
        events.append(("set_backup_reserve", percent, None))
        return True

    controller.connect = connect
    controller.restore_export_limit = restore_export_limit
    controller._restore_ess_max_limits_to_rated = restore_ess_limits
    controller._write_holding_registers = write
    controller.set_backup_reserve = set_backup_reserve

    assert asyncio.run(controller.restore_normal(native_control=True))

    assert events == [
        ("restore_export_limit", "called", None),
        ("restore_ess_limits", "called", None),
        ("write", controller.REG_REMOTE_EMS_ENABLE, [0]),
        ("set_backup_reserve", 25, None),
    ]


def test_curtail_does_not_capture_curtailed_zero_as_original(sigenergy_module):
    """HD-14 regression: a fresh controller (e.g. after a config reload) that
    calls curtail() while the export limit register already reads 0 (already
    curtailed) must not treat that 0 as the 'original' limit to restore to —
    doing so silently discards an inverter-side-only DNSP export cap."""
    controller = sigenergy_module.SigenergyController(host="127.0.0.1")
    assert controller._original_pv_limit is None

    async def connect():
        return True

    async def get_current_export_limit():
        # Simulates a fresh controller instance reading the register while
        # it is already curtailed to zero (e.g. after a reload mid-curtailment).
        return 0

    async def write(address, values, slave_id=None):
        return True

    controller.connect = connect
    controller._get_current_export_limit = get_current_export_limit
    controller._write_holding_registers = write

    assert asyncio.run(controller.curtail())

    assert controller._original_pv_limit is None


def test_restore_treats_stored_zero_as_valid_original_limit(sigenergy_module):
    """HD-14 regression: restore() must not use a falsy check on
    _original_pv_limit — a stored value of 0 is a legitimate captured limit
    distinct from 'no original captured' (None) and must not fall through to
    the safety-cap/unlimited fallback."""
    controller = sigenergy_module.SigenergyController(host="127.0.0.1")
    controller._original_pv_limit = 0
    writes: list[tuple[int, list[int]]] = []

    async def connect():
        return True

    async def write(address, values, slave_id=None):
        writes.append((address, list(values)))
        return True

    async def get_status():
        return types.SimpleNamespace(is_curtailed=False)

    async def fail_safety_cap():
        raise AssertionError(
            "safety cap fallback should not be used when an original limit is stored"
        )

    controller.connect = connect
    controller._write_holding_registers = write
    controller.get_status = get_status
    controller._get_effective_export_safety_cap_kw = fail_safety_cap

    assert asyncio.run(controller.restore())

    assert writes == [
        (controller.REG_GRID_EXPORT_LIMIT, controller._from_unsigned32(0)),
    ]


def test_restore_uses_stored_original_limit_not_safety_cap(sigenergy_module):
    """Regression guard: a legitimately captured original limit (e.g. a
    5 kW DNSP cap) must still be restored verbatim, not overridden by the
    safety-cap/unlimited fallback."""
    controller = sigenergy_module.SigenergyController(host="127.0.0.1")
    controller._original_pv_limit = 5000
    writes: list[tuple[int, list[int]]] = []

    async def connect():
        return True

    async def write(address, values, slave_id=None):
        writes.append((address, list(values)))
        return True

    async def get_status():
        return types.SimpleNamespace(is_curtailed=False)

    controller.connect = connect
    controller._write_holding_registers = write
    controller.get_status = get_status

    assert asyncio.run(controller.restore())

    assert writes == [
        (controller.REG_GRID_EXPORT_LIMIT, controller._from_unsigned32(5000)),
    ]
    assert controller._original_pv_limit is None


def test_configured_export_cap_replaces_active_curtailment_restore_baseline(
    sigenergy_module,
):
    """A mobile cap change during curtailment must become the restore target.

    The controller may already hold the pre-curtailment rated limit.  Keeping
    that stale snapshot would restore above the user's newly selected cap when
    the tariff leaves the curtailment window.
    """
    controller = sigenergy_module.SigenergyController(host="127.0.0.1")
    controller._original_pv_limit = 16800
    writes: list[tuple[int, list[int]]] = []

    async def connect():
        return True

    async def write(address, values, slave_id=None):
        writes.append((address, list(values)))
        return True

    async def get_status():
        return types.SimpleNamespace(is_curtailed=False)

    controller.connect = connect
    controller._write_holding_registers = write
    controller.get_status = get_status

    controller.set_configured_export_limit(5.0)

    assert controller._configured_max_export_limit_kw == 5.0
    assert controller._original_pv_limit == 5000
    assert asyncio.run(controller.restore())
    assert writes == [
        (controller.REG_GRID_EXPORT_LIMIT, controller._from_unsigned32(5000)),
    ]


def test_apply_configured_export_cap_keeps_active_curtailment_at_zero(
    sigenergy_module,
):
    controller = sigenergy_module.SigenergyController(host="127.0.0.1")
    controller._original_pv_limit = 16800
    writes: list[tuple[int, list[int]]] = []

    async def connect():
        return True

    async def write(address, values, slave_id=None):
        writes.append((address, list(values)))
        return True

    controller.connect = connect
    controller._write_holding_registers = write

    assert asyncio.run(
        controller.apply_configured_export_limit(5.0, curtailment_active=True)
    )
    assert writes == [
        (controller.REG_GRID_EXPORT_LIMIT, controller._from_unsigned32(0)),
    ]
    assert controller._configured_max_export_limit_kw == 5.0
    assert controller._original_pv_limit == 5000


def test_apply_active_export_cap_does_not_recapture_stale_live_limit(
    sigenergy_module,
):
    controller = sigenergy_module.SigenergyController(host="127.0.0.1")
    assert controller._original_pv_limit is None
    writes: list[tuple[int, list[int]]] = []

    async def connect():
        return True

    async def write(address, values, slave_id=None):
        writes.append((address, list(values)))
        return True

    async def get_current_export_limit():
        raise AssertionError("active cap update must not recapture the live register")

    async def get_status():
        return types.SimpleNamespace(is_curtailed=False)

    controller.connect = connect
    controller._write_holding_registers = write
    controller._get_current_export_limit = get_current_export_limit
    controller.get_status = get_status

    assert asyncio.run(
        controller.apply_configured_export_limit(5.0, curtailment_active=True)
    )
    assert controller._original_pv_limit == 5000
    assert asyncio.run(controller.restore())
    assert writes == [
        (controller.REG_GRID_EXPORT_LIMIT, controller._from_unsigned32(0)),
        (controller.REG_GRID_EXPORT_LIMIT, controller._from_unsigned32(5000)),
    ]


def test_apply_configured_export_cap_rolls_back_after_failed_write(
    sigenergy_module,
):
    controller = sigenergy_module.SigenergyController(
        host="127.0.0.1",
        max_export_limit_kw=16.8,
    )
    controller._original_pv_limit = 16800

    async def connect():
        return True

    async def write(address, values, slave_id=None):
        return False

    controller.connect = connect
    controller._write_holding_registers = write

    assert not asyncio.run(
        controller.apply_configured_export_limit(5.0, curtailment_active=False)
    )
    assert controller._configured_max_export_limit_kw == 16.8
    assert controller._original_pv_limit == 16800


def test_sigenergy_settings_post_persists_and_reoptimizes_export_cap():
    """The Controls API must update durable restore and planning state."""
    source = (COMPONENT_ROOT / "__init__.py").read_text()
    tree = ast.parse(source)
    view = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SigenergySettingsView"
    )
    post = next(
        node
        for node in view.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "post"
    )
    post_source = ast.get_source_segment(source, post)

    assert post_source is not None
    assert "controller.apply_configured_export_limit" in post_source
    assert "CONF_SIGENERGY_EXPORT_LIMIT_KW" in post_source
    assert 'entry_data["_skip_reload"] = True' in post_source
    assert "opt_coordinator.update_config(" in post_source
    assert "max_grid_export_w" in post_source
    assert "opt_coordinator.force_reoptimize()" in post_source
    assert post_source.index("if success:") < post_source.index(
        'entry_data["_skip_reload"] = True'
    )
