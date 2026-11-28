"""Exercise HA EV actions against isolated entries, storage and executors."""

import ast
import asyncio
import runpy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import voluptuous as vol

ROOT = Path(__file__).resolve().parent.parent / "custom_components/power_sync"


@pytest.fixture
def actions():
    namespace = {
        "charging_mode_settings": runpy.run_path(str(ROOT / "ev_mode_settings.py"))["charging_mode_settings"],
        "asyncio": asyncio, "vol": vol, "DOMAIN": "power_sync",
        "HomeAssistantError": RuntimeError,
        "SupportsResponse": SimpleNamespace(OPTIONAL="optional"),
        "cv": SimpleNamespace(string=str, boolean=vol.Boolean(), datetime=str),
        "async_update_solar_surplus_config": AsyncMock(return_value={"enabled": False}),
    }
    tree = ast.parse((ROOT / "ev_charging_services.py").read_text())
    tree.body = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
    exec(compile(tree, "ev_charging_services", "exec"), namespace)
    return SimpleNamespace(**namespace)


def runtime():
    entry = SimpleNamespace(entry_id="one", domain="power_sync")
    store = SimpleNamespace(_data={}, async_save=AsyncMock())
    data = {"automation_store": store}
    hass = SimpleNamespace(data={"power_sync": {"one": data}})
    return hass, entry, store, data


@pytest.mark.parametrize("mode", ["scheduled", "price_level"])
def test_mode_action_preserves_settings_and_other_modes(actions, mode):
    hass, entry, store, data = runtime()
    key = f"{mode}_charging"
    store._data = {key: {"enabled": True, "max_price_cents": 0, "custom": 12},
                   "solar_surplus_config": {"enabled": True}}
    refresh = Mock()
    data["optimization_coordinator"] = SimpleNamespace(_schedule_settings_reoptimization=refresh)
    asyncio.run(actions.async_set_charging_mode(hass, entry, mode, False))
    assert store._data[key]["enabled"] is False
    assert store._data[key]["max_price_cents"] == 0
    assert store._data[key]["custom"] == 12
    assert store._data["solar_surplus_config"]["enabled"] is True
    store.async_save.assert_awaited_once()
    refresh.assert_called_once_with()
    # No executor is required and no direct stop/start bypasses the coordinator.
    asyncio.run(actions.async_set_charging_mode(hass, entry, mode, False))
    assert store._data[key]["enabled"] is False


@pytest.mark.parametrize("mode", ["scheduled_charging", "price_level_charging"])
@pytest.mark.parametrize("previous", [None, {"enabled": True, "custom": 7}])
def test_save_failure_restores_original_and_does_not_refresh(actions, mode, previous):
    hass, entry, store, data = runtime()
    if previous is not None:
        store._data[mode] = previous
    store.async_save.side_effect = OSError("disk full")
    data["optimization_coordinator"] = SimpleNamespace(_schedule_settings_reoptimization=Mock())
    with pytest.raises(OSError, match="disk full"):
        asyncio.run(actions.async_update_charging_config(hass, entry, store, mode, {"enabled": False}))
    assert store._data.get(mode) == previous
    data["optimization_coordinator"]._schedule_settings_reoptimization.assert_not_called()


@pytest.mark.parametrize("mode,field", [
    ("scheduled_charging", "max_price_cents"), ("price_level_charging", "recovery_price_cents"),
    ("price_level_charging", "opportunity_price_cents"),
])
def test_mode_thresholds_reject_invalid_input_and_preserve_valid_legacy_values(actions, mode, field):
    hass, entry, store, _ = runtime()
    store._data[mode] = {field: "-2.25", "enabled": True, "preserve_home_battery": True}
    saved = asyncio.run(actions.async_update_charging_config(hass, entry, store, mode, {"no_grid_import": True}))
    assert saved[field] == -2.25 and saved["no_grid_import"] and not saved["preserve_home_battery"]
    for invalid in (None, True, "5", float("nan"), float("inf")):
        with pytest.raises(ValueError, match=field):
            asyncio.run(actions.async_update_charging_config(hass, entry, store, mode, {"enabled": False, field: invalid}))
        assert store._data[mode] == saved
        assert actions.charging_mode_settings(mode, {field: invalid if invalid != "5" else "garbage"})[field] == 0
    saved = asyncio.run(actions.async_update_charging_config(hass, entry, store, mode, {field: 0}))
    assert saved[field] == 0


@pytest.mark.parametrize("field,bad", [("enabled", "false"), ("start_time", "24:00"), ("end_time", "12:70")])
def test_invalid_scheduled_input_is_rejected_and_corrupt_stored_times_disable_the_mode(actions, field, bad):
    hass, entry, store, _ = runtime()
    with pytest.raises(ValueError, match=field):
        asyncio.run(actions.async_update_charging_config(hass, entry, store, "scheduled_charging", {field: bad}))
    assert store._data == {}
    stored = {"enabled": True, "start_time": "broken", "end_time": "2:00"}
    assert actions.charging_mode_settings("scheduled_charging", stored)["enabled"] is False
    saved = actions.charging_mode_settings("scheduled_charging", stored, {"start_time": "22:30"})
    assert saved["enabled"] and saved["end_time"] == "02:00"


def test_action_and_api_updates_serialize_and_keep_latest_options(actions):
    hass, entry, store, _ = runtime()

    async def run():
        entered, resume = asyncio.Event(), asyncio.Event()

        async def save():
            entered.set()
            await resume.wait()

        store.async_save.side_effect = save
        first = asyncio.create_task(actions.async_set_charging_mode(hass, entry, "scheduled", False))
        await entered.wait()
        second = asyncio.create_task(actions.async_update_charging_config(
            hass, entry, store, "scheduled_charging", {"start_time": "05:00", "max_price_cents": 0}))
        await asyncio.sleep(0)
        resume.set()
        await asyncio.gather(first, second)

    asyncio.run(run())
    assert store._data["scheduled_charging"]["enabled"] is False
    assert store._data["scheduled_charging"]["start_time"] == "05:00"
    assert store._data["scheduled_charging"]["max_price_cents"] == 0


def test_two_entries_are_independent_and_solar_uses_existing_cleanup(actions):
    hass, entry, store, _ = runtime()
    other = SimpleNamespace(_data={"scheduled_charging": {"enabled": True}}, async_save=AsyncMock())
    hass.data["power_sync"]["two"] = {"automation_store": other}
    asyncio.run(actions.async_set_charging_mode(hass, entry, "scheduled", False))
    assert other._data["scheduled_charging"]["enabled"] is True
    other.async_save.assert_not_called()
    asyncio.run(actions.async_set_charging_mode(hass, entry, "solar_surplus", False))
    actions.async_update_solar_surplus_config.assert_awaited_once_with(hass, entry, store, {"enabled": False})


def test_smart_schedule_requires_existing_vehicle_and_preserves_other_vehicle(actions):
    hass, entry, _, data = runtime()
    one, two = SimpleNamespace(enabled=True), SimpleNamespace(enabled=True)
    executor = SimpleNamespace(_settings={"VIN1": one, "VIN2": two})
    async def save_update(store, vehicle_id, updates):
        settings = executor._settings[vehicle_id]
        settings.enabled = updates["enabled"]
        return settings
    executor.async_save_settings_update = AsyncMock(side_effect=save_update)
    data.update(auto_schedule_executor=executor, store=object())
    with pytest.raises(RuntimeError, match="existing Smart Schedule"):
        asyncio.run(actions.async_set_charging_mode(hass, entry, "smart_schedule", False, "unknown"))
    executor.async_save_settings_update.assert_not_awaited()
    asyncio.run(actions.async_set_charging_mode(hass, entry, "smart_schedule", False, "VIN1"))
    assert one.enabled is False and two.enabled is True
    executor.async_save_settings_update.assert_awaited_once_with(data["store"], "VIN1", {"enabled": False})
    executor.async_save_settings_update.side_effect = OSError("disk full")
    with pytest.raises(OSError):
        asyncio.run(actions.async_set_charging_mode(hass, entry, "smart_schedule", True, "VIN1"))
    assert one.enabled is False


def test_registered_actions_validate_entry_and_route_each_mode(actions):
    hass, entry, store, data = runtime()
    registered = {}

    def register(domain, name, handler, **kwargs):
        registered[name] = (handler, kwargs["schema"])

    hass.services = SimpleNamespace(has_service=lambda domain, name: name in registered, async_register=register)
    hass.config_entries = SimpleNamespace(async_get_entry=lambda eid: entry if eid == "one" else None)
    actions.register_services(hass)
    actions.register_services(hass)
    assert len(registered) == 5
    for mode in ("scheduled", "price_level", "solar_surplus"):
        handler, schema = registered[f"set_{mode}_charging"]
        asyncio.run(handler(SimpleNamespace(data=schema({"entry_id": "one", "enabled": False}))))
        with pytest.raises(RuntimeError, match="loaded PowerSync"):
            asyncio.run(handler(SimpleNamespace(data={"entry_id": "other", "enabled": False})))
    assert store._data["scheduled_charging"]["enabled"] is False
    assert store._data["price_level_charging"]["enabled"] is False
    _, schema = registered["set_smart_schedule_charging"]
    with pytest.raises(vol.Invalid):
        schema({"entry_id": "one", "enabled": False})
    _, schema = registered["set_scheduled_charging"]
    with pytest.raises(vol.Invalid):
        schema({"entry_id": "one", "enabled": "not a boolean"})
    handler, schema = registered["set_ev_return"]
    assert schema({"entry_id": "one", "vehicle_id": "VIN1", "mode": "not_home"})["mode"] == "not_home"
    with pytest.raises(vol.Invalid):
        schema({"entry_id": "one", "vehicle_id": "VIN1", "mode": "tomorrow"})
    with pytest.raises(vol.Invalid):
        schema({"entry_id": "one", "mode": "auto"})
    with pytest.raises(RuntimeError, match="loaded PowerSync"):
        asyncio.run(handler(SimpleNamespace(data={"entry_id": "other", "vehicle_id": "VIN1", "mode": "auto"})))


@pytest.mark.parametrize("existing,hidden_by", [(False, None), (True, None), (True, "user")])
def test_legacy_solar_switch_keeps_identity_and_visibility_but_new_setups_use_actions(existing, hidden_by):
    tree = ast.parse((ROOT / "switch.py").read_text())
    helper = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_legacy_solar_surplus_switch")
    entity_id = "switch.renamed_solar" if existing else None
    registry = SimpleNamespace(
        async_get_entity_id=Mock(return_value=entity_id),
        async_get=Mock(return_value=SimpleNamespace(hidden_by=hidden_by)),
        async_update_entity=Mock(), async_remove=Mock(),
    )
    factory = Mock(return_value="legacy entity")
    ns = {"er": SimpleNamespace(async_get=lambda hass: registry,
                                RegistryEntryHider=SimpleNamespace(INTEGRATION="integration")),
          "DOMAIN": "power_sync", "SolarSurplusChargingSwitch": factory}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), "legacy switch", "exec"), ns)
    hass, entry = object(), SimpleNamespace(entry_id="one")
    result = ns["_legacy_solar_surplus_switch"](hass, entry)
    registry.async_get_entity_id.assert_called_once_with("switch", "power_sync", "one_solar_surplus_ev_charging")
    registry.async_remove.assert_not_called()
    if existing:
        assert result == "legacy entity"
        factory.assert_called_once_with(hass, entry)
        registry.async_update_entity.assert_not_called()
    else:
        assert result is None
        factory.assert_not_called()
        registry.async_update_entity.assert_not_called()


def test_actions_unregister_and_register_again_after_reload(actions):
    registered = {}
    hass = SimpleNamespace(services=SimpleNamespace(
        has_service=lambda domain, name: name in registered,
        async_register=lambda domain, name, handler, **kw: registered.update({name: handler}),
        async_remove=lambda domain, name: registered.pop(name),
    ))
    actions.register_services(hass)
    assert len(registered) == 5
    actions.unregister_services(hass)
    assert registered == {}
    actions.register_services(hass)
    assert len(registered) == 5


@pytest.mark.parametrize("remaining", [False, True])
def test_action_unload_ignores_global_metadata_but_preserves_other_entries(remaining):
    tree = ast.parse((ROOT / "__init__.py").read_text())
    unload = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_unload_entry")
    cleanup = next(n for n in unload.body if isinstance(n, ast.If)
                   and "_unregister_ev_charging_services" in ast.unparse(n))
    cleanup.body = [n for n in cleanup.body if not isinstance(n, ast.ImportFrom)]
    remove = Mock()
    unregister = Mock()
    data = {"automation_store": object()}
    if remaining:
        data["other"] = {}
    hass = SimpleNamespace(data={"power_sync": data}, services=SimpleNamespace(async_remove=remove),
                           config_entries=SimpleNamespace(async_entries=lambda domain: [SimpleNamespace(entry_id="other")]))
    ns = {"hass": hass, "DOMAIN": "power_sync", "SERVICE_SYNC_TOU": "sync_tou",
          "SERVICE_SYNC_NOW": "sync_now", "_unregister_ev_charging_services": unregister}
    exec(compile(ast.Module(body=[cleanup], type_ignores=[]), "unload", "exec"), ns)
    assert unregister.call_count == (0 if remaining else 1)


def test_not_home_default_lasts_until_next_morning(actions):
    from datetime import datetime, timedelta, timezone
    tz = timezone(timedelta(hours=10))
    evening = datetime(2026, 11, 28, 18, 0, tzinfo=tz)
    assert actions._default_not_home_until(evening) == datetime(2026, 11, 29, 10, 0, tzinfo=tz)
    early = datetime(2026, 11, 29, 6, 0, tzinfo=tz)
    assert actions._default_not_home_until(early) == datetime(2026, 11, 29, 10, 0, tzinfo=tz)
    late_morning = datetime(2026, 11, 29, 9, 30, tzinfo=tz)
    assert actions._default_not_home_until(late_morning) == datetime(2026, 11, 30, 10, 0, tzinfo=tz)
