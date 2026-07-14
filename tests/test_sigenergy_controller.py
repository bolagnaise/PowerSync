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


def test_force_charge_does_not_exceed_configured_charge_cap(sigenergy_module):
    controller = sigenergy_module.SigenergyController(
        host="127.0.0.1",
        configured_charge_rate_limit_kw=5.0,
    )
    writes: list[tuple[int, list[int]]] = []

    async def connect():
        return True

    async def write(address, values, slave_id=None):
        writes.append((address, list(values)))
        return True

    controller.connect = connect
    controller._write_holding_registers = write

    assert asyncio.run(controller.force_charge(power_kw=10.0))
    assert writes[0] == (
        controller.REG_ESS_MAX_CHARGE_LIMIT,
        controller._from_unsigned32(5000),
    )


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


def test_force_discharge_does_not_exceed_configured_discharge_cap(sigenergy_module):
    controller = sigenergy_module.SigenergyController(
        host="127.0.0.1",
        configured_discharge_rate_limit_kw=3.0,
    )
    writes: list[tuple[int, list[int]]] = []

    async def connect():
        return True

    async def get_status():
        return types.SimpleNamespace(attributes={"pv_power_kw": 5.0})

    async def write(address, values, slave_id=None):
        writes.append((address, list(values)))
        return True

    controller.connect = connect
    controller.get_status = get_status
    controller._write_holding_registers = write

    assert asyncio.run(controller.force_discharge(power_kw=10.0))
    assert writes[-2:] == [
        (
            controller.REG_ACTIVE_POWER_FIXED_TARGET,
            controller._from_signed32(-3000),
        ),
        (
            controller.REG_GRID_EXPORT_LIMIT,
            controller._from_unsigned32(3000),
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

    async def restore_ess_limits(*, use_configured_caps=True):
        events.append(("restore_ess_limits", "called", use_configured_caps))
        return True

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
        ("restore_ess_limits", "called", True),
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

    async def restore_ess_limits(*, use_configured_caps=True):
        events.append(("restore_ess_limits", "called", use_configured_caps))
        return True

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
        ("restore_ess_limits", "called", False),
        ("set_backup_reserve", 25, None),
        ("write", controller.REG_REMOTE_EMS_ENABLE, [0]),
    ]


@pytest.mark.parametrize(
    (
        "configured_charge",
        "configured_discharge",
        "native_control",
        "expected_charge",
        "expected_discharge",
    ),
    [
        (10.0, 10.0, False, 10000, 10000),
        (0.0, 0.0, False, 0, 0),
        (None, None, False, 16800, 19200),
        (10.0, 10.0, True, 16800, 19200),
    ],
)
def test_restore_normal_uses_configured_or_rated_ess_limits(
    sigenergy_module,
    configured_charge,
    configured_discharge,
    native_control,
    expected_charge,
    expected_discharge,
):
    """Configured caps survive PowerSync restores but not native handoff."""
    controller = sigenergy_module.SigenergyController(
        host="127.0.0.1",
        configured_charge_rate_limit_kw=configured_charge,
        configured_discharge_rate_limit_kw=configured_discharge,
    )
    writes: list[tuple[int, list[int]]] = []

    async def connect():
        return True

    async def set_self_consumption_mode():
        return True

    async def restore_export_limit():
        return True

    async def disable_remote_ems():
        return True

    async def read_input(address, count, slave_id=None):
        values = {
            controller.REG_ESS_RATED_CHARGE_POWER: 16800,
            controller.REG_ESS_RATED_DISCHARGE_POWER: 19200,
        }
        return controller._from_unsigned32(values[address])

    async def write(address, values, slave_id=None):
        writes.append((address, list(values)))
        return True

    controller.connect = connect
    controller.set_self_consumption_mode = set_self_consumption_mode
    controller.restore_export_limit = restore_export_limit
    controller.disable_remote_ems = disable_remote_ems
    controller._read_input_registers = read_input
    controller._write_holding_registers = write

    assert asyncio.run(controller.restore_normal(native_control=native_control))
    assert writes == [
        (
            controller.REG_ESS_MAX_CHARGE_LIMIT,
            controller._from_unsigned32(expected_charge),
        ),
        (
            controller.REG_ESS_MAX_DISCHARGE_LIMIT,
            controller._from_unsigned32(expected_discharge),
        ),
    ]


@pytest.mark.parametrize("failure_mode", ["false", "exception", "missing_rated"])
def test_restore_normal_reports_incomplete_ess_cleanup_and_keeps_remote_ems(
    sigenergy_module,
    failure_mode,
):
    """Native handoff must not hide a stranded PowerSync hardware limit."""
    controller = sigenergy_module.SigenergyController(host="127.0.0.1")
    disabled_remote_ems = False

    async def connect():
        return True

    async def restore_export_limit():
        return True

    async def read_input(address, count, slave_id=None):
        if failure_mode == "missing_rated" and address == controller.REG_ESS_RATED_CHARGE_POWER:
            return None
        rated = {
            controller.REG_ESS_RATED_CHARGE_POWER: 16800,
            controller.REG_ESS_RATED_DISCHARGE_POWER: 19200,
        }
        return controller._from_unsigned32(rated[address])

    async def write(address, values, slave_id=None):
        if address == controller.REG_ESS_MAX_DISCHARGE_LIMIT:
            if failure_mode == "false":
                return False
            if failure_mode == "exception":
                raise RuntimeError("write failed")
        return True

    async def disable_remote_ems():
        nonlocal disabled_remote_ems
        disabled_remote_ems = True
        return True

    controller.connect = connect
    controller.restore_export_limit = restore_export_limit
    controller._read_input_registers = read_input
    controller._write_holding_registers = write
    controller.disable_remote_ems = disable_remote_ems

    assert not asyncio.run(controller.restore_normal(native_control=True))
    assert disabled_remote_ems is False


def test_restore_normal_reserve_failure_prevents_native_handoff(sigenergy_module):
    controller = sigenergy_module.SigenergyController(
        host="127.0.0.1",
        configured_charge_rate_limit_kw=10.0,
        configured_discharge_rate_limit_kw=10.0,
    )
    controller._restore_backup_reserve_pct = 20
    disabled_remote_ems = False

    async def connect():
        return True

    async def restore_export_limit():
        return True

    async def read_input(address, count, slave_id=None):
        rated = {
            controller.REG_ESS_RATED_CHARGE_POWER: 16800,
            controller.REG_ESS_RATED_DISCHARGE_POWER: 19200,
        }
        return controller._from_unsigned32(rated[address])

    async def write(address, values, slave_id=None):
        return True

    async def set_backup_reserve(percent):
        return False

    async def disable_remote_ems():
        nonlocal disabled_remote_ems
        disabled_remote_ems = True
        return True

    controller.connect = connect
    controller.restore_export_limit = restore_export_limit
    controller._read_input_registers = read_input
    controller._write_holding_registers = write
    controller.set_backup_reserve = set_backup_reserve
    controller.disable_remote_ems = disable_remote_ems

    assert not asyncio.run(controller.restore_normal(native_control=True))
    assert disabled_remote_ems is False


def test_configured_rate_target_changes_only_after_successful_write(sigenergy_module):
    controller = sigenergy_module.SigenergyController(
        host="127.0.0.1",
        configured_charge_rate_limit_kw=16.8,
        configured_discharge_rate_limit_kw=19.2,
    )

    async def charge_success(limit_kw):
        return True

    async def discharge_failure(limit_kw):
        return False

    controller.set_charge_rate_limit = charge_success
    controller.set_discharge_rate_limit = discharge_failure

    assert asyncio.run(controller.apply_configured_charge_rate_limit(10.0))
    assert not asyncio.run(controller.apply_configured_discharge_rate_limit(10.0))
    assert controller._configured_charge_rate_limit_kw == 10.0
    assert controller._configured_discharge_rate_limit_kw == 19.2


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
    assert "controller.apply_configured_charge_rate_limit" in post_source
    assert "controller.apply_configured_discharge_rate_limit" in post_source
    assert "CONF_SIGENERGY_CHARGE_RATE_LIMIT_KW" in post_source
    assert "CONF_SIGENERGY_DISCHARGE_RATE_LIMIT_KW" in post_source
    assert "controller.apply_configured_export_limit" in post_source
    assert "CONF_SIGENERGY_EXPORT_LIMIT_KW" in post_source
    assert 'entry_data["_skip_reload"] = True' in post_source
    assert "opt_coordinator.update_config(" in post_source
    assert "max_grid_export_w" in post_source
    assert "opt_coordinator.force_reoptimize()" in post_source
    assert post_source.index("if success:") < post_source.index(
        'entry_data["_skip_reload"] = True'
    )
    assert '"max_charge_w": CONF_OPTIMIZATION_MAX_CHARGE_W' in post_source
    assert '"max_discharge_w": CONF_OPTIMIZATION_MAX_DISCHARGE_W' in post_source
    assert "sigenergy_capped_optimizer_limit_w(" in post_source


def test_sigenergy_settings_get_prefers_configured_rate_caps_including_zero():
    """Controls shows durable caps while retaining live register diagnostics."""
    source = (COMPONENT_ROOT / "__init__.py").read_text()
    tree = ast.parse(source)
    resolver = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_sigenergy_controls_rate_limit_kw"
    )
    resolver_module = ast.Module(body=[resolver], type_ignores=[])
    ast.fix_missing_locations(resolver_module)
    namespace: dict[str, object] = {}
    exec(compile(resolver_module, str(COMPONENT_ROOT / "__init__.py"), "exec"), namespace)
    resolve = namespace["_sigenergy_controls_rate_limit_kw"]

    assert callable(resolve)
    assert resolve(10.0, 16.8) == 10.0
    assert resolve(0.0, 16.8) == 0.0
    assert resolve(None, 0.0) == 0.0
    assert resolve(None, 19.2) == 19.2

    view = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SigenergySettingsView"
    )
    get = next(
        node
        for node in view.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "get"
    )
    get_source = ast.get_source_segment(source, get)

    assert get_source is not None
    assert '"configured_charge_rate_limit_kw": configured_charge_limit_kw' in get_source
    assert '"effective_charge_rate_limit_kw": effective_charge_limit_kw' in get_source
    assert '"configured_discharge_rate_limit_kw": configured_discharge_limit_kw' in get_source
    assert '"effective_discharge_rate_limit_kw": effective_discharge_limit_kw' in get_source


def test_sigenergy_setup_threads_rate_caps_into_restore_and_optimizer():
    """Restart paths retain both hardware restore targets and LP limits."""
    init_source = (COMPONENT_ROOT / "__init__.py").read_text()
    coordinator_source = (COMPONENT_ROOT / "coordinator.py").read_text()

    # Persistent energy coordinator plus every force/restore scratch controller.
    assert init_source.count(
        "configured_charge_rate_limit_kw=entry.data.get("
    ) >= 6
    assert init_source.count(
        "configured_discharge_rate_limit_kw=entry.data.get("
    ) >= 6
    assert "sigenergy_capped_optimizer_limit_w(" in init_source
    assert "saved_max_charge_w," in init_source
    assert "saved_max_discharge_w," in init_source
    assert "configured_charge_rate_limit_kw=configured_charge_rate_limit_kw" in coordinator_source
    assert (
        "configured_discharge_rate_limit_kw=configured_discharge_rate_limit_kw"
        in coordinator_source
    )


def test_sigenergy_optimizer_cap_never_raises_raw_planning_limit():
    source_path = COMPONENT_ROOT / "optimization" / "coordinator.py"
    source = source_path.read_text()
    tree = ast.parse(source)
    helper = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "sigenergy_capped_optimizer_limit_w"
    )
    helper_module = ast.Module(body=[helper], type_ignores=[])
    ast.fix_missing_locations(helper_module)
    namespace: dict[str, object] = {"Any": object}
    exec(compile(helper_module, str(source_path), "exec"), namespace)
    cap = namespace["sigenergy_capped_optimizer_limit_w"]

    assert callable(cap)
    assert cap(3000, 5) == 3000
    assert cap(3000, 10) == 3000
    assert cap(8000, 5) == 5000
    assert cap(8000, 0) == 0
    assert cap(0, 5) == 0
    assert cap(None, 5) == 5000


def test_optimizer_settings_persist_raw_sigenergy_limit_but_apply_cap():
    source = (COMPONENT_ROOT / "optimization" / "coordinator.py").read_text()
    tree = ast.parse(source)
    coordinator = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "OptimizationCoordinator"
    )
    set_settings = next(
        node
        for node in coordinator.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "set_settings"
    )
    method_source = ast.get_source_segment(source, set_settings)

    assert method_source is not None
    assert "raw_config_updates =" in method_source
    assert "sigenergy_capped_optimizer_limit_w(" in method_source
    assert 'raw_config_updates["max_charge_w"]' in method_source
    assert 'raw_config_updates["max_discharge_w"]' in method_source
    assert 'int(settings["max_charge_w"])' in method_source
    assert 'int(settings["max_discharge_w"])' in method_source


def test_sigenergy_settings_validate_complete_payload_before_hardware_writes():
    source_path = COMPONENT_ROOT / "__init__.py"
    source = source_path.read_text()
    tree = ast.parse(source)
    validator = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_validate_sigenergy_settings_payload"
    )
    validator_module = ast.Module(body=[validator], type_ignores=[])
    ast.fix_missing_locations(validator_module)
    namespace: dict[str, object] = {"Any": object}
    exec(compile(validator_module, str(source_path), "exec"), namespace)
    validate = namespace["_validate_sigenergy_settings_payload"]

    assert callable(validate)
    assert validate(
        {
            "backup_reserve": "20",
            "charge_rate_limit_kw": "5",
            "discharge_rate_limit_kw": 8,
            "export_limit_kw": None,
        }
    ) == (
        {
            "backup_reserve": 20,
            "charge_rate_limit_kw": 5.0,
            "discharge_rate_limit_kw": 8.0,
            "export_limit_kw": None,
        },
        None,
    )
    assert validate(
        {"charge_rate_limit_kw": 5, "discharge_rate_limit_kw": "invalid"}
    )[0] == {}

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
    validation_index = post_source.index("_validate_sigenergy_settings_payload(body)")
    hardware_write_indexes = [
        post_source.index("controller.set_backup_reserve"),
        post_source.index("controller.apply_configured_charge_rate_limit"),
        post_source.index("controller.apply_configured_discharge_rate_limit"),
        post_source.index("controller.apply_configured_export_limit"),
    ]
    assert validation_index < min(hardware_write_indexes)


def test_sigenergy_restore_failure_keeps_force_state_active():
    """The restore service must clear state only after hardware success."""
    source = (COMPONENT_ROOT / "__init__.py").read_text()
    tree = ast.parse(source)
    setup = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry"
    )
    restore = next(
        node
        for node in ast.walk(setup)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_restore_normal"
    )
    restore_source = ast.get_source_segment(source, restore)
    assert restore_source is not None
    failure_index = restore_source.index('else:\n                        _LOGGER.warning("Sigenergy restore_normal failed")')
    failure_return_index = restore_source.index("return", failure_index)
    state_clear_index = restore_source.index(
        'force_discharge_state["active"] = False',
        failure_index,
    )
    assert failure_index < failure_return_index < state_clear_index


def test_sigenergy_restore_failure_schedules_bounded_retry():
    """A partial restore must retry while preserving active force state."""
    source = (COMPONENT_ROOT / "__init__.py").read_text()
    tree = ast.parse(source)
    setup = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry"
    )
    restore = next(
        node
        for node in ast.walk(setup)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_restore_normal"
    )
    restore_source = ast.get_source_segment(source, restore)

    assert restore_source is not None
    assert "def _schedule_sigenergy_restore_retry" in restore_source
    assert "if restore_retry_count >= 3:" in restore_source
    assert '"_restore_retry": next_retry' in restore_source
    assert '"_native_control": sigenergy_native_control' in restore_source
    assert 'if source != "optimizer" and not (' in restore_source
    assert 'if source == "optimizer":\n                            raise HomeAssistantError(' in restore_source
    failure_index = restore_source.index(
        'else:\n                        _LOGGER.warning("Sigenergy restore_normal failed")'
    )
    retry_index = restore_source.index(
        '_schedule_sigenergy_restore_retry("hardware restore returned false")',
        failure_index,
    )
    failure_return_index = restore_source.index("return", retry_index)
    assert failure_index < retry_index < failure_return_index


def test_sigenergy_self_consumption_failure_propagates_to_optimizer():
    """The HA service must raise so the optimizer wrapper can retry."""
    source = (COMPONENT_ROOT / "__init__.py").read_text()
    tree = ast.parse(source)
    setup = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry"
    )
    handler = next(
        node
        for node in ast.walk(setup)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "handle_set_self_consumption"
    )
    handler_source = ast.get_source_segment(source, handler)

    assert handler_source is not None
    sigenergy_index = handler_source.index(
        "is_sigenergy = bool(entry.data.get(CONF_SIGENERGY_STATION_ID))"
    )
    sigenergy_source = handler_source[sigenergy_index:]
    assert "Sigenergy self-consumption restore failed" in sigenergy_source
    assert "Sigenergy self-consumption coordinator/controller unavailable" in sigenergy_source
    assert "except HomeAssistantError:\n                raise" in sigenergy_source


def test_optimizer_restore_failure_retains_force_state_and_action_marker():
    """Failed Sigenergy cleanup must remain retryable on the next optimizer cycle."""
    source = (COMPONENT_ROOT / "optimization" / "coordinator.py").read_text()

    reserve_log = "Optimizer: Force-discharge reserve restore failed; "
    reserve_index = source.index(reserve_log)
    reserve_section = source[reserve_index - 1400:reserve_index + 700]
    assert "restore_success = await battery.restore_normal()" in reserve_section
    assert "if restore_success is False:" in reserve_section
    assert reserve_section.index("if restore_success is False:") < reserve_section.index(
        "self._clear_optimizer_force_state()"
    )
    assert 'self._last_executed_action = "self_consumption"' not in reserve_section[
        reserve_section.index("if restore_success is False:"):
        reserve_section.index("self._clear_optimizer_force_state()")
    ]

    cancel_log = "Optimizer: Restore after canceling force %s failed; "
    cancel_index = source.index(cancel_log)
    cancel_section = source[cancel_index - 1200:cancel_index + 500]
    assert "optimizer_force_snapshot = dict(self._optimizer_force_state)" in cancel_section
    assert "restore_success = await battery.restore_normal()" in cancel_section
    assert "self._optimizer_force_state = optimizer_force_snapshot" in cancel_section


def test_sigenergy_settings_get_keeps_configured_cap_visible_during_curtailment():
    """Controls must not replace the durable cap with the temporary live 0 kW.

    Sigenergy curtailment writes zero to the physical export-limit register.
    The existing mobile client renders ``export_limit_kw`` as the Controls
    value, so that field must prefer the configured site cap while exposing
    the live register separately for diagnostics.
    """
    source = (COMPONENT_ROOT / "__init__.py").read_text()
    tree = ast.parse(source)
    resolver = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_sigenergy_controls_export_limit_kw"
    )
    resolver_module = ast.Module(body=[resolver], type_ignores=[])
    ast.fix_missing_locations(resolver_module)
    namespace: dict[str, object] = {}
    exec(compile(resolver_module, str(COMPONENT_ROOT / "__init__.py"), "exec"), namespace)
    resolve = namespace["_sigenergy_controls_export_limit_kw"]

    assert callable(resolve)
    assert resolve(5.0, 0.0) == 5.0
    assert resolve(0.0, 16.8) == 0.0
    assert resolve(None, 16.8) == 16.8
    assert resolve(5.0, None) == 5.0

    view = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SigenergySettingsView"
    )
    get = next(
        node
        for node in view.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "get"
    )
    get_source = ast.get_source_segment(source, get)

    assert get_source is not None
    assert "configured_export_limit_kw = entry.data.get(" in get_source
    assert "CONF_SIGENERGY_EXPORT_LIMIT_KW" in get_source
    assert '"effective_export_limit_kw": effective_export_limit_kw' in get_source
    assert '"export_limit_kw": _sigenergy_controls_export_limit_kw(' in get_source
