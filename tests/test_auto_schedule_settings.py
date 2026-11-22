"""Tests for auto-schedule settings serialization and clearing."""

from __future__ import annotations

import ast
import asyncio
import logging
import importlib
import sys
import types
from unittest.mock import AsyncMock
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"

sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))

_ha_root = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
_ha_core = sys.modules.setdefault("homeassistant.core", types.ModuleType("homeassistant.core"))
_ha_config_entries = sys.modules.setdefault(
    "homeassistant.config_entries", types.ModuleType("homeassistant.config_entries")
)
_ha_exceptions = sys.modules.setdefault(
    "homeassistant.exceptions", types.ModuleType("homeassistant.exceptions")
)
_ha_helpers = sys.modules.setdefault("homeassistant.helpers", types.ModuleType("homeassistant.helpers"))
_ha_storage = sys.modules.setdefault(
    "homeassistant.helpers.storage", types.ModuleType("homeassistant.helpers.storage")
)
_ha_update = sys.modules.setdefault(
    "homeassistant.helpers.update_coordinator",
    types.ModuleType("homeassistant.helpers.update_coordinator"),
)
_ha_event = sys.modules.setdefault(
    "homeassistant.helpers.event", types.ModuleType("homeassistant.helpers.event")
)
_ha_util = sys.modules.setdefault("homeassistant.util", types.ModuleType("homeassistant.util"))
_ha_dt = sys.modules.setdefault("homeassistant.util.dt", types.ModuleType("homeassistant.util.dt"))
_ha_core.HomeAssistant = type("HomeAssistant", (), {})
_ha_config_entries.ConfigEntry = type("ConfigEntry", (), {})
_ha_exceptions.ConfigEntryNotReady = type("ConfigEntryNotReady", (Exception,), {})
_ha_storage.Store = type("Store", (), {"__init__": lambda self, *args, **kwargs: None})
_ha_update.DataUpdateCoordinator = type(
    "DataUpdateCoordinator",
    (),
    {
        "__class_getitem__": classmethod(lambda cls, item: cls),
        "__init__": lambda self, *args, **kwargs: None,
    },
)
_ha_event.async_track_time_change = lambda *args, **kwargs: (lambda: None)
_ha_helpers.storage = _ha_storage
_ha_helpers.update_coordinator = _ha_update
_ha_helpers.event = _ha_event
_ha_dt.now = getattr(_ha_dt, "now", lambda *args, **kwargs: None)
_ha_util.dt = _ha_dt
_ha_root.helpers = _ha_helpers
_ha_root.util = _ha_util

_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps

_optimization = types.ModuleType("power_sync.optimization")
_optimization.__path__ = [str(ROOT / "optimization")]
sys.modules["power_sync.optimization"] = _optimization

_automations = types.ModuleType("power_sync.automations")
_automations.__path__ = [str(ROOT / "automations")]
sys.modules["power_sync.automations"] = _automations

sys.modules.pop("power_sync.const", None)
ev_planner = importlib.import_module("power_sync.automations.ev_charging_planner")


def test_optimizer_battery_params_do_not_snapshot_grid_import_limit():
    config = types.SimpleNamespace(
        max_charge_w=7160,
        max_discharge_w=10000,
        max_grid_import_w=14900,
    )
    hass = types.SimpleNamespace(
        data={
            "power_sync": {
                "entry-1": {
                    "optimization_coordinator": types.SimpleNamespace(_config=config),
                }
            }
        }
    )
    entry = types.SimpleNamespace(entry_id="entry-1")

    assert ev_planner._get_optimizer_battery_params(hass, entry) == {
        "max_inverter_kw": 10.0,
        "max_battery_charge_rate_kw": 7.16,
    }


def test_optimizer_battery_params_keep_legacy_battery_limits_without_grid_limit():
    config = types.SimpleNamespace(max_charge_w=7160, max_discharge_w=10000)
    hass = types.SimpleNamespace(
        data={
            "power_sync": {
                "entry-1": {
                    "optimization_coordinator": types.SimpleNamespace(_config=config),
                }
            }
        }
    )
    entry = types.SimpleNamespace(entry_id="entry-1")

    assert ev_planner._get_optimizer_battery_params(hass, entry) == {
        "max_inverter_kw": 10.0,
        "max_battery_charge_rate_kw": 7.16,
    }


def test_empty_departure_times_does_not_rehydrate_legacy_schedule():
    settings = ev_planner.AutoScheduleSettings.from_dict({
        "departure_time": "07:30",
        "departure_days": [0, 1, 2, 3, 4],
        "departure_times": {},
    })

    assert settings.departure_times == {}
    assert settings.to_dict()["departure_time"] is None
    assert settings.to_dict()["departure_days"] == []


def test_legacy_departure_edit_round_trips_and_empty_weekdays_clear_the_deadline():
    executor = object.__new__(ev_planner.AutoScheduleExecutor)
    executor._settings, executor._state = {}, {}
    current = executor.update_settings("car", {"departure_time": "06:30", "departure_days": [1, 5]})
    restored = ev_planner.AutoScheduleSettings.from_dict(current.to_dict())
    assert restored.departure_times == {1: "06:30", 5: "06:30"}
    current = executor.update_settings("car", {"departure_days": []})
    assert current.departure_time is None and current.to_dict()["departure_times"] == {}


def test_empty_per_day_overrides_clear_legacy_aliases():
    settings = ev_planner.AutoScheduleSettings.from_dict({
        "departure_priorities": {},
        "departure_min_battery_to_start": {},
        "departure_home_battery_min": {"0": 80},
        "departure_limit_grid_import": {},
        "departure_no_grid_import": {"0": True},
        "departure_consume_battery_level": {},
        "departure_stop_at_battery_floor": {},
    })

    assert settings.departure_priorities == {}
    assert settings.departure_min_battery_to_start == {}
    assert settings.departure_limit_grid_import == {}
    assert settings.departure_consume_battery_level == {}
    assert settings.departure_stop_at_battery_floor == {}


def test_generic_status_entity_round_trips_with_auto_schedule_settings():
    settings = ev_planner.AutoScheduleSettings.from_dict({
        "charger_type": "generic",
        "charger_switch_entity": "switch.garage_ev",
        "charger_amps_entity": "number.garage_ev_current",
        "charger_status_entity": "sensor.garage_ev_status",
    })

    assert settings.charger_status_entity == "sensor.garage_ev_status"
    assert settings.to_dict()["charger_status_entity"] == "sensor.garage_ev_status"


@pytest.mark.parametrize("invalid", [None, True, False, "5", [], {}, float("nan"), float("inf"), -float("inf"), 10**500])
@pytest.mark.parametrize("field", ["max_grid_price_cents", "departure_max_grid_price_cents"])
def test_smart_schedule_rejects_invalid_grid_caps_without_partial_update(invalid, field):
    executor = object.__new__(ev_planner.AutoScheduleExecutor)
    executor._settings, executor._state = {}, {}
    settings = executor.update_settings("car", {"target_soc": 77, "max_grid_price_cents": -3})
    value = {"0": invalid} if field.startswith("departure_") else invalid
    with pytest.raises(ValueError, match="grid_price"):
        executor.update_settings("car", {"target_soc": 90, field: value})
    assert settings.target_soc == 77
    assert settings.max_grid_price_cents == -3


@pytest.mark.parametrize("invalid", [None, [], {"7": 4}, {"-1": 4}, {"1.2": 4}, {True: 4}])
def test_smart_schedule_rejects_malformed_grid_cap_weekday_map(invalid):
    executor = object.__new__(ev_planner.AutoScheduleExecutor)
    executor._settings, executor._state = {}, {}
    with pytest.raises(ValueError, match="departure_max_grid_price"):
        executor.update_settings("car", {"departure_max_grid_price_cents": invalid})


def test_smart_schedule_caps_round_trip_per_vehicle_weekday_and_clear():
    executor = object.__new__(ev_planner.AutoScheduleExecutor)
    executor._settings, executor._state = {}, {}
    settings = executor.update_settings("car", {
        "max_grid_price_cents": -2.25,
        "departure_max_grid_price_cents": {"0": 0, "4": 1.125},
        "charger_power_entity": "sensor.garage_charge_power",
    })
    executor.update_settings("car", {"target_soc": 89})
    assert executor.get_settings("other").get_effective_max_grid_price(0) == 25
    saved = settings.to_dict()
    assert saved["departure_max_grid_price_cents"] == {"0": 0, "4": 1.125}
    restored = ev_planner.AutoScheduleSettings.from_dict(saved)
    assert restored.charger_power_entity == "sensor.garage_charge_power"
    assert [restored.get_effective_max_grid_price(d) for d in (0, 3, 4)] == [0, -2.25, 1.125]
    executor.update_settings("car", {"departure_max_grid_price_cents": {}})
    assert settings.get_effective_max_grid_price(0) == -2.25


def test_smart_schedule_restores_legacy_caps_with_a_finite_non_paid_fallback():
    restore = ev_planner.AutoScheduleSettings.from_dict
    assert restore({}).max_grid_price_cents == 25
    assert restore({"max_grid_price_cents": "-3.5"}).max_grid_price_cents == -3.5
    for invalid in (None, True, "no", float("inf"), float("nan")):
        settings = restore({"max_grid_price_cents": invalid})
        assert settings.get_effective_max_grid_price(0) == 0
    settings = restore({"max_grid_price_cents": -3, "departure_max_grid_price_cents": {"1": "bad", "3": 8}})
    assert settings.get_effective_max_grid_price(1) == -3
    assert settings.get_effective_max_grid_price(3) == 8


@pytest.fixture
def smart_schedule_routes(monkeypatch):
    # Exercise the app routes and the real executor. Only the HA store and
    # plan regeneration are substituted.
    tree = ast.parse((ROOT / "__init__.py").read_text())
    views = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name in {
        "AutoScheduleSettingsView", "AutoScheduleStatusView", "AutoScheduleToggleView",
    }]
    namespace = {
        "HomeAssistantView": object, "HomeAssistant": object, "ConfigEntry": object, "DOMAIN": "power_sync",
        "web": types.SimpleNamespace(json_response=lambda data, status=200: (status, data)),
        "_LOGGER": logging.getLogger(__name__), "__name__": "power_sync.test_views", "__package__": "power_sync",
    }
    exec(compile(ast.Module(body=views, type_ignores=[]), "smart_schedule_views", "exec"), namespace)
    entry = types.SimpleNamespace(entry_id="one", options={}, data={})
    hass = types.SimpleNamespace(data={"power_sync": {"one": {}}})
    executor = ev_planner.AutoScheduleExecutor(hass, entry, planner=object())
    executor._regenerate_plan = AsyncMock()
    runtime = hass.data["power_sync"]["one"]
    runtime.update(auto_schedule_executor=executor, store=object())
    saved = {"auto_schedule_settings": {"other": {"max_grid_price_cents": 8}}}
    async def persist(store, merge):
        saved.update(merge(saved))
        return True
    storage = AsyncMock(side_effect=persist)
    monkeypatch.setattr(sys.modules["power_sync"], "_update_powerwall_store", storage, raising=False)
    get_post = namespace["AutoScheduleSettingsView"](hass, entry)
    status = namespace["AutoScheduleStatusView"](hass, entry)
    toggle = namespace["AutoScheduleToggleView"](hass, entry)
    def request(fields):
        return types.SimpleNamespace(json=AsyncMock(return_value={"vehicle_id": "car", **fields}))
    return executor, runtime, saved, storage, get_post, status, toggle, request


@pytest.mark.asyncio
async def test_smart_schedule_api_saves_per_vehicle_and_returns_durable_caps(smart_schedule_routes):
    executor, runtime, saved, storage, get_post, status, toggle, request = smart_schedule_routes
    hass, entry = executor.hass, executor.config_entry
    code, response = await get_post.post(request({"max_grid_price_cents": 0, "departure_max_grid_price_cents": {"0": -2.5}}))
    assert code == 200 and response["settings"]["max_grid_price_cents"] == 0
    assert saved["auto_schedule_settings"]["other"] == {"max_grid_price_cents": 8}
    assert saved["auto_schedule_settings"]["car"]["departure_max_grid_price_cents"] == {"0": -2.5}
    for route in (get_post, status):
        code, response = await route.get(None)
        assert code == 200
        assert response["settings"]["car"]["max_grid_price_cents"] == 0
        assert response["settings"]["car"]["departure_max_grid_price_cents"] == {"0": -2.5}
    assert (await get_post.post(request({"max_grid_price_cents": True})))[0] == 400
    assert storage.await_count == 1
    assert (await toggle.post(request({"enabled": True})))[0] == 200
    storage.side_effect = OSError("disk full")
    assert (await get_post.post(request({"max_grid_price_cents": 99})))[0] == 500
    assert executor.get_settings("car").max_grid_price_cents == 0
    assert (await toggle.post(request({"enabled": False})))[0] == 500
    assert executor.get_settings("car").enabled
    shared = importlib.import_module("power_sync.automations.shared_generic_charger")
    profiles = [
        {"id": name, "name": name, "battery_capacity_kwh": 60, "schedule": {}}
        for name in ("first", "second")
    ]
    entered, release = asyncio.Event(), asyncio.Event()
    async def delay_save():
        entered.set()
        await release.wait()
    shared_store = types.SimpleNamespace(
        _data={shared.KEY: {"profiles": profiles, "selected_vehicle_id": "first"}},
        async_save=AsyncMock(side_effect=delay_save),
    )
    runtime["automation_store"] = shared_store
    shared.restore_schedule(executor)
    edit = request({"vehicle_id": "generic_ev", "shared_charger_profile_id": "first", "max_grid_price_cents": -6.5})
    saving = asyncio.create_task(get_post.post(edit))
    await asyncio.wait_for(entered.wait(), timeout=5)
    assert shared.selection_block_reason(hass, entry, executor)
    assert executor.get_settings("generic_ev").max_grid_price_cents == 25
    release.set()
    code, response = await saving
    assert code == 200 and response["settings"]["shared_charger_profile_id"] == "first"
    assert profiles[0]["schedule"]["max_grid_price_cents"] == -6.5
    wrong_value = await get_post.post(request({
        "vehicle_id": "generic_ev", "shared_charger_profile_id": "first", "max_grid_price_cents": 0,
        "expected_settings": {"max_grid_price_cents": 25},
    }))
    assert wrong_value[0] == 409 and wrong_value[1]["code"] == "settings_conflict"

    # A second request already sent by the old editor waits for the write lock.
    # Change the selected profile before the request can commit.
    async with executor._settings_write_lock:
        pending_toggle = asyncio.create_task(toggle.post(request({
            "vehicle_id": "generic_ev", "shared_charger_profile_id": "first", "enabled": True,
            "expected_settings": {"enabled": False},
        })))
        await asyncio.sleep(0)
        shared_store._data[shared.KEY]["selected_vehicle_id"] = "second"
        shared.restore_schedule(executor)
    toggle_conflict = await pending_toggle
    assert toggle_conflict[0] == 409 and toggle_conflict[1]["code"] == "shared_profile_changed"
    edit_conflict = await get_post.post(edit)
    assert edit_conflict[0] == 409 and edit_conflict[1]["code"] == "shared_profile_changed"
    shared_store.async_save.assert_awaited_once()
    assert profiles[1]["schedule"] == {}
    for route in (get_post, status):
        code, response = await route.get(None)
        assert code == 200 and response["settings"]["generic_ev"]["shared_charger_profile_id"] == "second"
    code, response = await toggle.post(request({"vehicle_id": "generic_ev", "enabled": True}))
    assert code == 200 and response["shared_charger_profile_id"] == "second"
    assert profiles[1]["schedule"]["enabled"] and not profiles[0]["schedule"]["enabled"]
    runtime.pop("store")
    assert (await get_post.post(request({"max_grid_price_cents": 99})))[0] == 503


@pytest.mark.asyncio
async def test_weekday_map_precondition_serializes_overlapping_edits_and_retries(smart_schedule_routes):
    executor, _, saved, storage, editor, _, _, req = smart_schedule_routes
    key = "departure_max_grid_price_cents"
    await editor.post(req({key: {"2": 0}}))
    opened_a = (await editor.get(None))[1]["settings"]["car"][key]
    opened_b = (await editor.get(None))[1]["settings"]["car"][key]
    entered, release = asyncio.Event(), asyncio.Event()
    original_save = storage.side_effect

    async def delayed_save(store, merge):
        entered.set()
        await release.wait()
        return await original_save(store, merge)

    storage.side_effect = delayed_save
    first = {"expected_settings": {key: opened_a}, key: {**opened_a, "0": -2.125}}
    second = {"expected_settings": {key: opened_b}, key: {**opened_b, "1": 0}}
    writer_a = asyncio.create_task(editor.post(req(first)))
    await asyncio.wait_for(entered.wait(), 3)
    writer_b = asyncio.create_task(editor.post(req(second)))
    await asyncio.sleep(0)
    assert not writer_b.done()
    release.set()
    result_a, result_b = await asyncio.gather(writer_a, writer_b)
    assert result_a[0] == 200
    assert result_b[0] == 409, (result_b, saved["auto_schedule_settings"]["car"][key])
    assert result_b[1]["code"] == "settings_conflict"
    assert storage.await_count == 2
    latest = saved["auto_schedule_settings"]["car"][key]
    assert latest == {"2": 0, "0": -2.125}
    assert executor.get_settings("car").to_dict()[key] == latest
    refreshed = {"expected_settings": {key: latest}, key: {**latest, "1": 0}}
    assert (await editor.post(req(refreshed)))[0] == 200
    assert (await editor.post(req(refreshed)))[0] == 200
    assert storage.await_count == 3
    assert saved["auto_schedule_settings"]["car"][key] == {"0": -2.125, "1": 0, "2": 0}

    # A stale request for a different value fails, even if the value is zero;
    # an independently selected car can use its own baseline.
    assert (await editor.post(req({"expected_settings": {key: {}}, key: {"1": 0}})))[0] == 409
    assert (await editor.post(req({"vehicle_id": "different", "expected_settings": {key: {}}, key: {"1": 0}})))[0] == 200
    assert saved["auto_schedule_settings"]["different"][key] == {"1": 0}


@pytest.mark.asyncio
async def test_smart_schedule_guard_types_capability_toggle_and_rollback(smart_schedule_routes):
    executor, _, saved, storage, editor, status, toggle, req = smart_schedule_routes
    key = "max_grid_price_cents"
    assert (await editor.post(req({key: 0})))[0] == 200
    for view in (editor, status):
        response = (await view.get(None))[1]["settings"]["car"]
        assert response["settings_precondition_supported"] is True
    assert "settings_precondition_supported" not in saved["auto_schedule_settings"]["car"]
    for expected in (None, {}, [], {key: True}, {key: "0"}, {key: float("nan")}, {key: float("inf")},
                     {"vehicle_id": "car"}, {"unknown": 0}, {"priority": "cost_optimized"}):
        assert (await editor.post(req({key: -0.25, "expected_settings": expected})))[0] == 400
    for bad in ({"00": 0}, {"7": 0}, {"0": False}, {"4": float("nan")}, {"5": None}, []):
        day = "departure_max_grid_price_cents"
        assert (await editor.post(req({day: {}, "expected_settings": {day: bad}})))[0] == 400
    assert (await editor.post(req({"departure_times": {}, "expected_settings": {"departure_times": {"0": "25:00"}}})))[0] == 400
    assert (await editor.post(req({"departure_preserve_home_battery": {"0": "false"}, "expected_settings": {"departure_preserve_home_battery": {}}})))[0] == 400
    assert (await toggle.post(req({"expected_settings": {"enabled": False}})))[0] == 400
    assert storage.await_count == 1
    good = {"enabled": True, "expected_settings": {"enabled": False}}
    assert (await toggle.post(req(good)))[1]["enabled"] is True
    assert (await toggle.post(req(good)))[1]["enabled"] is True
    assert storage.await_count == 2
    # An application/storage failure is independent of the successful expected-value check.
    original_save = storage.side_effect
    storage.side_effect = OSError("unavailable")
    patch = {key: -2.125, "expected_settings": {key: 0}}
    assert (await editor.post(req(patch)))[0] == 500
    assert executor.get_settings("car").max_grid_price_cents == saved["auto_schedule_settings"]["car"][key] == 0
    storage.side_effect = original_save
    assert (await editor.post(req(patch)))[0] == 200
    await editor.post(req({key: 7}))
    conflict = await editor.post(req(patch))
    assert conflict[0] == 409 and conflict[1]["field"] == key
    assert saved["auto_schedule_settings"]["car"][key] == 7


def test_preserve_home_battery_round_trips_with_auto_schedule_settings():
    settings = ev_planner.AutoScheduleSettings.from_dict({
        "preserve_home_battery": True,
    })

    assert settings.preserve_home_battery is True
    assert settings.to_dict()["preserve_home_battery"] is True


def test_preserve_home_battery_disables_consume_battery_level_on_load():
    settings = ev_planner.AutoScheduleSettings.from_dict({
        "preserve_home_battery": True,
        "consume_battery_level": 50,
    })

    assert settings.preserve_home_battery is True
    assert settings.consume_battery_level == 0


def test_departure_preserve_home_battery_round_trips_and_overrides_default():
    settings = ev_planner.AutoScheduleSettings.from_dict({
        "preserve_home_battery": True,
        "departure_preserve_home_battery": {"0": False, "1": True},
    })

    assert settings.departure_preserve_home_battery == {0: False, 1: True}
    assert settings.get_effective_preserve_home_battery(0) is False
    assert settings.get_effective_preserve_home_battery(1) is True
    assert settings.get_effective_preserve_home_battery(2) is True
    assert settings.to_dict()["departure_preserve_home_battery"] == {
        "0": False,
        "1": True,
    }


def test_departure_preserve_home_battery_disables_day_consume_on_load():
    settings = ev_planner.AutoScheduleSettings.from_dict({
        "departure_consume_battery_level": {"0": 45, "1": 35},
        "departure_preserve_home_battery": {"0": True, "1": False},
    })

    assert settings.departure_consume_battery_level == {0: 0, 1: 35}
    assert settings.departure_preserve_home_battery == {0: True, 1: False}


def test_auto_schedule_preserve_and_consume_settings_are_mutually_exclusive():
    executor = object.__new__(ev_planner.AutoScheduleExecutor)
    executor._settings = {}
    executor._state = {}

    settings = executor.update_settings(
        "ev-1",
        {
            "consume_battery_level": 40,
            "preserve_home_battery": True,
        },
    )

    assert settings.preserve_home_battery is True
    assert settings.consume_battery_level == 0

    settings = executor.update_settings(
        "ev-1",
        {"consume_battery_level": 30},
    )

    assert settings.consume_battery_level == 30
    assert settings.preserve_home_battery is False


def test_auto_schedule_departure_preserve_and_consume_settings_are_mutually_exclusive():
    executor = object.__new__(ev_planner.AutoScheduleExecutor)
    executor._settings = {}
    executor._state = {}

    settings = executor.update_settings(
        "ev-1",
        {
            "departure_consume_battery_level": {0: 40},
            "departure_preserve_home_battery": {0: True},
        },
    )

    assert settings.departure_preserve_home_battery == {0: True}
    assert settings.departure_consume_battery_level == {0: 0}

    settings = executor.update_settings(
        "ev-1",
        {"departure_consume_battery_level": {0: 30}},
    )

    assert settings.departure_consume_battery_level == {0: 30}
    assert settings.departure_preserve_home_battery == {0: False}


def test_vehicle_charger_config_syncs_generic_status_entity():
    settings = ev_planner.AutoScheduleSettings()

    settings.apply_charger_config({
        "charger_type": "generic",
        "min_amps": 6,
        "max_amps": 24,
        "voltage": 240,
        "phases": 3,
        "charger_switch_entity": "switch.garage_ev",
        "charger_amps_entity": "number.garage_ev_current",
        "charger_status_entity": "sensor.garage_ev_status",
    })

    assert settings.charger_type == "generic"
    assert settings.min_charge_amps == 6
    assert settings.max_charge_amps == 24
    assert settings.voltage == 240
    assert settings.phases == 3
    assert settings.charger_status_entity == "sensor.garage_ev_status"


def test_vehicle_charger_config_syncs_app_charge_amp_aliases():
    settings = ev_planner.AutoScheduleSettings(
        min_charge_amps=5,
        max_charge_amps=32,
    )

    settings.apply_charger_config({
        "min_charge_amps": 6,
        "max_charge_amps": 15,
        "voltage": 240,
        "phases": 1,
    })

    assert settings.min_charge_amps == 6
    assert settings.max_charge_amps == 15
    assert settings.voltage == 240
    assert settings.phases == 1


def test_vehicle_charger_config_syncs_nonempty_display_name():
    settings = ev_planner.AutoScheduleSettings(display_name="EV")

    settings.apply_charger_config({"display_name": "W3RT1E"})

    assert settings.display_name == "W3RT1E"

    settings.apply_charger_config({"display_name": ""})

    assert settings.display_name == "W3RT1E"


def test_auto_schedule_states_hide_disabled_cached_plan():
    executor = object.__new__(ev_planner.AutoScheduleExecutor)
    executor._settings = {
        "tessy": ev_planner.AutoScheduleSettings(
            enabled=False,
            vehicle_id="tessy",
            display_name="TESSY",
        ),
    }
    executor._state = {
        "tessy": ev_planner.AutoScheduleState(
            vehicle_id="tessy",
            is_charging=True,
            last_decision="waiting",
            last_decision_reason="Waiting for planned deadline window",
            current_plan=types.SimpleNamespace(
                windows=[object()],
                estimated_solar_kwh=0,
                estimated_grid_kwh=13,
                estimated_cost_cents=403,
                battery_capacity_kwh=60,
                effective_battery_capacity_kwh=60,
                battery_capacity_source="manual",
            ),
        ),
    }

    states = executor.get_all_states()

    assert states["tessy"]["enabled"] is False
    assert states["tessy"]["display_name"] == "TESSY"
    assert states["tessy"]["is_charging"] is False
    assert states["tessy"]["last_decision"] == "disabled"
    assert states["tessy"]["plan_summary"] is None
    assert executor.get_all_states(enabled_only=True) == {}


def test_auto_schedule_states_keep_enabled_vehicle_identity_and_plan():
    executor = object.__new__(ev_planner.AutoScheduleExecutor)
    executor._settings = {
        "w3rt1e": ev_planner.AutoScheduleSettings(
            enabled=True,
            vehicle_id="w3rt1e",
            display_name="W3RT1E",
        ),
    }
    executor._state = {
        "w3rt1e": ev_planner.AutoScheduleState(
            vehicle_id="w3rt1e",
            is_charging=True,
            last_decision="charging",
        ),
    }

    states = executor.get_all_states(enabled_only=True)

    assert states["w3rt1e"]["enabled"] is True
    assert states["w3rt1e"]["display_name"] == "W3RT1E"
    assert states["w3rt1e"]["is_charging"] is True
    assert states["w3rt1e"]["last_decision"] == "charging"


def test_auto_schedule_plan_presentation_is_offset_safe_and_read_only():
    plan = ev_planner.ChargingPlan(
        vehicle_id="tessy",
        current_soc=55,
        target_soc=80,
        target_time=None,
        energy_needed_kwh=12,
        windows=[
            ev_planner.PlannedChargingWindow(
                start_time="2026-10-04T01:30:00+10:00",
                end_time="2026-10-04T02:30:00+10:00",
                source="solar_surplus",
                estimated_power_kw=7,
                estimated_energy_kwh=7,
                price_cents_kwh=0,
                reason="solar_forecast",
            ),
            ev_planner.PlannedChargingWindow(
                start_time="2026-10-04T03:30:00+11:00",
                end_time="2026-10-04T04:30:00+11:00",
                source="grid_offpeak",
                estimated_power_kw=7,
                estimated_energy_kwh=7,
                price_cents_kwh=12,
                reason="offpeak_rate",
            ),
        ],
    )
    state = ev_planner.AutoScheduleState(
        vehicle_id="tessy",
        current_plan=plan,
        last_plan_update=datetime(2026, 10, 3, 14, tzinfo=timezone.utc),
    )

    snapshot = state.plan_presentation_snapshot(
        datetime(2026, 10, 3, 16, tzinfo=timezone.utc)
    )

    assert snapshot == {
        "status": "active",
        "window": {
            "start_time": "2026-10-04T01:30:00+10:00",
            "end_time": "2026-10-04T02:30:00+10:00",
            "source": "solar_surplus",
        },
        "last_plan_update": "2026-10-03T14:00:00+00:00",
    }
    # Presentation does not alter the executable plan or its raw source.
    assert plan.windows[0].source == "solar_surplus"
    assert len(plan.windows) == 2


def test_auto_schedule_plan_presentation_distinguishes_no_plan_and_stale():
    now = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
    assert ev_planner.AutoScheduleState(vehicle_id="tessy").plan_presentation_snapshot(now)["status"] == "no_plan"

    stale_plan = types.SimpleNamespace(windows=[
        types.SimpleNamespace(
            start_time="2026-10-03T08:00:00+00:00",
            end_time="2026-10-03T09:00:00+00:00",
            source="grid_offpeak",
        ),
    ])
    assert ev_planner.AutoScheduleState(
        vehicle_id="tessy", current_plan=stale_plan
    ).plan_presentation_snapshot(now)["status"] == "stale"


def test_auto_schedule_sync_reads_app_vehicle_config_store():
    automation_store = types.SimpleNamespace(
        _data={
            "vehicle_charging_configs": [
                {
                    "vehicle_id": "ble_teslablefbd",
                    "charger_type": "tesla",
                    "min_amps": 5,
                    "max_amps": 15,
                    "voltage": 240,
                    "phases": 1,
                }
            ]
        }
    )
    hass = types.SimpleNamespace(
        data={
            ev_planner.DOMAIN: {
                "entry-1": {
                    "automation_store": automation_store,
                }
            }
        }
    )
    entry = types.SimpleNamespace(entry_id="entry-1")
    executor = object.__new__(ev_planner.AutoScheduleExecutor)
    executor.hass = hass
    executor.config_entry = entry
    executor._store = types.SimpleNamespace(_data={})

    settings = ev_planner.AutoScheduleSettings(
        vehicle_id="ble_teslablefbd",
        max_charge_amps=32,
        voltage=230,
    )

    executor._sync_charger_params_from_vehicle_configs(
        "ble_teslablefbd",
        settings,
    )

    assert settings.max_charge_amps == 15
    assert settings.voltage == 240
    assert settings.phases == 1


def test_auto_schedule_sync_reads_app_charge_amp_aliases():
    automation_store = types.SimpleNamespace(
        _data={
            "vehicle_charging_configs": [
                {
                    "vehicle_id": "ble_teslablefbd",
                    "charger_type": "tesla",
                    "min_charge_amps": 6,
                    "max_charge_amps": 15,
                    "voltage": 240,
                    "phases": 1,
                }
            ]
        }
    )
    hass = types.SimpleNamespace(
        data={
            ev_planner.DOMAIN: {
                "entry-1": {
                    "automation_store": automation_store,
                }
            }
        }
    )
    entry = types.SimpleNamespace(entry_id="entry-1")
    executor = object.__new__(ev_planner.AutoScheduleExecutor)
    executor.hass = hass
    executor.config_entry = entry
    executor._store = types.SimpleNamespace(_data={})

    settings = ev_planner.AutoScheduleSettings(
        vehicle_id="ble_teslablefbd",
        min_charge_amps=5,
        max_charge_amps=32,
        voltage=230,
    )

    executor._sync_charger_params_from_vehicle_configs(
        "ble_teslablefbd",
        settings,
    )

    assert settings.min_charge_amps == 6
    assert settings.max_charge_amps == 15
    assert settings.voltage == 240
    assert settings.phases == 1


def test_auto_schedule_sync_normalizes_stale_synthetic_backend():
    automation_store = types.SimpleNamespace(
        _data={
            "vehicle_charging_configs": [
                {
                    "vehicle_id": "generic_ev",
                    "charger_type": "ocpp",
                    "charger_switch_entity": "switch.charger_charge_control",
                }
            ]
        }
    )
    hass = types.SimpleNamespace(
        data={
            ev_planner.DOMAIN: {
                "entry-1": {
                    "automation_store": automation_store,
                }
            }
        }
    )
    entry = types.SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={"generic_charger_enabled": True},
    )
    executor = object.__new__(ev_planner.AutoScheduleExecutor)
    executor.hass = hass
    executor.config_entry = entry
    executor._store = types.SimpleNamespace(_data={})
    settings = ev_planner.AutoScheduleSettings(
        vehicle_id="generic_ev",
        charger_type="ocpp",
    )

    executor._sync_charger_params_from_vehicle_configs(
        "generic_ev",
        settings,
    )

    assert settings.charger_type == "generic"
    assert (
        settings.charger_switch_entity
        == "switch.charger_charge_control"
    )


def test_generic_options_override_stale_app_profile_entities():
    """A saved Generic Charger replacement wins over legacy generic_ev data."""
    opts = {
        "generic_charger_enabled": True,
        "generic_charger_switch_entity": "switch.replacement",
        "generic_charger_amps_entity": "number.replacement_amps",
        "generic_charger_status_entity": "sensor.replacement_status",
        "generic_charger_power_entity": "sensor.replacement_power",
    }
    params = ev_planner._with_configured_charger_entities(
        types.SimpleNamespace(),
        {
            "vehicle_id": "generic_ev",
            "charger_switch_entity": "input_boolean.deleted",
            "charger_amps_entity": "number.old_amps",
            "charger_status_entity": "sensor.old_status",
            "charger_power_entity": "sensor.old_power",
        },
        opts,
        "generic",
    )

    assert params == {
        "vehicle_id": "generic_ev",
        "charger_switch_entity": "switch.replacement",
        "charger_amps_entity": "number.replacement_amps",
        "charger_status_entity": "sensor.replacement_status",
        "charger_power_entity": "sensor.replacement_power",
    }


def test_auto_schedule_sync_matches_ble_prefix_alias():
    automation_store = types.SimpleNamespace(
        _data={
            "vehicle_charging_configs": [
                {
                    "vehicle_id": "teslablefbd",
                    "max_amps": 15,
                    "voltage": 240,
                    "phases": 1,
                }
            ]
        }
    )
    hass = types.SimpleNamespace(
        data={
            ev_planner.DOMAIN: {
                "entry-1": {
                    "automation_store": automation_store,
                }
            }
        }
    )
    entry = types.SimpleNamespace(entry_id="entry-1")
    executor = object.__new__(ev_planner.AutoScheduleExecutor)
    executor.hass = hass
    executor.config_entry = entry
    executor._store = types.SimpleNamespace(_data={})

    settings = ev_planner.AutoScheduleSettings(
        vehicle_id="ble_teslablefbd",
        max_charge_amps=32,
    )

    executor._sync_charger_params_from_vehicle_configs(
        "ble_teslablefbd",
        settings,
    )

    assert settings.max_charge_amps == 15


def test_configured_generic_entities_preserve_vehicle_overrides():
    params = {
        "charger_switch_entity": "switch.vehicle_ev",
        "charger_amps_entity": "number.vehicle_ev_current",
        "charger_status_entity": "sensor.vehicle_ev_status",
    }

    result = ev_planner._with_configured_charger_entities(
        None,
        params,
        {
            "generic_charger_switch_entity": "switch.global_ev",
            "generic_charger_amps_entity": "number.global_ev_current",
            "generic_charger_status_entity": "sensor.global_ev_status",
        },
        "generic",
    )

    assert result["charger_switch_entity"] == "switch.vehicle_ev"
    assert result["charger_amps_entity"] == "number.vehicle_ev_current"
    assert result["charger_status_entity"] == "sensor.vehicle_ev_status"


def test_power_to_amps_uses_vehicle_charger_phase_settings():
    executor = object.__new__(ev_planner.AutoScheduleExecutor)

    single_phase = ev_planner.AutoScheduleSettings(
        voltage=230,
        phases=1,
        max_charge_amps=32,
    )
    three_phase = ev_planner.AutoScheduleSettings(
        voltage=230,
        phases=3,
        max_charge_amps=32,
    )

    assert executor._power_to_amps_for_settings(6900, single_phase) == 30
    assert executor._power_to_amps_for_settings(6900, three_phase) == 10


def test_successful_smart_schedule_rates_are_suppressed_per_vehicle(monkeypatch):
    """A second car or repeated plan evaluation must not replay paid targets."""
    from unittest.mock import AsyncMock

    executor = object.__new__(ev_planner.AutoScheduleExecutor)
    executor.hass = types.SimpleNamespace()
    executor.config_entry = types.SimpleNamespace(data={}, options={})
    executor._current_charge_amps = {}
    executor._charge_rate_change_threshold = 1
    executor._resolve_effective_charger_capability = AsyncMock(return_value=None)
    executor._resolve_vehicle_vin = lambda vehicle: vehicle
    monkeypatch.setattr(ev_planner, "get_ev_location", AsyncMock(return_value="home"))
    monkeypatch.setattr(ev_planner, "_with_configured_charger_entities", lambda _hass, params, *_: params)
    calls = []

    async def rate(_hass, _entry, vehicle, amps, params):
        calls.append((vehicle, amps))
        return True

    boundary = types.ModuleType("power_sync.automations.actions")
    boundary._set_vehicle_amps = rate
    monkeypatch.setitem(sys.modules, "power_sync.automations.actions", boundary)
    settings = ev_planner.AutoScheduleSettings(voltage=230, phases=1, max_charge_amps=32)

    async def replay():
        for _ in range(40):
            assert await executor._set_vehicle_charge_rate("VIN_A", 3680, settings)
            assert await executor._set_vehicle_charge_rate("VIN_B", 1840, settings)
        assert calls == [("VIN_A", 16), ("VIN_B", 8)]
        assert await executor._set_vehicle_charge_rate("VIN_A", 3910, settings)
        assert await executor._set_vehicle_charge_rate("VIN_B", 1840, settings)
        assert calls == [("VIN_A", 16), ("VIN_B", 8), ("VIN_A", 17)]

    asyncio.run(replay())


def test_power_to_amps_uses_configured_vehicle_minimum():
    executor = object.__new__(ev_planner.AutoScheduleExecutor)
    settings = ev_planner.AutoScheduleSettings(
        min_charge_amps=8,
        max_charge_amps=10,
        voltage=240,
        phases=1,
    )

    assert executor._power_to_amps_for_settings(1000, settings) == 8
    assert executor._power_to_amps_for_settings(10000, settings) == 10

    malformed_legacy = ev_planner.AutoScheduleSettings(
        min_charge_amps=16,
        max_charge_amps=10,
        voltage=240,
        phases=1,
    )
    assert executor._power_to_amps_for_settings(1000, malformed_legacy) == 10


def test_legacy_numeric_schedule_uses_resolved_vehicle_current_and_capacity():
    secondary_vin = "5YJTEST0000000002"
    automation_store = types.SimpleNamespace(
        _data={
            "vehicle_charging_configs": [
                {
                    "vehicle_id": secondary_vin,
                    "display_name": "Secondary EV",
                    "charger_type": "tesla",
                    "min_amps": 6,
                    "max_amps": 10,
                    "voltage": 240,
                    "phases": 1,
                    "battery_capacity_kwh": 86,
                },
            ],
        },
    )
    hass = types.SimpleNamespace(
        data={
            ev_planner.DOMAIN: {
                "entry-1": {"automation_store": automation_store},
            },
        },
    )
    entry = types.SimpleNamespace(entry_id="entry-1", data={}, options={})
    executor = object.__new__(ev_planner.AutoScheduleExecutor)
    executor.hass = hass
    executor.config_entry = entry
    executor._store = types.SimpleNamespace(_data={})
    executor._resolve_vehicle_vin = lambda vehicle_id: (
        secondary_vin if vehicle_id == "2" else None
    )
    settings = ev_planner.AutoScheduleSettings(
        vehicle_id="2",
        min_charge_amps=5,
        max_charge_amps=32,
        voltage=230,
        phases=1,
    )

    resolved = executor.resolve_vehicle_capacity("2", settings)

    assert settings.min_charge_amps == 6
    assert settings.max_charge_amps == 10
    assert settings.voltage == 240
    assert settings.phases == 1
    assert settings.get_max_charge_power_kw() == 2.4
    assert resolved.effective_battery_capacity_kwh == 86
    assert resolved.battery_capacity_source == "manual"


def test_stored_default_migrates_to_configured_generic_id():
    entry = types.SimpleNamespace(
        data={},
        options={"generic_charger_enabled": True},
    )
    stored_data = {
        "auto_schedule_settings": {
            "_default": {
                "enabled": True,
                "vehicle_id": "_default",
                "charger_type": "tesla",
            }
        },
        "cached_vehicle_soc": {
            "_default": {"soc": 61},
        },
    }

    changed = ev_planner._normalize_stored_auto_schedule_ids(
        None,
        entry,
        stored_data,
    )

    assert changed is True
    assert set(stored_data["auto_schedule_settings"]) == {"generic_ev"}
    assert (
        stored_data["auto_schedule_settings"]["generic_ev"]["vehicle_id"]
        == "generic_ev"
    )
    assert stored_data["cached_vehicle_soc"] == {
        "generic_ev": {"soc": 61},
    }


def test_existing_canonical_generic_settings_win_over_default():
    entry = types.SimpleNamespace(
        data={},
        options={"generic_charger_enabled": True},
    )
    stored_data = {
        "auto_schedule_settings": {
            "_default": {"enabled": True, "charger_type": "tesla"},
            "generic_ev": {"enabled": False, "charger_type": "ocpp"},
        },
    }

    ev_planner._normalize_stored_auto_schedule_ids(None, entry, stored_data)

    assert stored_data["auto_schedule_settings"] == {
        "generic_ev": {
            "enabled": False,
            "vehicle_id": "generic_ev",
            "charger_type": "generic",
        },
    }


def test_existing_synthetic_backend_is_normalized_without_default_row():
    entry = types.SimpleNamespace(
        data={},
        options={"generic_charger_enabled": True},
    )
    stored_data = {
        "auto_schedule_settings": {
            "generic_ev": {
                "enabled": True,
                "vehicle_id": "generic_ev",
                "charger_type": "ocpp",
            },
        },
    }

    changed = ev_planner._normalize_stored_auto_schedule_ids(
        None,
        entry,
        stored_data,
    )

    assert changed is True
    assert (
        stored_data["auto_schedule_settings"]["generic_ev"]["charger_type"]
        == "generic"
    )


def test_tesla_default_settings_remain_backward_compatible():
    entry = types.SimpleNamespace(data={}, options={})
    stored_data = {
        "auto_schedule_settings": {
            "_default": {"enabled": True, "charger_type": "tesla"},
        },
    }

    changed = ev_planner._normalize_stored_auto_schedule_ids(
        None,
        entry,
        stored_data,
    )

    assert changed is False
    assert "_default" in stored_data["auto_schedule_settings"]


def test_stable_vehicle_settings_remove_legacy_phantom_rows():
    entry = types.SimpleNamespace(data={}, options={})
    stored_data = {
        "auto_schedule_settings": {
            "_default": {"enabled": False, "charger_type": "tesla"},
            "1": {"enabled": False, "charger_type": "tesla"},
            "5YJTEST0000000001": {
                "enabled": True,
                "charger_type": "tesla",
            },
        },
        "cached_vehicle_soc": {
            "_default": {"soc": 40},
            "1": {"soc": 45},
            "5YJTEST0000000001": {"soc": 50},
        },
    }

    changed = ev_planner._normalize_stored_auto_schedule_ids(
        None,
        entry,
        stored_data,
    )

    assert changed is True
    assert set(stored_data["auto_schedule_settings"]) == {"5YJTEST0000000001"}
    assert set(stored_data["cached_vehicle_soc"]) == {"5YJTEST0000000001"}


def test_paired_ble_auto_schedule_profile_is_coalesced_into_existing_vin():
    vin = "5YJTEST0000000001"
    entry = types.SimpleNamespace(
        data={},
        options={
            "ev_provider": "both",
            "tesla_ble_entity_prefix": "bridge_alpha",
            "tesla_ble_vehicle_mapping": f"{vin}=bridge_alpha",
        },
    )
    stored_data = {
        "auto_schedule_settings": {
            vin: {
                "enabled": True,
                "display_name": "Canonical Tesla",
                "charger_type": "tesla",
            },
            "ble_bridge_alpha": {
                "enabled": False,
                "display_name": "Stale BLE alias",
                "charger_type": "tesla",
            },
        },
        "cached_vehicle_soc": {
            vin: {"soc": 61},
            "ble_bridge_alpha": {"soc": 42},
        },
    }

    changed = ev_planner._normalize_stored_auto_schedule_ids(
        None,
        entry,
        stored_data,
        fleet_vins=[vin],
        resolved_prefixes=["bridge_alpha"],
    )

    assert changed is True
    assert stored_data["auto_schedule_settings"] == {
        vin: {
            "enabled": True,
            "display_name": "Canonical Tesla",
            "charger_type": "tesla",
            "vehicle_id": vin,
        }
    }
    assert stored_data["cached_vehicle_soc"] == {vin: {"soc": 61}}


def test_paired_ble_only_auto_schedule_profile_migrates_to_vin():
    vin = "5YJTEST0000000001"
    entry = types.SimpleNamespace(
        data={},
        options={
            "ev_provider": "both",
            "tesla_ble_entity_prefix": "bridge_alpha",
            "tesla_ble_vehicle_mapping": f"{vin}=bridge_alpha",
        },
    )
    stored_data = {
        "auto_schedule_settings": {
            "ble_bridge_alpha": {
                "enabled": True,
                "display_name": "Tesla BLE",
                "charger_type": "tesla",
            },
        },
        "cached_vehicle_soc": {
            "ble_bridge_alpha": {"soc": 57},
        },
    }

    changed = ev_planner._normalize_stored_auto_schedule_ids(
        None,
        entry,
        stored_data,
        fleet_vins=[vin],
        resolved_prefixes=["bridge_alpha"],
    )

    assert changed is True
    assert stored_data["auto_schedule_settings"] == {
        vin: {
            "enabled": True,
            "display_name": "Tesla BLE",
            "charger_type": "tesla",
            "vehicle_id": vin,
        }
    }
    assert stored_data["cached_vehicle_soc"] == {vin: {"soc": 57}}


def test_paired_ble_runtime_updates_use_canonical_vin():
    vin = "5YJTEST0000000001"
    entry = types.SimpleNamespace(
        data={},
        options={
            "ev_provider": "both",
            "tesla_ble_entity_prefix": "bridge_alpha",
            "tesla_ble_vehicle_mapping": f"{vin}=bridge_alpha",
        },
    )
    executor = object.__new__(ev_planner.AutoScheduleExecutor)
    executor.hass = None
    executor.config_entry = entry
    executor._fleet_vins = (vin,)
    executor._settings = {}
    executor._state = {}

    settings = executor.update_settings(
        "ble_bridge_alpha",
        {"enabled": True, "vehicle_id": "ble_bridge_alpha"},
    )

    assert settings.vehicle_id == vin
    assert set(executor._settings) == {vin}


def test_ble_only_storage_drops_ambiguous_fleet_profiles_and_keeps_aliases():
    vin_a = "5YJTEST0000000001"
    vin_b = "5YJTEST0000000002"
    entry = types.SimpleNamespace(
        data={},
        options={
            "ev_provider": "tesla_ble",
            "tesla_ble_entity_prefix": "tesla_yf88,tesla_flinn",
        },
    )
    stored_data = {
        "auto_schedule_settings": {
            vin_a: {"enabled": True, "charger_type": "ocpp"},
            vin_b: {"enabled": True, "charger_type": "ocpp"},
            "ble_tesla_flinn": {"enabled": True, "charger_type": "tesla"},
        },
        "cached_vehicle_soc": {
            vin_a: {"soc": 60},
            vin_b: {"soc": 61},
            "ble_tesla_flinn": {"soc": 67},
        },
    }

    changed = ev_planner._normalize_stored_auto_schedule_ids(
        None,
        entry,
        stored_data,
        fleet_vins=[vin_a, vin_b],
        resolved_prefixes=["tesla_yf88", "tesla_flinn"],
    )

    assert changed is True
    assert stored_data["auto_schedule_settings"] == {
        "ble_tesla_flinn": {
            "enabled": True,
            "charger_type": "tesla",
            "vehicle_id": "ble_tesla_flinn",
        }
    }
    assert stored_data["cached_vehicle_soc"] == {
        "ble_tesla_flinn": {"soc": 67},
    }

    executor = object.__new__(ev_planner.AutoScheduleExecutor)
    executor.hass = None
    executor.config_entry = entry
    executor._fleet_vins = (vin_a, vin_b)
    executor._resolved_ble_prefixes = ("tesla_yf88", "tesla_flinn")
    executor._settings = {}
    executor._state = {}
    settings = executor.update_settings("ble_tesla_yf88", {"enabled": True})

    assert settings.vehicle_id == "ble_tesla_yf88"
    assert set(executor._settings) == {"ble_tesla_yf88"}


def test_autodiscovered_ble_runtime_updates_use_canonical_vin():
    vin = "5YJTEST0000000001"
    entry = types.SimpleNamespace(
        data={},
        options={
            "ev_provider": "both",
            "tesla_ble_entity_prefix": "tesla_ble",
        },
    )
    executor = object.__new__(ev_planner.AutoScheduleExecutor)
    executor.hass = None
    executor.config_entry = entry
    executor._fleet_vins = (vin,)
    executor._resolved_ble_prefixes = ("garage_ble",)
    executor._settings = {}
    executor._state = {}

    settings = executor.update_settings(
        "ble_garage_ble",
        {"enabled": True},
    )

    assert settings.vehicle_id == vin
    assert set(executor._settings) == {vin}


def test_missing_vehicle_id_updates_canonical_generic_settings():
    entry = types.SimpleNamespace(
        data={},
        options={"generic_charger_enabled": True},
    )
    executor = object.__new__(ev_planner.AutoScheduleExecutor)
    executor.hass = None
    executor.config_entry = entry
    executor._settings = {}
    executor._state = {}

    settings = executor.update_settings("_default", {"enabled": True})

    assert settings.vehicle_id == "generic_ev"
    assert set(executor._settings) == {"generic_ev"}


def test_generic_charger_load_provisions_disabled_runtime_schedule_candidate(monkeypatch):
    """A first Generic Charger schedule can be configured without a saved row."""
    class Store:
        saved = []

        async def async_load(self):
            return {}

        async def async_save(self, data):
            self.saved.append(data)

    async def no_fleet_vehicles(*args, **kwargs):
        return []

    monkeypatch.setattr(ev_planner, "discover_all_tesla_vehicles", no_fleet_vehicles)
    executor = object.__new__(ev_planner.AutoScheduleExecutor)
    executor.hass = types.SimpleNamespace(data={})
    executor.config_entry = types.SimpleNamespace(
        data={},
        options={"generic_charger_enabled": True},
        entry_id="entry-1",
    )
    executor._settings = {}
    executor._state = {}

    store = Store()
    asyncio.run(executor.load_settings(store))

    assert set(executor._settings) == {"generic_ev"}
    assert executor._settings["generic_ev"].enabled is False
    assert executor._settings["generic_ev"].charger_type == "generic"
    assert set(executor._state) == {"generic_ev"}
    assert store.saved == []


def test_configured_generic_backend_overrides_stale_ocpp_synthetic_type():
    settings = ev_planner.AutoScheduleSettings(
        vehicle_id="generic_ev",
        charger_type="ocpp",
    )

    assert ev_planner._effective_auto_schedule_charger_type(
        settings,
        {"generic_charger_enabled": True},
    ) == "generic"


def test_real_vin_preserves_explicit_ocpp_backend():
    settings = ev_planner.AutoScheduleSettings(
        vehicle_id="5YJTEST0000000002",
        charger_type="ocpp",
    )

    assert ev_planner._effective_auto_schedule_charger_type(
        settings,
        {"generic_charger_enabled": True},
    ) == "ocpp"


def test_stale_synthetic_ocpp_start_uses_configured_generic_backend(monkeypatch):
    captured = {}
    fake_actions = types.ModuleType("power_sync.automations.actions")

    async def fake_start(hass, entry, params, context=None):
        captured.update(params)
        return True

    fake_actions._action_start_ev_charging_dynamic = fake_start
    monkeypatch.setitem(
        sys.modules,
        "power_sync.automations.actions",
        fake_actions,
    )
    monkeypatch.setattr(
        ev_planner,
        "_get_optimizer_battery_params",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        ev_planner.dt_util,
        "now",
        lambda: types.SimpleNamespace(weekday=lambda: 0),
    )

    executor = object.__new__(ev_planner.AutoScheduleExecutor)
    executor.hass = types.SimpleNamespace()
    executor.config_entry = types.SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "generic_charger_enabled": True,
            "generic_charger_switch_entity": "switch.charger_charge_control",
        },
    )
    executor._last_external_smart_schedule_stops = {}
    executor._resolve_vehicle_vin = lambda vehicle_id: None
    settings = ev_planner.AutoScheduleSettings(
        enabled=True,
        vehicle_id="generic_ev",
        charger_type="ocpp",
    )
    state = ev_planner.AutoScheduleState(vehicle_id="generic_ev")

    asyncio.run(
        executor._start_charging(
            "generic_ev",
            settings,
            state,
            "price_level_charging",
        )
    )

    assert state.is_charging is True
    assert captured["charger_type"] == "generic"
    assert captured["vehicle_id"] == "generic_ev"
    assert captured["vehicle_vin"] == "generic_ev"
    assert (
        captured["charger_switch_entity"]
        == "switch.charger_charge_control"
    )


def test_failed_start_backoff_suppresses_immediate_retry(monkeypatch):
    calls = []
    fake_actions = types.ModuleType("power_sync.automations.actions")

    async def fake_start(*args, **kwargs):
        calls.append(True)
        return True

    fake_actions._action_start_ev_charging_dynamic = fake_start
    monkeypatch.setitem(
        sys.modules,
        "power_sync.automations.actions",
        fake_actions,
    )
    monkeypatch.setattr(ev_planner.time, "monotonic", lambda: 100.0)

    executor = object.__new__(ev_planner.AutoScheduleExecutor)
    executor._start_failure_state = {
        "generic_ev": (1, 130.0),
    }

    started = asyncio.run(
        executor._start_charging(
            "generic_ev",
            ev_planner.AutoScheduleSettings(vehicle_id="generic_ev"),
            ev_planner.AutoScheduleState(vehicle_id="generic_ev"),
            "price_level_charging",
        )
    )

    assert started is False
    assert calls == []


def test_failed_start_backoff_is_exponential_and_bounded(monkeypatch):
    monkeypatch.setattr(ev_planner.time, "monotonic", lambda: 100.0)
    executor = object.__new__(ev_planner.AutoScheduleExecutor)
    executor._start_failure_state = {}

    delays = [
        executor._record_start_failure("generic_ev")[1]
        for _ in range(10)
    ]

    assert delays[:4] == [30, 60, 120, 240]
    assert delays[-1] == ev_planner.AUTO_SCHEDULE_START_RETRY_MAX_SECONDS


def test_action_smart_schedule_save_reports_storage_failure(monkeypatch):
    import pytest

    executor = object.__new__(ev_planner.AutoScheduleExecutor)
    executor._settings = {"car": ev_planner.AutoScheduleSettings(vehicle_id="car", enabled=True)}
    executor._cached_soc = {}
    failed_save = AsyncMock(side_effect=OSError("disk full"))
    monkeypatch.setattr(sys.modules["power_sync"], "_update_powerwall_store", failed_save, raising=False)
    store = types.SimpleNamespace(async_save=AsyncMock())
    with pytest.raises(OSError, match="disk full"):
        asyncio.run(executor.save_settings(store, raise_errors=True))
    failed_save.assert_awaited_once()
