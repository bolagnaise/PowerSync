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
import math
from pathlib import Path
import sys
import types
from collections.abc import Mapping
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


@pytest.fixture
def simple_site(monkeypatch):
    """A shared persistent store and device survive replacement controllers."""
    saved = {}
    faults = {"save": False, "read": False, "write": False, "swap_on_save": False}

    class MemoryStore:
        def __init__(self, _hass, _version, key, **_kwargs):
            self.key = key

        async def async_load(self):
            return saved.get(self.key)

        async def async_save(self, data):
            if faults["save"]:
                raise OSError("storage unavailable")
            saved[self.key] = dict(data)
            if data and faults["swap_on_save"]:
                serial[0] += 1

    storage = types.ModuleType("homeassistant.helpers.storage")
    storage.Store = MemoryStore
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.storage", storage)
    hass = SimpleNamespace(data={"power_sync": {"entry-id": {}}})
    registers = {40236: 1, 40232: 10000}
    serial = [0x4142] * 16
    writes = []

    async def connect(self):
        return True

    async def disconnect(self):
        pass

    async def read(self, address, count=1):
        if faults["read"]:
            return None
        if address == 40052:
            return list(serial)
        return [registers.get(address, 0)]

    async def write(self, address, value):
        # The obligation must be durable BEFORE touching hardware.
        assert any(saved.values())
        writes.append((address, value))
        if faults["write"]:
            return False
        registers[address] = value
        return True

    monkeypatch.setattr(FroniusController, "connect", connect)
    monkeypatch.setattr(FroniusController, "disconnect", disconnect)
    monkeypatch.setattr(FroniusController, "_read_register", read)
    monkeypatch.setattr(FroniusController, "_write_register", write)
    from power_sync.inverters.fronius_native import FroniusNativeExportLimit
    monkeypatch.setattr(FroniusNativeExportLimit, "_surface", lambda self: None)

    def make_controller(**kwargs):
        controller = FroniusController(
            kwargs.pop("host", "192.0.2.1"), hass=hass,
            entry_id=kwargs.pop("entry_id", "entry-id"), **kwargs,
        )
        return controller

    return SimpleNamespace(saved=saved, hass=hass, registers=registers,
                           writes=writes, serial=serial, faults=faults,
                           make=make_controller)


def test_simple_mode_restart_retains_restore_obligation(simple_site):
    """Ticket #157: a new controller/runtime must not forget its own Ena=0."""
    site = simple_site

    async def scenario():
        before_restart = site.make()
        assert await before_restart.curtail() is True
        assert site.registers[40236] == 0
        site.hass.data["power_sync"]["entry-id"] = {}
        after_restart = site.make()
        state = await after_restart.get_status()
        assert state.is_curtailed is True
        assert await after_restart.restore() is True
        assert site.registers[40236] == 1
        assert site.registers[40232] == 10000
        assert await after_restart.has_pending_simple_restore() is False

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["serial", "read", "limit", "owner", "stopping"])
def test_simple_recovery_mismatch_never_writes_or_discards_obligation(simple_site, fault):
    site = simple_site

    async def scenario():
        assert await site.make().curtail() is True
        site.writes.clear()
        if fault == "serial":
            site.serial[0] += 1
        elif fault == "read":
            site.faults["read"] = True
        elif fault == "limit":
            site.registers[40232] = 5000
        elif fault == "owner":
            next(v for v in site.saved.values() if v)["owner_entry_id"] = "another-entry"
        elif fault == "stopping":
            site.hass.data["power_sync"]["entry-id"]["aemo_dispatch_stopping"] = True
        assert await site.make().restore() is False
        assert not site.writes
        assert any(site.saved.values())

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["save", "write"])
def test_simple_curtailment_and_restore_failures_retain_safe_boundary(simple_site, fault):
    site = simple_site

    async def scenario():
        if fault == "save":
            site.faults["save"] = True
            controller = site.make()
            assert await controller.curtail() is False
            assert controller.simple_restore_blocked is True
            assert not site.writes
        else:
            assert await site.make().curtail() is True
            site.faults["write"] = True
            assert await site.make().restore() is False
            assert any(site.saved.values())
            site.faults["write"] = False
            assert await site.make().restore() is True
            assert not any(site.saved.values())

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["none", "host", "slave", "mode", "disabled", "unowned"])
def test_startup_recovers_only_matching_owned_simple_mode_without_writes(simple_site, change):
    from power_sync.inverters.fronius_simple import async_recover_simple_curtailment

    site = simple_site
    entry = SimpleNamespace(entry_id="entry-id", data={}, options={
        "inverter_brand": "fronius", "inverter_host": "192.0.2.1",
        "ac_inverter_curtailment_enabled": True,
    })

    async def scenario():
        assert await site.make().curtail() is True
        site.hass.data["power_sync"]["entry-id"] = {}
        site.writes.clear()
        if change == "host":
            entry.options["inverter_host"] = "192.0.2.2"
        elif change == "slave":
            entry.options["inverter_slave_id"] = 2
        elif change == "mode":
            entry.options["fronius_load_following"] = True
        elif change == "disabled":
            entry.options["ac_inverter_curtailment_enabled"] = False
        elif change == "unowned":
            site.saved.clear()  # A pre-upgrade Ena=0 has no provenance.
        saved_before = dict(site.saved)
        await async_recover_simple_curtailment(site.hass, entry)
        runtime = site.hass.data["power_sync"]["entry-id"]
        assert (runtime.get("inverter_last_state") == "curtailed") is (change == "none")
        assert not site.writes
        assert site.saved == saved_before
        if change == "none":
            assert runtime["inverter_curtailment_device_limit_confirmed"] is False
            assert runtime.get("inverter_curtailment_physical_converged") is None

    asyncio.run(scenario())


def test_smart_restore_after_restart_reaches_actual_modbus_restore(simple_site):
    site = simple_site
    assert asyncio.run(site.make().curtail()) is True
    site.hass.data["power_sync"]["entry-id"] = {}
    state = _run_fronius_curtailment(
        load_following=False, should_curtail_result=False,
        controller_override=site.make(),
    )
    assert state["control_mode"] == "normal"
    assert site.registers[40236] == 1
    assert not any(site.saved.values())


def test_recovery_readback_mismatch_does_not_escalate_to_offgrid(simple_site):
    site = simple_site
    assert asyncio.run(site.make().curtail()) is True
    site.serial[0] += 1
    site.writes.clear()
    recorded = _run_fronius_curtailment(
        load_following=False, controller_override=site.make(),
        expected_result=False, expect_state=False,
    )
    assert recorded["fallback_calls"] == []
    assert recorded["restore_calls"] == 0
    assert not site.writes


def test_already_restored_device_retires_journal_without_control_write(simple_site):
    site = simple_site
    assert asyncio.run(site.make().curtail()) is True
    site.registers[40236] = 1
    site.writes.clear()
    assert asyncio.run(site.make().restore()) is True
    assert not site.writes
    assert not any(site.saved.values())


def test_restore_journal_failure_is_retryable_without_replaying_control(simple_site):
    site = simple_site
    assert asyncio.run(site.make().curtail()) is True
    site.faults["save"] = True
    assert asyncio.run(site.make().restore()) is False
    assert site.registers[40236] == 1
    assert any(site.saved.values())
    site.faults["save"] = False
    site.writes.clear()
    assert asyncio.run(site.make().restore()) is True
    assert not site.writes


@pytest.mark.parametrize("enabled,limit", [(0, 10000), (1, 5000)])
def test_unowned_disabled_setting_or_user_cap_is_never_adopted(simple_site, enabled, limit):
    site = simple_site
    site.registers.update({40236: enabled, 40232: limit})
    controller = site.make()
    assert asyncio.run(controller.curtail()) is False
    assert controller.simple_restore_blocked is True
    assert not site.writes
    assert not any(site.saved.values())


def test_device_swap_during_durable_save_never_reaches_control(simple_site):
    site = simple_site
    site.faults["swap_on_save"] = True
    assert asyncio.run(site.make().curtail()) is False
    assert not site.writes
    assert any(site.saved.values())


@pytest.mark.parametrize("tracked,battery_w,grid_w,import_c,export_c,expected", [
    (True, 0, 100, 14, -1, True),
    (True, -60, 100, 14, -1, False),
    (False, 0, 100, 14, -1, False),
    (False, 0, -101, 14, -1, True),
    (True, 0, 100, 14, 13.1, False),
    (True, -60, 100, -1, -1, True),
])
def test_low_soc_alone_does_not_override_negative_export_ownership(
    tracked, battery_w, grid_w, import_c, export_c, expected,
):
    """Replay the secondary claim without weakening the deliberate hold policy."""
    runtime = {"inverter_last_state": "curtailed"} if tracked else {}

    async def live():
        return {"battery_soc": 6, "battery_power": battery_w, "grid_power": grid_w,
                "solar_power": 400, "load_power": 400}

    namespace = {
        "_aemo_dispatch_entry_data": lambda: runtime,
        "get_live_status": live, "_LOGGER": _Logger(),
        "entry": SimpleNamespace(options={"inverter_restore_soc": 98}, data={}),
        "CONF_INVERTER_RESTORE_SOC": "inverter_restore_soc",
        "DEFAULT_INVERTER_RESTORE_SOC": 98,
        "export_earnings_are_uneconomic": lambda value, *_args: value < 0,
    }
    exec(_function_source("should_curtail_ac_coupled"), namespace)
    assert asyncio.run(namespace["should_curtail_ac_coupled"](import_c, export_c)) is expected


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


def _top_level_function_source(name: str) -> str:
    source = INIT_PATH.read_text()
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            return segment
    raise AssertionError(f"{name} not found")


class _Logger:
    def __getattr__(self, _name):
        def _log(*_args, **_kwargs):
            return None

        return _log


def _run_fronius_curtailment(
    *,
    load_following: bool,
    should_curtail_result: bool | None = True,
    inverter_last_state: str | None = None,
    expect_state: bool = True,
    native_result: bool | None = None,
    controller_override=None,
    expected_result=True,
) -> dict:
    """Drive the real apply_inverter_curtailment() fronius success branch."""
    entry_id = "entry-id"
    generation = object()
    entry_data = {"aemo_dispatch_generation": generation}
    if inverter_last_state is not None:
        entry_data["inverter_last_state"] = inverter_last_state
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
    recorded: dict = {"state": None, "constructed": 0, "restore_calls": 0, "fallback_calls": []}

    controller, _writes = _recording_controller(load_following=load_following)
    if controller_override is not None:
        controller = controller_override
    original_restore = controller.restore

    async def _record_restore():
        recorded["restore_calls"] += 1
        return await original_restore()

    controller.restore = _record_restore
    if native_result is not None:
        async def native_curtail(home_load_w=None):
            controller.native_export_control = True
            if native_result:
                controller.last_curtail_mode = "native_export"
                controller.last_curtail_limit_w = 0
            return native_result
        controller.curtail = native_curtail
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
        return should_curtail_result

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
        recorded["fallback_calls"].append(_curtail)
        return False

    def _record_state(control_mode, target_power_w=None, **kwargs):
        entry_data["inverter_last_state"] = "normal" if control_mode == "normal" else "curtailed"
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
        "ac_inverter_is_same_hybrid": lambda: False,
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

    namespace["Mapping"] = Mapping
    namespace["math"] = math
    exec(_top_level_function_source("_load_following_site_load_w"), namespace)
    exec(_function_source("_aemo_dispatch_entry_data"), namespace)
    exec(_function_source("apply_inverter_curtailment"), namespace)
    result = asyncio.run(namespace["apply_inverter_curtailment"](True, 10.67, 0.14))

    assert result is (expected_result and native_result is not False)
    assert recorded["constructed"] == 1
    if expect_state:
        assert recorded["state"] is not None
        return recorded["state"]
    return recorded


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


def test_unavailable_live_status_never_restores_an_existing_curtailment():
    """A missing sample must defer, rather than treating it as absorption."""
    recorded = _run_fronius_curtailment(
        load_following=True,
        should_curtail_result=None,
        inverter_last_state="curtailed",
        expect_state=False,
    )
    assert recorded["state"] is None
    assert recorded["restore_calls"] == 0


def test_native_site_limit_does_not_claim_device_or_physical_confirmation():
    state = _run_fronius_curtailment(load_following=True, native_result=True)
    assert state["target_power_w"] == 0
    assert state["device_limit_confirmed"] is False
    assert "physical_converged" not in state


def test_native_failure_does_not_escalate_to_offgrid_fallback():
    recorded = _run_fronius_curtailment(load_following=True, native_result=False, expect_state=False)
    assert recorded["fallback_calls"] == []
    assert recorded["state"] is None
