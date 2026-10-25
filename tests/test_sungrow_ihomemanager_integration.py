"""Native command routing and eligibility with AST-isolated HA coordinators."""
from __future__ import annotations

import ast
import asyncio
import math
import sys
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from test_sungrow_ihomemanager import device, driver, writes

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/power_sync"


def compile_nodes(path, names, namespace):
    tree = ast.parse(path.read_text())
    selected = [node for node in tree.body if getattr(node, "name", None) in names]
    assert len(selected) == len(names)
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.Module(body=[future, *selected], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)


@pytest.fixture
def integration():
    ns = {"math": math, "time": time}
    for name in ("CONF_MONITORING_MODE", "CONF_OPTIMIZATION_BACKUP_RESERVE", "CONF_SUNGROW_HOST",
                 "CONF_SUNGROW_PORT", "CONF_SUNGROW_SLAVE_ID", "CONF_SUNGROW_CONNECTION_TYPE",
                 "CONF_SUNGROW_IHOMEMANAGER_HOST", "CONF_SUNGROW_IHOMEMANAGER_PORT"):
        ns[name] = name.removeprefix("CONF_").lower()
    ns["DOMAIN"] = "power_sync"
    ns["SUNGROW_CONNECTION_IHOMEMANAGER_NATIVE"] = "ihomemanager_native"
    compile_nodes(COMPONENT / "sungrow_ihomemanager_config.py", {"native_connection_settings"}, ns)
    generation = [None]
    ns.update(control_generation=lambda: generation[0], control_entry_generation=lambda: 7,
              service_allowed=lambda hass, data: hass.permitted)
    logger = SimpleNamespace(warning=lambda *args: None)
    base_namespace = {"_LOGGER": logger}
    base_tree = ast.parse((COMPONENT / "coordinator.py").read_text())
    original = next(node for node in base_tree.body if isinstance(node, ast.ClassDef) and node.name == "SungrowEnergyCoordinator")
    blocked = {
        "_native_control_allowed", "force_grid_export", "set_max_soc", "set_backup_reserve",
        "set_backup_mode", "set_no_discharge_mode", "set_charge_rate_limit", "set_discharge_rate_limit",
        "set_export_limit", "set_curtailment_export_limit", "restore_curtailment_export_limit",
        "restore_work_mode_from_idle", "restore_no_discharge_mode",
    }
    methods = [node for node in original.body if getattr(node, "name", None) in blocked]
    assert len(methods) == len(blocked)
    isolated = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *methods], type_ignores=[])
    exec(compile(ast.fix_missing_locations(isolated), "original-sungrow-methods", "exec"), base_namespace)

    class Base:
        def __init__(self, hass, host, port, slave_id, *, entry_id, telemetry_only, **kwargs):
            self.hass, self.host, self.port, self.slave_id = hass, host, port, slave_id
            self._entry_id = entry_id
            self._telemetry_only = telemetry_only
            self._modbus_lock = asyncio.Lock()
            self.last_update_success = True
            self.data = {}
            self._poll_data = dict(telemetry_ready=True, solar_power=2, grid_power=-1,
                                   battery_power=1, load_power=2, battery_level=65)
        async def _async_update_data(self):
            await self._controller.get_battery_data()
            return dict(self._poll_data)
        def startup_control_ready(self):
            return self.last_update_success and self.data.get("telemetry_ready") is True
        async def async_shutdown(self):
            self.base_shutdown = True
    for name in blocked:
        setattr(Base, name, base_namespace[name])

    class SH:
        def __init__(self, *args):
            self.raw = dict(battery_soc=65, battery_power=1000, pv_power=2000, load_power=2000, meter_power=-1000)
        async def get_battery_data(self):
            return dict(self.raw)

    class Native:
        MAX_DURATION_SECONDS = 86400
        def __init__(self, host, port, *, control_allowed):
            self.allowed = control_allowed
            self.calls = []
            self.snapshot = SimpleNamespace(supports_dispatch=True, protocol_version=0x01000200,
                                            max_charge_w=20000, max_discharge_w=18000)
        async def read_snapshot(self):
            if isinstance(self.snapshot, Exception):
                raise self.snapshot
            return self.snapshot
        def set_soc_limits(self, reserve_percent):
            if not 0 <= reserve_percent <= 100:
                raise ValueError("Invalid SOC guard")
            self.reserve_percent = reserve_percent
        async def start_dispatch(self, *args):
            assert self.allowed()
            self.calls.append(args)
            return True
        async def stop_dispatch(self):
            self.calls.append(("stop",))
            return True
        async def async_shutdown(self):
            self.calls.append(("shutdown",))
            return True

    ns.update(SungrowEnergyCoordinator=Base, SungrowSHController=SH, SungrowIHomeManagerController=Native)
    compile_nodes(COMPONENT / "sungrow_ihomemanager_coordinator.py",
                  {"_ReadOnlySungrowController", "SungrowIHomeManagerEnergyCoordinator"}, ns)
    settings = dict(sungrow_connection_type="ihomemanager_native", sungrow_host="inverter.test",
                    sungrow_port=502, sungrow_slave_id=1, sungrow_ihomemanager_host="ihm.test",
                    sungrow_ihomemanager_port=503, monitoring_mode=False, optimization_backup_reserve=20)
    entry = SimpleNamespace(data=settings, options={})
    optimizer = SimpleNamespace(enabled=True)
    runtime = {"optimization_coordinator": optimizer}
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_get_entry=lambda entry_id: entry),
                           data={"power_sync": {"entry": runtime}}, permitted=True)
    coordinator = ns["SungrowIHomeManagerEnergyCoordinator"](
        hass, "inverter.test", entry_id="entry", ihomemanager_host="ihm.test",
    )
    return SimpleNamespace(coordinator=coordinator, entry=entry, hass=hass, runtime=runtime,
                           optimizer=optimizer, generation=generation, ns=ns, blocked=blocked)


async def publish(coordinator):
    coordinator.data = await coordinator._async_update_data()


def test_native_charge_discharge_and_restore_use_native_transport(integration):
    async def run():
        c = integration.coordinator
        assert not await c.force_charge(30, 5000)  # No initial snapshots.
        await publish(c)
        assert await c.force_charge(60, 5000)
        assert await c.force_discharge(30, 7000)
        assert c._native.calls == [("charge", 5000, 3600), ("discharge", 7000, 1800)]
        assert await c.restore_normal()
        assert c._native.calls[-1] == ("stop",)
        assert not c.pending_optimizer_export_restore
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["force_grid_export", "set_max_soc", "set_backup_reserve",
    "set_backup_mode", "set_no_discharge_mode", "set_charge_rate_limit", "set_discharge_rate_limit",
    "set_export_limit", "set_curtailment_export_limit", "restore_curtailment_export_limit",
    "restore_work_mode_from_idle", "restore_no_discharge_mode"])
def test_unsupported_controls_never_fall_through_to_inverter_writes(integration, operation):
    async def run():
        c = integration.coordinator
        await publish(c)
        args = () if operation in ("force_grid_export", "set_backup_mode", "set_no_discharge_mode", "restore_curtailment_export_limit", "restore_work_mode_from_idle", "restore_no_discharge_mode") else (20,)
        assert await getattr(c, operation)(*args) is False
        assert not c._native.calls
        assert await c._controller._write_register(13049, 2) is False
    asyncio.run(run())


@pytest.mark.parametrize("reason", ["monitoring", "handoff", "protocol", "native_read", "incomplete_pv",
    "incomplete_grid", "stale", "failed_poll", "removed_entry", "changed_host", "changed_port",
    "changed_telemetry", "changed_route", "invalid_reserve"])
def test_controls_and_renewal_callback_fail_closed(integration, reason):
    async def run():
        c = integration.coordinator
        await publish(c)
        if reason == "monitoring": integration.entry.options["monitoring_mode"] = True
        elif reason == "handoff": integration.runtime["_monitoring_handoff_active"] = True
        elif reason == "protocol":
            c._native.snapshot.supports_dispatch = False
            await publish(c)
        elif reason == "native_read":
            c._native.snapshot = OSError("unreachable")
            await publish(c)
        elif reason == "incomplete_pv":
            del c._controller.raw["pv_power"]
            await publish(c)
        elif reason == "incomplete_grid":
            del c._controller.raw["meter_power"]
            await publish(c)
        elif reason == "stale": c._telemetry_at -= 91
        elif reason == "failed_poll": c.last_update_success = False
        elif reason == "removed_entry": integration.hass.config_entries.async_get_entry = lambda entry_id: None
        elif reason == "changed_host": integration.entry.options["sungrow_ihomemanager_host"] = "other.test"
        elif reason == "changed_port": integration.entry.options["sungrow_ihomemanager_port"] = "bad"
        elif reason == "changed_telemetry": integration.entry.options["sungrow_host"] = "other.test"
        elif reason == "changed_route": integration.entry.options["sungrow_connection_type"] = "direct"
        elif reason == "invalid_reserve": integration.entry.data["optimization_backup_reserve"] = float("nan")
        assert not await c.force_discharge(30, 5000)
        assert not c._native.allowed()
        assert not c._native.calls
        assert await c.restore_normal()  # Cleanup remains available.
    asyncio.run(run())


def test_optimizer_lease_tracks_disable_and_license_revocation(integration):
    async def run():
        c = integration.coordinator
        await publish(c)
        integration.generation[0] = 4
        assert await c.force_discharge(30, 5000, source="optimizer")
        assert c._lease_permission["_licensing_generation"] == 4
        integration.optimizer.enabled = False
        assert not c._native.allowed()
        integration.optimizer.enabled = True
        integration.hass.permitted = False
        assert not c._native.allowed()
        assert await c.restore_normal()
    asyncio.run(run())


def test_manual_control_does_not_require_optimizer_enabled(integration):
    async def run():
        c = integration.coordinator
        await publish(c)
        integration.optimizer.enabled = False
        assert await c.force_discharge(30, 5000)
        assert c._native.allowed()
    asyncio.run(run())


def test_zero_power_uses_fresh_native_maximum_and_reserve_changes_reach_guard(integration):
    async def run():
        c = integration.coordinator
        await publish(c)
        assert await c.force_discharge(30)
        assert c._native.calls[-1] == ("discharge", 18000, 1800)
        integration.entry.data["optimization_backup_reserve"] = 0.35
        assert c._native.allowed()
        assert c._native.reserve_percent == 35
    asyncio.run(run())


@pytest.mark.parametrize("data_reserve,options_reserve,expected", [
    (50, 20, 50), (0.5, 0.2, 50), (20, 50, 20), (0, 50, 0),
    (100, 20, 100), (None, 0.35, 35), (None, None, 20),
])
def test_native_reserve_uses_data_before_legacy_options(
    integration, data_reserve, options_reserve, expected,
):
    async def run():
        c = integration.coordinator
        key = "optimization_backup_reserve"
        integration.entry.data.pop(key)
        if data_reserve is not None:
            integration.entry.data[key] = data_reserve
        if options_reserve is not None:
            integration.entry.options[key] = options_reserve
        await publish(c)
        assert c._native.allowed()
        assert c._native.reserve_percent == expected
    asyncio.run(run())


@pytest.mark.parametrize("soc_tenths", [499, 500, 501])
def test_configured_reserve_blocks_native_discharge_at_floor(
    integration, driver, device, soc_tenths,
):
    async def run():
        c = integration.coordinator
        integration.entry.data["optimization_backup_reserve"] = 50
        integration.entry.options["optimization_backup_reserve"] = 20
        c._native = driver.SungrowIHomeManagerController(
            "ihm.test", control_allowed=c._dispatch_allowed,
        )
        device.data[12] = soc_tenths
        try:
            await publish(c)
            assert await c.force_discharge(30, 5000) is (soc_tenths > 500)
            if soc_tenths <= 500:
                assert not writes(device)
        finally:
            await c._native.async_shutdown()
    asyncio.run(run())


def test_active_native_lease_stops_when_data_reserve_rises(integration, driver, device):
    async def run():
        c = integration.coordinator
        integration.entry.options["optimization_backup_reserve"] = 20
        c._native = driver.SungrowIHomeManagerController(
            "ihm.test", control_allowed=c._dispatch_allowed,
        )
        c._native.RENEW_SECONDS = 0.001
        try:
            await publish(c)
            assert await c.force_discharge(30, 5000)
            # At 65% SOC a new 70% reserve must end the lease even though
            # the legacy options still say 20%.
            integration.entry.data["optimization_backup_reserve"] = 70
            await asyncio.wait_for(c._native._task, timeout=1)
            assert device.mode == [1, 0xCC, 0, 0]
            assert len([call for call in writes(device) if call[1:] == (8032, [20])]) == 1
        finally:
            await c._native.async_shutdown()
    asyncio.run(run())


def test_shutdown_stops_native_lease_before_closing_telemetry(integration):
    async def run():
        c = integration.coordinator
        await publish(c)
        assert await c.force_discharge(30, 5000)
        await c.async_shutdown()
        assert c._native.calls[-1] == ("shutdown",)
        assert c.base_shutdown
        assert not c._native.allowed()
    asyncio.run(run())


@pytest.mark.parametrize("updates", [{"sungrow_ihomemanager_host": ""}, {"sungrow_ihomemanager_port": 502},
    {"sungrow_slave_id": 247}, {"sungrow_host": "ihm.test", "sungrow_port": 503}])
def test_runtime_endpoint_guard_covers_legacy_bad_settings(integration, updates):
    settings = dict(integration.entry.data)
    settings.update(updates)
    with pytest.raises(ValueError):
        integration.ns["native_connection_settings"](settings)


def test_service_calls_preserve_power_and_optimizer_source_for_native_route():
    tree = ast.parse((COMPONENT / "__init__.py").read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Name) and n.func.value.id == "sungrow_coord"
             and n.func.attr in ("force_charge", "force_discharge")]
    assert len(calls) == 4
    for native in (True, False):
        class Receiver:
            native_ihomemanager = native
            def force_charge(self, duration, power_w, **kwargs):
                return duration, power_w, kwargs
            force_discharge = force_charge
        for call in calls:
            result = eval(compile(ast.Expression(call), "sungrow-service-call", "eval"),
                          {"sungrow_coord": Receiver(), "duration": 60, "power_w": 5000,
                           "guarded_w": 7000, "source": "optimizer"})
            assert result[0] == 60
            assert result[1] in (5000, 7000)
            assert result[2] == ({"source": "optimizer"} if native else {})


def test_optimizer_capabilities_replace_idle_and_disable_spread_export(integration):
    tree = ast.parse((COMPONENT / "optimization/coordinator.py").read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "OptimizationCoordinator")
    selected = [n for n in cls.body if getattr(n, "name", None) in
                ("_should_disable_idle_schedule", "spread_export_enabled", "_should_spread_export_schedule", "_effective_runtime_action")]
    namespace = {}
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, *selected], type_ignores=[])), "optimizer-capabilities", "exec"), namespace)
    owner = SimpleNamespace(energy_coordinator=integration.coordinator,
                            _config=SimpleNamespace(disable_idle_enabled=False, spread_export_enabled=True))
    owner._should_disable_idle_schedule = lambda: namespace["_should_disable_idle_schedule"](owner)
    assert owner._should_disable_idle_schedule()
    assert namespace["_effective_runtime_action"](owner, "idle") == "self_consumption"
    assert namespace["spread_export_enabled"].fget(owner) is False
    owner.spread_export_enabled = namespace["spread_export_enabled"].fget(owner)
    owner._supports_target_export_power = lambda: True
    assert namespace["_should_spread_export_schedule"](owner) is False
    owner.energy_coordinator = None
    assert not owner._should_disable_idle_schedule()
    assert namespace["spread_export_enabled"].fget(owner) is True


def test_rejected_replacement_cannot_change_existing_optimizer_lease_authority(integration):
    async def run():
        c = integration.coordinator
        await publish(c)
        integration.generation[0] = 4
        assert await c.force_discharge(30, 5000, source="optimizer")
        original = dict(c._lease_permission)
        integration.generation[0] = None
        integration.entry.options["monitoring_mode"] = True
        assert not await c.force_charge(30, 5000)
        assert c._lease_permission == original
        integration.entry.options["monitoring_mode"] = False
        integration.optimizer.enabled = False
        assert not c._native.allowed()
        assert not await c.force_charge(-1, 5000)
        assert c._lease_permission == original
    asyncio.run(run())


@pytest.mark.parametrize("action", ["charge", "discharge"])
@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("failure", ["rejected", "exception"])
def test_native_service_failure_is_propagated_without_marking_force_active(action, native, failure):
    tree = ast.parse((COMPONENT / "__init__.py").read_text())
    handler = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == f"handle_force_{action}")
    branch = next(n for n in handler.body if isinstance(n, ast.If) and isinstance(n.test, ast.Name) and n.test.id == "is_sungrow")
    class ControlError(Exception):
        pass
    class Coordinator:
        native_ihomemanager = native
        async def force_charge(self, *args, **kwargs):
            if failure == "exception":
                raise OSError("write failed")
            return False
        force_discharge = force_charge
    async def no_op(*args, **kwargs):
        return None
    async def guarded(factory):
        return await factory(5000)
    state = {"active": False}
    hass = SimpleNamespace(data={"power_sync": {"entry": {"sungrow_coordinator": Coordinator()}}},
                           async_create_task=lambda coro: coro.close())
    namespace = dict(is_sungrow=True, hass=hass, DOMAIN="power_sync", entry=SimpleNamespace(entry_id="entry"),
                     force_charge_state=dict(state), force_discharge_state=dict(state), source="optimizer",
                     duration=60, power_w=5000, command_power_w=5000,
                     _guarded_force_discharge_write=guarded, persist_force_mode_state=no_op,
                     _notify_api_error=no_op, async_dispatcher_send=lambda *args: None,
                     HomeAssistantError=ControlError,
                     _LOGGER=SimpleNamespace(info=lambda *args, **kwargs: None,
                                             error=lambda *args, **kwargs: None))
    function = ast.AsyncFunctionDef(name="invoke", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
                                   body=[branch], decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), "native-service-failure", "exec"), namespace)
    if native:
        with pytest.raises(ControlError, match="not confirmed"):
            asyncio.run(namespace["invoke"]())
    else:
        assert asyncio.run(namespace["invoke"]()) is None
    assert namespace[f"force_{action}_state"]["active"] is False


def test_runtime_factory_constructs_native_coordinator_with_option_endpoints(integration, monkeypatch):
    from types import ModuleType
    tree = ast.parse((COMPONENT / "__init__.py").read_text())
    branch = next(n for n in ast.walk(tree) if isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
                  and isinstance(n.test.left, ast.Name) and n.test.left.id == "sungrow_connection_type"
                  and any(isinstance(rhs, ast.Name) and rhs.id == "SUNGROW_CONNECTION_IHOMEMANAGER_NATIVE" for rhs in n.test.comparators))
    package = ModuleType("native_factory_test")
    package.__path__ = []
    module = ModuleType("native_factory_test.sungrow_ihomemanager_coordinator")
    module.SungrowIHomeManagerEnergyCoordinator = integration.ns["SungrowIHomeManagerEnergyCoordinator"]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    integration.entry.entry_id = "entry"
    integration.entry.options["sungrow_ihomemanager_host"] = "override.test"
    namespace = {**integration.ns, "__package__": package.__name__, "entry": integration.entry,
                 "hass": integration.hass, "sungrow_connection_type": "ihomemanager_native",
                 "sungrow_host": "inverter.test", "sungrow_port": 502, "sungrow_slave_id": 1,
                 "ac_inverter_source_id": None}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[branch], type_ignores=[])), "native-factory", "exec"), namespace)
    coordinator = namespace["sungrow_coordinator"]
    assert isinstance(coordinator, module.SungrowIHomeManagerEnergyCoordinator)
    assert coordinator._native_host == "override.test"
    assert coordinator.host == "inverter.test"
    assert coordinator._telemetry_only is True
