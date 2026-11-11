"""Exercise shared-client EMS commands, durable recovery and device fencing."""
from __future__ import annotations

import asyncio
import ast
import copy
import importlib
import logging
import sys
from types import ModuleType, SimpleNamespace as NS
from pathlib import Path

import pytest

from test_goodwe_battery_controller import _load_goodwe_controller_module
from test_goodwe_ems_load_following import _EmsInverter


class Inverter(_EmsInverter):
    serial_number = "TEST-ESA-001"
    model_name = "GW9.999K-EHA-G20"
    rated_power = 9999

    def __init__(self):
        super().__init__(ems_power=321)
        self.registers.update({35187: 1, 47509: 0, 47510: 5000})
        self.events = []
        self.dod = 90
        self.on_write = None
        self.read_error = False

    def settings(self):
        return [NS(id_=name, offset=address, unit="W" if name == "ems_power_limit" else "") for name, address in self._NAMES.items()]

    async def read_setting(self, name):
        if self.read_error:
            raise TimeoutError("no readback")
        self.events.append(("read", name))
        return await super().read_setting(name)

    def _read_command(self, address, count):
        return NS(offset=address, count=count, read=True)

    async def _read_from_socket(self, frame):
        if getattr(frame, "read", False):
            if self.read_error:
                raise TimeoutError("no readback")
            data = b"".join(self.registers[n].to_bytes(2, "big") for n in range(frame.offset, frame.offset + frame.count))
            self.events.append(("pair_read", frame.offset))
            return NS(response_data=lambda: data)
        self.events.append(("write", frame.offset))
        await super()._read_from_socket(frame)
        if self.on_write:
            await self.on_write()

    async def get_ongrid_battery_dod(self):
        return self.dod

    async def set_ongrid_battery_dod(self, value):
        self.events.append(("dod", value))
        self.dod = value

    async def disconnect(self):
        raise AssertionError("PowerSync must not disconnect upstream")


@pytest.fixture
def env(monkeypatch):
    _module, restore = _load_goodwe_controller_module()
    previous = sys.modules.pop("power_sync.inverters.goodwe_shared", None)
    module = importlib.import_module("power_sync.inverters.goodwe_shared")
    inverter = Inverter()
    entities = {name: NS(config_entry_id="upstream", device_id="device", platform="goodwe") for name in (
        "select.goodwe_ems_mode", "number.goodwe_ems_power_limit", "sensor.goodwe_battery_soc",
    )}
    devices = {"device": NS(identifiers={("goodwe", inverter.serial_number)})}
    registry = NS(async_get=entities.get)
    helpers = ModuleType("homeassistant.helpers")
    helpers.entity_registry = NS(async_get=lambda hass: registry)
    helpers.device_registry = NS(async_get=lambda hass: NS(async_get=devices.get))
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers)
    entry = NS(domain="goodwe", state=NS(value="loaded"), runtime_data=NS(inverter=inverter))
    states = {
        "select.goodwe_ems_mode": NS(state="auto", attributes={"options": ["auto", "buy_power", "charge_pv", "charge_battery", "discharge_battery", "sell_power", "conserve"]}),
        "number.goodwe_ems_power_limit": NS(state=321, attributes={"min": 0, "max": 19998}),
    }
    hass = NS(config_entries=NS(async_get_entry=lambda key: entry if key == "upstream" else None),
              data={}, states=NS(get=states.get))
    telemetry = NS(telemetry_ready=lambda: True,
                   control_binding_entities=lambda: ("sensor.goodwe_battery_soc",),
                   get_runtime_data=lambda: {"telemetry_ready": True})
    allowed = [True]
    saves = []

    async def persist(state):
        inverter.events.append(("save", copy.deepcopy(state)))
        saves.append(copy.deepcopy(state))

    def make(method="ems_load_following"):
        controller = module.GoodWeSharedController(hass, "goodwe", telemetry,
            curtailment_method=method, control_allowed=lambda: allowed[0])
        controller.set_restore_persistence(persist, lambda: allowed[0])
        return controller

    result = NS(module=module, inverter=inverter, entities=entities, devices=devices,
                entry=entry, states=states, hass=hass, allowed=allowed, saves=saves,
                persist=persist, make=make)
    result.controller = make()
    yield result
    if previous is None:
        sys.modules.pop("power_sync.inverters.goodwe_shared", None)
    else:
        sys.modules["power_sync.inverters.goodwe_shared"] = previous
    restore()


def run(coro):
    return asyncio.run(coro)


def test_buy_power_writes_zero_atomically_after_durable_identity_snapshot(env):
    assert run(env.controller.curtail())
    assert env.inverter.frames == [(47511, [9, 0])]
    assert env.saves[-1]["binding"] == {"config_entry_id": "upstream", "device_id": "device", "serial": "TEST-ESA-001"}
    assert env.saves[0]["write_attempted"] is False
    assert env.saves[-1]["write_attempted"] is True
    assert [e[0] for e in env.inverter.events].index("save") < [e[0] for e in env.inverter.events].index("write")
    # Optimistically unchanged HA state does not affect hardware verification.
    assert env.states["select.goodwe_ems_mode"].state == "auto"


@pytest.mark.parametrize("method,address,expected", [("ems_load_following", 47511, [1, 321]), ("export_limit", 47509, [0, 5000])])
def test_both_curtailment_methods_restore_after_restart_and_method_change(env, method, address, expected):
    original = env.make(method)
    assert run(original.curtail())
    snapshot = copy.deepcopy(env.saves[-1])
    restarted = env.make("export_limit" if method == "ems_load_following" else "ems_load_following")
    assert restarted.restore_grid_export_restore_state(snapshot)
    assert run(restarted.restore())
    assert env.inverter.frames[-1] == (address, expected)
    assert env.saves[-1] is None


@pytest.mark.parametrize("failure", ["save", "fallback", "offgrid", "foreign", "readback", "not_allowed", "wrong_map", "missing_fc16"])
def test_preconditions_fail_closed(env, failure):
    if failure == "save":
        async def persist(state):
            raise OSError("disk failure")
        env.controller.set_restore_persistence(persist, lambda: True)
    elif failure == "fallback":
        env.inverter.registers[47505] = 3
    elif failure == "offgrid":
        env.inverter.registers[35187] = 2
    elif failure == "foreign":
        env.inverter.registers[47511] = 12
    elif failure == "readback":
        env.inverter.read_error = True
    elif failure == "not_allowed":
        env.allowed[0] = False
    elif failure == "wrong_map":
        env.inverter.settings = lambda: [NS(id_="ems_mode", offset=42)]
    elif failure == "missing_fc16":
        env.inverter._write_multi_command = None
    assert run(env.controller.curtail()) is False
    assert env.inverter.frames == []


@pytest.mark.parametrize("foreign", [(1, 0), (9, 0), (12, 4000)])
def test_refused_takeover_never_restores_external_state(env, foreign):
    async def changed_after_save(state):
        await env.persist(state)
        env.inverter.registers[47511], env.inverter.registers[47512] = foreign
    env.controller.set_restore_persistence(changed_after_save, lambda: True)
    assert run(env.controller.curtail()) is False
    assert env.inverter.frames == []
    assert run(env.controller.restore())
    assert env.inverter.frames == []
    assert (env.inverter.registers[47511], env.inverter.registers[47512]) == foreign


def test_runtime_replaced_during_snapshot_save_gives_zero_writes(env):
    async def replaced(state):
        await env.persist(state)
        env.entry.runtime_data.inverter = Inverter()
    env.controller.set_restore_persistence(replaced, lambda: True)
    assert run(env.controller.curtail()) is False
    assert env.inverter.frames == []


@pytest.mark.parametrize("mismatch", ["device", "entry", "serial", "unloaded", "telemetry"])
def test_wrong_or_unloaded_upstream_identity_blocks_every_command(env, mismatch):
    if mismatch == "device":
        env.entities["number.goodwe_ems_power_limit"].device_id = "different"
    elif mismatch == "entry":
        env.entities["number.goodwe_ems_power_limit"].config_entry_id = "different"
    elif mismatch == "serial":
        env.inverter.serial_number = "OTHER"
    elif mismatch == "unloaded":
        env.entry.state.value = "not_loaded"
    elif mismatch == "telemetry":
        env.entities["sensor.goodwe_battery_soc"].device_id = "other"
    assert env.controller.control_surface_ready() is False
    assert run(env.controller.curtail()) is False
    assert run(env.controller.set_ems_mode("charge_pv", 1000)) is False
    assert run(env.controller.set_backup_reserve(20)) is False
    assert env.inverter.frames == []
    assert not any(e[0] == "dod" for e in env.inverter.events)


def test_lost_ack_and_cancellation_recover_from_durable_write_intent(env):
    async def cancel_after_apply():
        raise asyncio.CancelledError()
    env.inverter.on_write = cancel_after_apply
    with pytest.raises(asyncio.CancelledError):
        run(env.controller.curtail())
    assert env.inverter.registers[47511] == 9
    restarted = env.make()
    assert restarted.restore_grid_export_restore_state(env.saves[-1])
    env.inverter.on_write = None
    assert run(restarted.restore())
    assert env.inverter.frames[-1] == (47511, [1, 321])


def test_missing_register_readback_retains_recovery_claim(env):
    assert run(env.controller.curtail())
    env.inverter.read_error = True
    assert run(env.controller.restore()) is False
    assert env.controller.get_grid_export_restore_state() is not None
    assert len(env.inverter.frames) == 1


def test_failed_clear_keeps_claim_and_retries_without_another_restore_write(env):
    assert run(env.controller.curtail())
    async def persist(state):
        if state is None:
            raise OSError("disk failure")
        await env.persist(state)
    env.controller.set_restore_persistence(persist, lambda: True)
    assert run(env.controller.restore()) is False
    assert env.controller.get_grid_export_restore_state() is not None
    assert len(env.inverter.frames) == 2
    env.controller.set_restore_persistence(env.persist, lambda: True)
    assert run(env.controller.restore())
    assert len(env.inverter.frames) == 2


def test_foreign_command_is_not_overwritten_on_restore_or_recurtail(env):
    assert run(env.controller.curtail())
    env.inverter.registers[47511], env.inverter.registers[47512] = 12, 4000
    assert run(env.controller.curtail()) is False
    assert len(env.inverter.frames) == 1
    assert env.saves[-1] is None
    assert run(env.controller.curtail()) is False
    assert len(env.inverter.frames) == 1


def test_saved_binding_cannot_migrate_to_another_device_or_legacy_route(env):
    assert run(env.controller.curtail())
    snapshot = copy.deepcopy(env.saves[-1])
    snapshot["binding"]["config_entry_id"] = "old-entry"
    restarted = env.make()
    assert restarted.restore_grid_export_restore_state(snapshot)
    assert run(restarted.restore()) is False
    assert restarted.get_grid_export_restore_state() == snapshot
    legacy = env.make()
    assert legacy.restore_grid_export_restore_state({"method": "ems_load_following", "ems_mode": 1, "ems_power_limit": 321})
    assert run(legacy.set_ems_mode("auto", 0, reset_power_limit=True)) is False
    assert len(env.inverter.frames) == 1


@pytest.mark.parametrize("option,power,expected", [("charge_pv", 1500, [2, 1500]), ("discharge_battery", 1400, [12, 1400]), ("conserve", 0, [6, 321]), ("auto", 0, [1, 9999])])
def test_battery_commands_verify_registers_and_release_own_hold(env, option, power, expected):
    assert run(env.controller.curtail())
    assert run(env.controller.set_ems_mode(option, power, reset_power_limit=option == "auto"))
    assert env.inverter.frames[-2] == (47511, [1, 321])
    assert env.inverter.frames[-1] == (47511, expected)


def test_unconfirmed_write_does_not_succeed_from_optimistic_entity_state(env):
    env.inverter.accept_writes = False
    env.states["select.goodwe_ems_mode"].state = "charge_pv"
    env.states["number.goodwe_ems_power_limit"].state = 1500
    assert run(env.controller.set_ems_mode("charge_pv", 1500)) is False


@pytest.mark.parametrize("reserve", [0, 10, 50, 100])
def test_reserve_uses_inverted_on_grid_dod_and_verified_readback(env, reserve):
    assert run(env.controller.set_backup_reserve(reserve))
    assert env.inverter.dod == 100 - reserve
    assert run(env.controller.get_backup_reserve()) == reserve
    writes = len(env.inverter.events)
    assert run(env.controller.set_backup_reserve(reserve))
    assert len(env.inverter.events) == writes  # No EEPROM rewrite for an unchanged reserve.


def test_sharing_does_not_open_poll_or_disconnect_the_upstream_transport(env):
    assert run(env.controller.get_runtime_data())["rated_power_w"] == 9999
    assert env.inverter.events == []
    assert run(env.controller.curtail())
    run(env.controller.disconnect())
    assert env.inverter.registers[47511] == 9


def test_charge_and_curtailment_are_serialized_without_retakeover(env):
    async def scenario():
        first_write = asyncio.Event()
        finish = asyncio.Event()
        async def pause():
            first_write.set()
            await finish.wait()
        env.inverter.on_write = pause
        curtail = asyncio.create_task(env.controller.curtail())
        await first_write.wait()
        charge = asyncio.create_task(env.controller.set_ems_mode("charge_pv", 1500))
        await asyncio.sleep(0)
        assert env.inverter.frames == [(47511, [9, 0])]
        env.inverter.on_write = None
        finish.set()
        assert await curtail
        assert await charge
        assert env.inverter.frames[-1] == (47511, [2, 1500])
        assert await env.controller.curtail() is False
    run(scenario())


def test_monitoring_handoff_during_write_ahead_save_sends_no_command(env):
    async def changed(state):
        await env.persist(state)
        if state and state["write_attempted"]:
            env.allowed[0] = False
    env.controller.set_restore_persistence(changed, lambda: env.allowed[0])
    assert run(env.controller.curtail()) is False
    assert env.inverter.frames == []
    assert run(env.controller.restore())
    assert env.inverter.frames == []


def test_owned_cleanup_can_restore_while_new_commands_are_blocked(env):
    assert run(env.controller.curtail())
    env.allowed[0] = False
    assert run(env.controller.set_ems_mode("charge_pv", 1500)) is False
    assert run(env.controller.restore())
    assert env.inverter.frames[-1] == (47511, [1, 321])


def test_change_during_final_write_ahead_save_refuses_without_restoration(env):
    async def changed(state):
        await env.persist(state)
        if state and state["write_attempted"]:
            env.inverter.registers[47511], env.inverter.registers[47512] = 9, 0
    env.controller.set_restore_persistence(changed, lambda: True)
    assert run(env.controller.curtail()) is False
    assert run(env.controller.restore())
    assert env.inverter.frames == []


def test_explicit_zero_target_uses_durable_curtailment_lifecycle(env):
    assert run(env.controller.set_ems_mode("buy_power", 0))
    assert env.inverter.frames[-1] == (47511, [9, 0])
    assert env.saves[-1]["write_attempted"] is True


@pytest.mark.parametrize("kind", ["no_buy_power", "nonzero_min", "percent_units"])
def test_unsupported_buy_power_surface_has_zero_writes(env, kind):
    if kind == "no_buy_power":
        env.states["select.goodwe_ems_mode"].attributes["options"].remove("buy_power")
    elif kind == "nonzero_min":
        env.states["number.goodwe_ems_power_limit"].attributes["min"] = 100
    else:
        previous = env.inverter.settings
        def settings():
            values = previous()
            for setting in values:
                if setting.id_ == "ems_power_limit":
                    setting.unit = "%"
            return values
        env.inverter.settings = settings
    assert run(env.controller.curtail()) is False
    assert env.inverter.frames == []


def test_discharge_fallback_must_be_advertised(env):
    options = env.states["select.goodwe_ems_mode"].attributes["options"]
    options.remove("discharge_battery")
    assert run(env.controller.set_ems_mode("discharge_battery", 1000, fallback_option="sell_power"))
    assert env.inverter.frames[-1] == (47511, [10, 1000])
    options.remove("sell_power")
    assert run(env.controller.set_ems_mode("discharge_battery", 1000, fallback_option="sell_power")) is False
    assert len(env.inverter.frames) == 1


def test_reserve_acknowledgement_without_readback_is_not_success(env):
    async def ignored(value):
        pass
    env.inverter.set_ongrid_battery_dod = ignored
    assert run(env.controller.set_backup_reserve(50)) is False


def test_legacy_upstream_runtime_data_location_uses_same_bound_client(env):
    env.hass.data["goodwe"] = {"upstream": env.entry.runtime_data}
    env.entry.runtime_data = None
    assert run(env.controller.curtail())
    assert env.controller._inverter is env.inverter


def test_actual_coordinator_routes_all_native_commands_through_verified_shared_client(env):
    path = Path(__file__).parents[1] / "custom_components/power_sync/coordinator.py"
    cls = next(n for n in ast.parse(path.read_text()).body if isinstance(n, ast.ClassDef) and n.name == "GoodWeEnergyCoordinator")
    names = {"_ems_set_mode", "force_charge", "force_discharge", "set_backup_mode", "restore_normal", "set_backup_reserve"}
    methods = [n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name in names]
    extracted = ast.fix_missing_locations(ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        ast.ClassDef(name="Coordinator", bases=[], keywords=[], body=methods, decorator_list=[]),
    ], type_ignores=[]))
    namespace = {}
    exec(compile(extracted, str(path), "exec"), namespace)
    coordinator = namespace["Coordinator"]()
    coordinator._controller = env.controller
    coordinator._ems_prefix = "goodwe"
    coordinator._connected = False
    coordinator.data = {"rated_power_w": 9999}
    coordinator._native_control_allowed = lambda operation: env.allowed[0]
    async def scenario():
        assert await coordinator.force_charge(power_w=1500)
        assert env.inverter.frames[-1] == (47511, [2, 1500])
        assert await coordinator.force_discharge(power_w=2000)
        assert env.inverter.frames[-1] == (47511, [12, 2000])
        assert await coordinator.set_backup_mode()
        assert env.inverter.frames[-1] == (47511, [6, 2000])
        assert await coordinator.restore_normal()
        assert env.inverter.frames[-1] == (47511, [1, 9999])
        assert await coordinator.set_backup_reserve(20)
        assert env.inverter.dod == 80
    run(scenario())


def test_actual_setup_persistence_callback_requires_verified_durable_save(env):
    path = Path(__file__).parents[1] / "custom_components/power_sync/__init__.py"
    node = next(n for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_persist_goodwe_shared_restore")
    calls = []
    async def failed_save(store, changes, **kwargs):
        calls.append((changes, kwargs))
        return False
    namespace = {"store": object(), "_update_powerwall_store": failed_save}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), str(path), "exec"), namespace)
    env.controller.set_restore_persistence(namespace["_persist_goodwe_shared_restore"], lambda: True)
    assert run(env.controller.curtail()) is False
    assert calls[0][1]["verify"] is True
    assert env.inverter.frames == []


@pytest.mark.parametrize("original,new,address,baseline", [
    ("export_limit", "ems_load_following", 47509, [0, 5000]),
    ("ems_load_following", "export_limit", 47511, [1, 321]),
])
def test_method_change_releases_original_hold_before_new_curtailment(env, original, new, address, baseline):
    controller = env.make(original)
    assert run(controller.curtail())
    restarted = env.make(new)
    assert restarted.restore_grid_export_restore_state(env.saves[-1])
    assert run(restarted.curtail()) is False
    assert env.inverter.frames[-1] == (address, baseline)
    assert restarted.get_grid_export_restore_state() is None
    assert run(restarted.curtail())


def test_explicit_export_limit_releases_owned_curtailment_first(env):
    controller = env.make("export_limit")
    assert run(controller.curtail())
    assert run(controller.set_grid_export_limit(5000))
    assert env.inverter.registers[47509] == 0
    assert env.inverter.registers[47510] == 5000
    assert controller.get_grid_export_restore_state() is None


def test_unproven_recovery_blocks_controls_but_not_telemetry_metadata(env):
    snapshot = {"method": "ems_load_following", "ems_mode": 1, "ems_power_limit": 321}
    assert env.controller.restore_grid_export_restore_state(snapshot)
    assert run(env.controller.get_runtime_data())["telemetry_ready"] is True
    assert run(env.controller.set_ems_mode("charge_pv", 1000)) is False
    assert env.controller.get_grid_export_restore_state() == snapshot


@pytest.mark.parametrize("option", ["charge_pv", "charge_battery", "discharge_battery", "sell_power"])
def test_zero_power_battery_command_never_reuses_an_old_target(env, option):
    assert run(env.controller.set_ems_mode(option, 0)) is False
    assert env.inverter.frames == []


def test_missing_fc16_does_not_arm_recovery_for_a_refused_write(env):
    env.inverter._write_multi_command = None
    assert run(env.controller.curtail()) is False
    assert env.controller.get_grid_export_restore_state() is None
    assert env.inverter.frames == []


@pytest.mark.parametrize("option", ["charge_pv", "charge_battery", "conserve"])
def test_export_limiting_can_coexist_with_our_verified_charge_or_hold(env, option):
    controller = env.make("export_limit")
    assert run(controller.set_ems_mode(option, 1000 if option != "conserve" else 0))
    owned_mode = env.inverter.registers[47511]
    assert run(controller.curtail())
    assert env.inverter.frames[-1] == (47509, [1, 0])
    assert env.inverter.registers[47511] == owned_mode
    assert run(controller.curtail())
    assert run(controller.restore())
    assert env.inverter.registers[47511] == owned_mode


@pytest.mark.parametrize("option", ["charge_pv", "conserve", "discharge_battery"])
def test_buy_power_cannot_replace_our_charge_hold_or_export_command(env, option):
    assert run(env.controller.set_ems_mode(option, 1000 if option != "conserve" else 0))
    count = len(env.inverter.frames)
    assert run(env.controller.curtail()) is False
    assert len(env.inverter.frames) == count


def test_export_limiting_does_not_claim_an_external_charge_command(env):
    controller = env.make("export_limit")
    env.inverter.registers[47511], env.inverter.registers[47512] = 2, 1000
    assert run(controller.curtail()) is False
    assert env.inverter.frames == []


def test_restart_during_charge_releases_owned_export_bank_without_touching_ems(env):
    controller = env.make("export_limit")
    assert run(controller.set_ems_mode("charge_pv", 1000))
    assert run(controller.curtail())
    restarted = env.make("export_limit")
    assert restarted.restore_grid_export_restore_state(env.saves[-1])
    assert run(restarted.curtail()) is False
    assert env.inverter.frames[-1] == (47509, [0, 5000])
    assert env.inverter.registers[47511] == 2
    assert restarted.get_grid_export_restore_state() is None


def test_nameplate_discovery_retries_late_shared_runtime_without_hardware_polling(env):
    path = Path(__file__).parents[1] / "custom_components/power_sync/coordinator.py"
    cls = next(n for n in ast.parse(path.read_text()).body if isinstance(n, ast.ClassDef) and n.name == "GoodWeEnergyCoordinator")
    node = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_probe_entity_telemetry_rated_power")
    namespace = {"asyncio": asyncio, "_LOGGER": logging.getLogger(__name__)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), str(path), "exec"), namespace)
    coordinator = NS(_controller=env.controller, _entity_telemetry_rated_power_probe_attempted=False,
                     _entity_telemetry_rated_power_w=None)
    env.entry.state.value = "setup_in_progress"
    assert run(namespace[node.name](coordinator)) is None
    assert coordinator._entity_telemetry_rated_power_probe_attempted is False
    env.entry.state.value = "loaded"
    assert run(namespace[node.name](coordinator)) == 9999
    assert run(namespace[node.name](coordinator)) == 9999
    assert coordinator._entity_telemetry_rated_power_probe_attempted is True
    assert env.inverter.events == []
