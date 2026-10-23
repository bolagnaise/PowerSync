"""Regression tests for manual force-mode duration options."""

from pathlib import Path
import ast
import asyncio
import logging
import math
import runpy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _const_duration_values() -> list[int]:
    tree = ast.parse((ROOT / "custom_components/power_sync/const.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "DISCHARGE_DURATIONS":
                    return ast.literal_eval(node.value)
    raise AssertionError("DISCHARGE_DURATIONS not found")


def test_force_mode_durations_accept_mobile_quarter_hour_values():
    durations = _const_duration_values()
    assert durations == [5, 10, *range(15, 241, 15)]
    assert 195 in durations


def _helpers():
    return runpy.run_path(str(ROOT / "custom_components/power_sync/force_discharge_duration.py"))


@pytest.mark.parametrize("value", [1, "7", 240, 255, 360, "421", 720, 1440, 30.0])
def test_custom_whole_minutes_are_not_replaced_with_default(value):
    assert _helpers()["validate_discharge_duration"](value) == int(value)


@pytest.mark.parametrize("value", [None, True, False, "", "invalid", "NaN", float("inf"),
                                  float("-inf"), 0, -1, 1.5, "1.5", 1441, 10**1000])
def test_invalid_duration_is_rejected(value):
    with pytest.raises(ValueError):
        _helpers()["validate_discharge_duration"](value)


def test_custom_value_is_persisted_separately_from_presets():
    resolve = _helpers()["selected_discharge_duration"]
    assert resolve("Custom", {"force_discharge_custom_duration": 421}) == 421
    assert resolve("195", {"force_discharge_custom_duration": 421}) == 195
    with pytest.raises(ValueError):
        resolve("Custom", {"force_discharge_custom_duration": "NaN"})


def _service_runtime():
    path = ROOT / "custom_components/power_sync/__init__.py"
    tree = ast.parse(path.read_text())
    handler = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "handle_force_discharge")

    class RemoveImports(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            return ast.copy_location(ast.Pass(), node)

    handler = RemoveImports().visit(handler)
    now = datetime(2026, 10, 24, 23, 55, tzinfo=timezone.utc)
    events, timers, snapshots = [], [], []
    state = {"active": False}
    generation = [0]
    entry = SimpleNamespace(entry_id="battery", options={"force_discharge_custom_duration": 421},
                            data={"CONF_SIGENERGY_STATION_ID": "site", "CONF_SIGENERGY_MODBUS_HOST": "fake"})

    class Controller:
        def __init__(self, **kwargs):
            pass

        async def force_discharge(self, **kwargs):
            events.append(("command", kwargs))
            return True

        async def disconnect(self):
            pass

    async def local_force(*args):
        return None

    async def persist():
        snapshots.append(dict(state))

    async def service_call(*args, **kwargs):
        events.append(("restore", args))

    def timer(hass, callback, expiry):
        timers.append((callback, expiry))
        return lambda: events.append(("cancel", expiry))

    def cancel_all(*args):
        if state.get("cancel_expiry_timer"):
            state.pop("cancel_expiry_timer")()

    namespace = {n.id: n.id for n in ast.walk(handler) if isinstance(n, ast.Name) and n.id.startswith(("CONF_", "BATTERY_SYSTEM_"))}
    hass = SimpleNamespace(data={"power_sync": {"battery": {}}}, services=SimpleNamespace(async_call=service_call))
    namespace.update({
        "Any": Any, "ServiceCall": object, "HomeAssistantError": ValueError,
        "DOMAIN": "power_sync", "DEFAULT_DISCHARGE_DURATION": 30,
        "DISCHARGE_DURATIONS": _const_duration_values(),
        "selected_discharge_duration": _helpers()["selected_discharge_duration"],
        "validate_native_discharge_duration": _helpers()["validate_native_discharge_duration"],
        "SigenergyController": Controller, "hass": hass, "entry": entry,
        "_control_call_source": lambda call: call.data.get("source", "user"),
        "_resolve_force_command_power_w": lambda *args: 1000,
        "_coerce_force_power_w": int, "_monitoring_mode_should_block_control": lambda call: False,
        "active_battery_system": "sigenergy", "local_battery_dispatch_configured": lambda _: False,
        "hold_soc_state": {}, "self_consumption_state": {}, "force_charge_state": {},
        "force_discharge_state": state, "_try_local_powerwall_force": local_force,
        "_get_tesla_site_configs": lambda *args: [], "_cancel_all_force_timers": cancel_all,
        "_command_generation": generation, "_tesla_reserve_generation": [0],
        "dt_util": SimpleNamespace(utcnow=lambda: now), "timedelta": timedelta,
        "_LOGGER": logging.getLogger(__name__), "persist_force_mode_state": persist,
        "async_dispatcher_send": lambda *args: events.append(("status", args[-1])),
        "async_track_point_in_utc_time": timer, "SERVICE_RESTORE_NORMAL": "restore_normal",
    })
    exec(compile(ast.fix_missing_locations(ast.Module(body=[handler], type_ignores=[])), str(path), "exec"), namespace)
    return namespace, now, events, timers, snapshots


@pytest.mark.parametrize("duration", ["255", "360", "Custom", 1440])
def test_custom_window_crosses_midnight_and_old_timer_cannot_restore_replacement(duration):
    ns, now, events, timers, snapshots = _service_runtime()
    call = SimpleNamespace(data={"duration": duration}, context=SimpleNamespace(user_id="user", parent_id=None))
    asyncio.run(ns["handle_force_discharge"](call))
    minutes = 421 if duration == "Custom" else int(duration)
    assert snapshots[-1]["duration"] == minutes
    assert snapshots[-1]["expires_at"] == now + timedelta(minutes=minutes)
    assert timers[-1][1] == now + timedelta(minutes=minutes)
    assert [e for e in events if e[0] == "command"]
    assert [e for e in events if e[0] == "status"][-1][1]["duration"] == minutes
    old_callback = timers[-1][0]
    asyncio.run(ns["handle_force_discharge"](call))
    assert [e for e in events if e[0] == "cancel"]
    asyncio.run(old_callback(now))
    assert not [e for e in events if e[0] == "restore"]
    asyncio.run(timers[-1][0](timers[-1][1]))
    assert [e for e in events if e[0] == "restore"][-1][1][1] == "restore_normal"


def test_invalid_or_monitoring_duration_has_no_command_or_timer_effect():
    ns, now, events, timers, snapshots = _service_runtime()
    call = SimpleNamespace(data={"duration": 0}, context=SimpleNamespace(user_id="user", parent_id=None))
    with pytest.raises(ValueError):
        asyncio.run(ns["handle_force_discharge"](call))
    assert not events and not timers and not snapshots
    ns["_monitoring_mode_should_block_control"] = lambda _: True
    call.data["duration"] = 421
    asyncio.run(ns["handle_force_discharge"](call))
    assert not events and not timers and not snapshots


def test_native_duration_preflight_checks_all_fleet_members_and_exact_options():
    validate = _helpers()["validate_native_discharge_duration"]
    states = {
        "number.duration_a": SimpleNamespace(attributes={"min": 1, "max": 1440, "step": 1}),
        "number.duration_b": SimpleNamespace(attributes={"min": 0, "max": 600, "step": 15}),
        "select.duration": SimpleNamespace(attributes={"options": ["Default", "360 minutes", "420", "1 hour"]}),
    }
    hass = SimpleNamespace(states=SimpleNamespace(get=states.get))
    first = SimpleNamespace(_entity_map={"dispatch_duration": "number.duration_a"})
    second = SimpleNamespace(_entity_map={"dispatch_duration": "number.duration_b"})
    runtime = {"neovolt_coordinator": SimpleNamespace(_controller=SimpleNamespace(_controllers=[first, second]))}
    validate(hass, runtime, 360)
    for minutes in (421, 720):
        with pytest.raises(ValueError, match="Neovolt"):
            validate(hass, runtime, minutes)
    states.pop("number.duration_b")
    with pytest.raises(ValueError, match="Neovolt"):
        validate(hass, runtime, 360)
    solax = SimpleNamespace(_control_profile="force_time", _entity_map={"export_duration": "select.duration"})
    validate(hass, {"solax_coordinator": SimpleNamespace(_controller=solax)}, 420)
    for minutes in (1, 421):
        with pytest.raises(ValueError, match="exact native option"):
            validate(hass, {"solax_coordinator": SimpleNamespace(_controller=solax)}, minutes)
    solax._control_profile = "remote_control"
    solax._entity_map = {"remotecontrol_autorepeat_duration": "number.seconds"}
    states["number.seconds"] = SimpleNamespace(attributes={"min": 0, "max": 65535, "step": 1})
    validate(hass, {"solax_coordinator": SimpleNamespace(_controller=solax)}, 1092)
    with pytest.raises(ValueError, match="SolaX"):
        validate(hass, {"solax_coordinator": SimpleNamespace(_controller=solax)}, 1093)
    validate(hass, {"foxess_coordinator": object()}, 1092)
    with pytest.raises(ValueError, match="1092"):
        validate(hass, {"foxess_coordinator": object()}, 1093)


def test_duration_entities_preserve_saved_choices_and_custom_number_after_reload():
    select_path = ROOT / "custom_components/power_sync/select.py"
    tree = ast.parse(select_path.read_text())
    duration_class = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PowerSyncDurationSelect")
    setup = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_setup_entry")
    setup.body = [n for n in setup.body if not isinstance(n, ast.ImportFrom)]
    entry = SimpleNamespace(entry_id="battery", domain="power_sync", options={"force_discharge_duration": "195"}, data={})
    registry = SimpleNamespace(async_get_entity_id=lambda *args: None)
    def update(current, *, options):
        current.options = options
    hass = SimpleNamespace(data={}, config_entries=SimpleNamespace(async_get_entry=lambda _: entry, async_update_entry=update))

    class Select:
        @property
        def options(self):
            return self._attr_options
        def async_write_ha_state(self):
            pass

    ns = {"Any": Any, "SelectEntity": Select, "ConfigEntry": object, "HomeAssistant": object,
          "AddEntitiesCallback": object, "DEFAULT_DISCHARGE_DURATION": 30,
          "DISCHARGE_DURATIONS": _const_duration_values(), "FORCE_DISCHARGE_DURATIONS": [*_const_duration_values(), *range(255, 361, 15)],
          "CUSTOM_DISCHARGE_DURATION": "Custom", "DOMAIN": "power_sync", "er": SimpleNamespace(async_get=lambda _: registry),
          "CONF_FORCE_CHARGE_DURATION": "force_charge_duration", "CONF_FORCE_DISCHARGE_DURATION": "force_discharge_duration",
          "CONF_HOLD_SELF_DURATION": "hold_self_duration", "CONF_TESLA_ENERGY_SITE_ID": "tesla_site"}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[duration_class, setup], type_ignores=[])), str(select_path), "exec"), ns)
    entities = []
    asyncio.run(ns["async_setup_entry"](hass, entry, entities.extend))
    charge, discharge, hold = entities
    for entity in entities:
        entity.hass = hass
    assert charge.options == hold.options == [str(x) for x in _const_duration_values()]
    assert discharge.options[-2:] == ["360", "Custom"]
    assert hold.current_option == discharge.current_option == "195"
    asyncio.run(discharge.async_select_option("Custom"))
    assert hold.current_option == "195" and charge.current_option == "30"
    assert hass.data["power_sync"]["battery"]["_skip_reload"]

    number_path = ROOT / "custom_components/power_sync/number.py"
    number_class = next(n for n in ast.parse(number_path.read_text()).body if isinstance(n, ast.ClassDef) and n.name == "ForceDischargeCustomDurationNumber")
    ns.update(_helpers())
    ns.update({"NumberEntity": Select, "NumberMode": SimpleNamespace(BOX="box")})
    exec(compile(ast.fix_missing_locations(ast.Module(body=[number_class], type_ignores=[])), str(number_path), "exec"), ns)
    number = ns["ForceDischargeCustomDurationNumber"](hass, entry)
    asyncio.run(number.async_set_native_value(421))
    reloaded = ns["ForceDischargeCustomDurationNumber"](hass, entry)
    assert reloaded.native_value == 421
    hass.data["power_sync"]["battery"].pop("_skip_reload")
    asyncio.run(reloaded.async_set_native_value(421))
    assert "_skip_reload" not in hass.data["power_sync"]["battery"]
    with pytest.raises(ValueError):
        asyncio.run(number.async_set_native_value(float("nan")))
    assert reloaded.native_value == 421


@pytest.mark.parametrize("expired", [False, True])
def test_custom_restart_replays_remaining_window_or_preserves_failed_expired_cleanup(expired):
    ns, now, events, timers, snapshots = _service_runtime()
    path = ROOT / "custom_components/power_sync/__init__.py"
    tree = ast.parse(path.read_text())
    function = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "restore_force_mode_from_persistence")
    ns.update({n.id: n.id for n in ast.walk(function) if isinstance(n, ast.Name) and n.id.startswith("BATTERY_SYSTEM_")})
    expiry = now + timedelta(minutes=-10 if expired else 411)
    persisted = {"mode": "discharge", "source": "user", "duration": 421, "expires_at": expiry.isoformat(), "power_w": 1000}

    class Store:
        data = {"force_mode_state": persisted}
        async def async_load(self):
            return dict(self.data)
        async def async_save(self, data):
            self.data = data

    store = Store()
    async def service_call(domain, service, data, **kwargs):
        events.append((service, data))
        if expired:
            raise RuntimeError("restore unconfirmed")

    ns["hass"].services.async_call = service_call
    ns.update({"persisted_force_state": persisted, "persisted_pending_restore": None,
               "datetime": datetime, "math": math, "is_foxess": False, "store": store,
               "_uses_native_battery_integration": lambda _: False,
               "_is_monitoring_mode": lambda: False,
               "_select_restorable_tesla_tariff": lambda *args: None,
               "_cached_restorable_tesla_tariff": lambda: None,
               "_configured_restorable_tesla_tariff": lambda: None,
               "SERVICE_FORCE_DISCHARGE": "force_discharge"})
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), str(path), "exec"), ns)
    asyncio.run(ns["restore_force_mode_from_persistence"]())
    if expired:
        assert events[-1][0] == "restore_normal"
        assert store.data["force_mode_state"] == persisted
        assert ns["force_discharge_state"]["active"]
    else:
        assert ("force_discharge", {"duration": 411, "source": "user", "power_w": 1000}) in events
        assert timers[-1][1] == expiry
        assert ns["force_discharge_state"]["duration"] == 421
