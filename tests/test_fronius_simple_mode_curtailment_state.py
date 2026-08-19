"""Ticket #226: Fronius simple mode must not report a device power limit.

Fronius has two curtailment modes.  Load-following writes ``WMaxLimPct`` and
therefore does set a real device power limit.  Simple mode only writes
``WMaxLim_Ena = 0`` so the inverter falls back to its own soft export limit —
no power-limit register is written at all.

The reported install ran simple mode with no soft export limit configured, so
the site kept exporting ~8.5 kW while PowerSync logged ``Fronius limit
confirmed at 1066W`` and published ``control_mode=load_following``,
``target_power_w=1066``, ``device_limit_confirmed=True`` for twenty minutes.
"""

from __future__ import annotations

import asyncio
import ast
from pathlib import Path
import sys
import types
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"
INIT_PATH = COMPONENT_ROOT / "__init__.py"


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
    sys.modules.setdefault("pymodbus", pymodbus)
    sys.modules.setdefault("pymodbus.client", pymodbus_client)
    sys.modules.setdefault("pymodbus.exceptions", pymodbus_exceptions)

    if "power_sync" not in sys.modules:
        power_sync = types.ModuleType("power_sync")
        power_sync.__path__ = [str(COMPONENT_ROOT)]
        sys.modules["power_sync"] = power_sync

    if "power_sync.inverters" not in sys.modules:
        inverters = types.ModuleType("power_sync.inverters")
        inverters.__path__ = [str(COMPONENT_ROOT / "inverters")]
        sys.modules["power_sync.inverters"] = inverters


_install_package_stubs()

from power_sync.inverters.fronius import FroniusController  # noqa: E402


@pytest.fixture(autouse=True)
def _skip_fronius_settle_delays(monkeypatch):
    async def no_sleep(_seconds: float) -> None:
        return None

    fronius_module = sys.modules["power_sync.inverters.fronius"]
    monkeypatch.setattr(fronius_module.asyncio, "sleep", no_sleep)


def _recording_controller(
    *, load_following: bool
) -> tuple[FroniusController, list[tuple[int, int]]]:
    """A controller whose registers always read back whatever was written."""
    controller = FroniusController("192.0.2.1", load_following=load_following)
    writes: list[tuple[int, int]] = []
    registers = {
        controller.REG_WMAXLIM_ENA: 1,
        controller.REG_WMAXLIMPCT: 10_000,
        controller.REG_WMAXLIMPCT_RVRT: 0,
    }

    async def connect() -> bool:
        return True

    async def write_register(address: int, value: int) -> bool:
        writes.append((address, value))
        registers[address] = value
        return True

    async def read_register(address: int, count: int = 1):
        return [registers.get(address, 0)]

    controller.connect = connect
    controller._write_register = write_register
    controller._read_register = read_register
    return controller, writes


def test_simple_mode_writes_no_power_limit_register_and_records_no_limit():
    controller, writes = _recording_controller(load_following=False)

    assert asyncio.run(controller.curtail(home_load_w=1_066)) is True

    assert writes == [(controller.REG_WMAXLIM_ENA, 0)]
    assert all(address != controller.REG_WMAXLIMPCT for address, _ in writes)
    assert controller.last_curtail_mode == "simple"
    assert controller.last_curtail_limit_w is None


def test_load_following_mode_records_the_limit_it_actually_wrote():
    controller, writes = _recording_controller(load_following=True)

    result = asyncio.run(
        controller.curtail(home_load_w=1_066, rated_capacity_w=10_000)
    )

    assert result is True
    assert (controller.REG_WMAXLIMPCT, 1_066) in writes
    assert controller.last_curtail_mode == "load_following"
    assert controller.last_curtail_limit_w == 1_066


def test_restore_clears_the_recorded_device_limit():
    controller, _writes = _recording_controller(load_following=True)

    asyncio.run(controller.curtail(home_load_w=1_066, rated_capacity_w=10_000))
    assert controller.last_curtail_limit_w == 1_066

    assert asyncio.run(controller.restore()) is True
    assert controller.last_curtail_mode is None
    assert controller.last_curtail_limit_w is None


def _function_source(name: str) -> str:
    source = INIT_PATH.read_text()
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry":
            for child in node.body:
                if (
                    isinstance(child, (ast.AsyncFunctionDef, ast.FunctionDef))
                    and child.name == name
                ):
                    segment = ast.get_source_segment(source, child)
                    assert segment is not None
                    return segment
    raise AssertionError(f"{name} not found")


class _Logger:
    def __getattr__(self, _name):
        def _log(*_args, **_kwargs):
            return None

        return _log


def _run_fronius_curtailment(*, load_following: bool) -> dict:
    """Drive the real apply_inverter_curtailment() fronius success branch."""
    entry_id = "entry-id"
    generation = object()
    entry_data = {"aemo_dispatch_generation": generation}
    hass = SimpleNamespace(data={"power_sync": {entry_id: entry_data}})
    entry = SimpleNamespace(
        entry_id=entry_id,
        options={
            "ac_inverter_curtailment_enabled": True,
            "inverter_brand": "fronius",
            "inverter_host": "192.0.2.1",
            "fronius_load_following": load_following,
        },
        data={},
    )
    recorded: dict = {"state": None, "constructed": 0}

    controller, _writes = _recording_controller(load_following=load_following)
    if load_following:
        # get_rated_capacity() reads the SunSpec model block; the reported site
        # is a Primo GEN24 10.0 Plus.
        async def get_rated_capacity():
            return 10_000

        controller.get_rated_capacity = get_rated_capacity

    def _get_inverter_controller(*_args, **_kwargs):
        recorded["constructed"] += 1
        return controller

    async def _should_curtail(*_args):
        return True

    async def _get_live_status():
        # Site is exporting 8,477 W with the battery full: curtail is correct.
        return {
            "load_power": 1_066,
            "battery_power": 0,
            "grid_power": -8_477.0,
            "solar_power": 9_541,
            "battery_soc": 100.0,
        }

    async def _fallback(_curtail, _reason):
        return False

    def _record_state(control_mode, target_power_w=None, **kwargs):
        recorded["state"] = {
            "control_mode": control_mode,
            "target_power_w": target_power_w,
            **kwargs,
        }

    namespace = {
        "Any": object,
        "DOMAIN": "power_sync",
        "hass": hass,
        "entry": entry,
        "aemo_dispatch_generation": generation,
        "_LOGGER": _Logger(),
        "get_inverter_controller": _get_inverter_controller,
        "should_curtail_ac_coupled": _should_curtail,
        "_powerwall_curtailment_fallback": _fallback,
        "_set_inverter_control_state": _record_state,
        "get_live_status": _get_live_status,
        "INVERTER_CONTROL_MODE_NORMAL": "normal",
        "INVERTER_CONTROL_MODE_LOAD_FOLLOWING": "load_following",
        "INVERTER_CONTROL_MODE_CURTAILED": "curtailed",
        "INVERTER_CONTROL_MODES": {"normal", "load_following", "curtailed"},
        "DEFAULT_INVERTER_PORT": 502,
        "DEFAULT_INVERTER_SLAVE_ID": 1,
    }
    for constant in (
        "CONF_AC_INVERTER_CURTAILMENT_ENABLED",
        "CONF_POWERWALL_OFFGRID_AS_CURTAILMENT",
        "CONF_INVERTER_BRAND",
        "CONF_INVERTER_HOST",
        "CONF_INVERTER_PORT",
        "CONF_INVERTER_SLAVE_ID",
        "CONF_INVERTER_MODEL",
        "CONF_INVERTER_TOKEN",
        "CONF_FRONIUS_LOAD_FOLLOWING",
        "CONF_INVERTER_RATED_POWER_W",
        "CONF_ENPHASE_USERNAME",
        "CONF_ENPHASE_PASSWORD",
        "CONF_ENPHASE_SERIAL",
        "CONF_ENPHASE_NORMAL_PROFILE",
        "CONF_ENPHASE_ZERO_EXPORT_PROFILE",
        "CONF_ENPHASE_IS_INSTALLER",
        "CONF_SIGENERGY_EXPORT_LIMIT_KW",
        "CONF_INVERTER_ENTITY_PREFIX",
    ):
        namespace[constant] = constant.removeprefix("CONF_").lower()

    exec(_function_source("_aemo_dispatch_entry_data"), namespace)
    exec(_function_source("apply_inverter_curtailment"), namespace)
    result = asyncio.run(namespace["apply_inverter_curtailment"](True, 10.67, 0.14))

    assert result is True
    assert recorded["constructed"] == 1
    assert recorded["state"] is not None
    return recorded["state"]


def test_simple_mode_never_records_a_confirmed_device_limit():
    state = _run_fronius_curtailment(load_following=False)

    assert state["control_mode"] == "curtailed"
    assert state["target_power_w"] is None
    assert state["device_limit_confirmed"] is False


def test_load_following_mode_records_the_limit_that_was_written():
    state = _run_fronius_curtailment(load_following=True)

    assert state["control_mode"] == "load_following"
    assert state["target_power_w"] == 1_066
    assert state["device_limit_confirmed"] is True


def test_convergence_is_not_judged_from_telemetry_taken_before_the_command():
    """The 100W convergence verdict used pre-command grid power.

    At 13:01:09 the site was exporting 5.6 W *before* any command, and the
    apply path credited the write with 'site export is within the 100W
    convergence threshold'.  The apply path must no longer publish a physical
    convergence verdict at all; the 30s cycle re-reads live status and owns it.
    """
    for load_following in (False, True):
        state = _run_fronius_curtailment(load_following=load_following)
        assert "physical_converged" not in state
        assert "residual_export_w" not in state

    source = _function_source("apply_inverter_curtailment")
    success_branch = source[source.index("if success:") :]
    assert "curtail_live_status" not in success_branch.split("else:")[0]


def test_recurring_simple_mode_cycles_never_accumulate_a_false_limit():
    """The reported variant: ~60 consecutive reapplications, none converging."""
    for _ in range(5):
        state = _run_fronius_curtailment(load_following=False)
        assert state["device_limit_confirmed"] is False
        assert state["target_power_w"] is None
