"""Exercise the Solar Surplus entity/API shared control without a HA runtime."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import logging
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

ROOT = Path(__file__).resolve().parents[1] / "custom_components/power_sync"


@pytest.fixture
def control(monkeypatch):
    # Load real pure config helpers, without installing any shared HA stubs.
    spec = importlib.util.spec_from_file_location("_solar_config_test", ROOT / "solar_surplus_config.py")
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)
    monkeypatch.setitem(sys.modules, "_solar_switch_test.solar_surplus_config", config)
    actions = types.ModuleType("_solar_switch_test.automations.actions")
    actions.stop_solar_surplus_ev_charging = AsyncMock(return_value=True)
    monkeypatch.setitem(sys.modules, actions.__name__, actions)
    events = []
    listeners = []

    def send(hass, signal, entry_id):
        events.append(entry_id)
        for listener in listeners:
            listener(entry_id)

    namespace = dict(vars(config))
    namespace.update({
        "__package__": "_solar_switch_test", "__spec__": None, "asyncio": asyncio,
        "logging": logging, "Any": Any, "DOMAIN": "power_sync",
        "async_dispatcher_send": send,
    })
    source = (ROOT / "solar_surplus_control.py").read_text()
    tree = ast.parse(source)
    tree.body = [node for node in tree.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
    exec(compile(tree, str(ROOT / "solar_surplus_control.py"), "exec"), namespace)
    control_module = types.ModuleType("_solar_switch_test.solar_surplus_control")
    control_module.async_update_solar_surplus_config = namespace["async_update_solar_surplus_config"]
    monkeypatch.setitem(sys.modules, control_module.__name__, control_module)

    class Entity:
        async_write_ha_state = Mock()

        async def async_added_to_hass(self):
            pass

        def async_on_remove(self, remove):
            self.remove_listener = remove

    def connect(hass, signal, listener):
        listeners.append(listener)
        return lambda: listeners.remove(listener)

    namespace.update({
        "SwitchEntity": Entity, "callback": lambda fn: fn,
        "HomeAssistantError": RuntimeError,
        "async_dispatcher_connect": connect,
    })
    source = (ROOT / "switch.py").read_text()
    node = next(n for n in ast.parse(source).body
                if isinstance(n, ast.ClassDef) and n.name == "SolarSurplusChargingSwitch")
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(ROOT / "switch.py"), "exec"), namespace)
    store = types.SimpleNamespace(_data={}, async_save=AsyncMock())
    entry = types.SimpleNamespace(entry_id="one")
    hass = types.SimpleNamespace(data={"power_sync": {"one": {"automation_store": store}}})
    switch = namespace["SolarSurplusChargingSwitch"](hass, entry)
    return types.SimpleNamespace(
        update=namespace["async_update_solar_surplus_config"],
        stop=actions.stop_solar_surplus_ev_charging, events=events,
        hass=hass, entry=entry, store=store, switch=switch, namespace=namespace,
    )


def test_default_enable_disable_preserves_settings_and_stops_after_save(control):
    c = control
    c.store._data = {"other": "preserved", "solar_surplus_config": {"home_battery_minimum": 42}}
    order = []
    c.store.async_save.side_effect = lambda: order.append("save")
    c.stop.side_effect = lambda *args: order.append("stop") or True

    async def run():
        assert c.switch.available and not c.switch.is_on
        await c.switch.async_turn_on()
        assert c.switch.is_on
        c.stop.assert_not_called()
        await c.switch.async_turn_off()
        assert not c.switch.is_on

    asyncio.run(run())
    assert order == ["save", "save", "stop"]
    assert c.store._data["other"] == "preserved"
    assert c.store._data["solar_surplus_config"]["home_battery_minimum"] == 42
    c.stop.assert_awaited_once_with(c.hass, c.entry)


def test_app_update_notifies_only_matching_entity_and_survives_recreation(control):
    c = control

    async def run():
        await c.switch.async_added_to_hass()
        c.switch.async_write_ha_state.reset_mock()
        c.switch._config_updated("other")
        c.switch.async_write_ha_state.assert_not_called()
        await c.update(c.hass, c.entry, c.store, {"enabled": True, "min_battery_soc": 30})
        c.switch.async_write_ha_state.assert_called_once()
        restored = type(c.switch)(c.hass, c.entry)
        assert restored.is_on
        assert c.store._data["solar_surplus_config"]["home_battery_minimum"] == 30
        c.switch.remove_listener()

    asyncio.run(run())


def test_missing_store_is_unavailable_and_cannot_toggle(control):
    c = control
    c.hass.data["power_sync"]["one"].clear()
    assert not c.switch.available
    with pytest.raises(RuntimeError, match="storage is not available"):
        asyncio.run(c.switch.async_turn_on())
    c.stop.assert_not_called()
    c.hass.data["power_sync"]["one"]["automation_store"] = c.store
    assert c.switch.available


@pytest.mark.parametrize("previous", [None, {"enabled": True, "household_buffer_kw": 1.2}])
def test_save_failure_restores_state_without_stop_or_notification(control, previous):
    c = control
    if previous is not None:
        c.store._data["solar_surplus_config"] = previous
    c.store.async_save.side_effect = OSError("disk full")
    with pytest.raises(RuntimeError, match="disk full"):
        asyncio.run(c.switch.async_turn_off())
    assert c.store._data.get("solar_surplus_config") == previous
    c.stop.assert_not_called()
    assert c.events == []


def test_failed_hardware_stop_keeps_disabled_setting_and_logs(control, caplog):
    c = control
    c.store._data["solar_surplus_config"] = {"enabled": True}
    c.stop.return_value = False
    with caplog.at_level(logging.WARNING):
        asyncio.run(c.switch.async_turn_off())
    assert not c.switch.is_on
    assert "charger stop commands failed" in caplog.text
    assert c.events == ["one"]


def test_concurrent_api_and_switch_writes_preserve_updates(control):
    c = control

    async def run():
        entered, resume = asyncio.Event(), asyncio.Event()

        async def save():
            entered.set()
            await resume.wait()

        c.store.async_save.side_effect = save
        first = asyncio.create_task(c.update(c.hass, c.entry, c.store, {"household_buffer_kw": 2}))
        await entered.wait()
        second = asyncio.create_task(c.switch.async_turn_on())
        await asyncio.sleep(0)
        resume.set()
        await asyncio.gather(first, second)

    asyncio.run(run())
    assert c.switch.is_on
    assert c.store._data["solar_surplus_config"]["household_buffer_kw"] == 2


def test_api_post_and_get_share_switch_state(control):
    from aiohttp import web
    import json

    c = control
    c.namespace.update({"HomeAssistantView": object, "web": web})
    source = (ROOT / "__init__.py").read_text()
    node = next(n for n in ast.parse(source).body
                if isinstance(n, ast.ClassDef) and n.name == "SolarSurplusConfigView")
    exec(compile(ast.Module(body=[node], type_ignores=[]), "solar_api", "exec"), c.namespace)
    c.hass.config_entries = types.SimpleNamespace(async_get_entry=lambda entry_id: c.entry)
    view = c.namespace["SolarSurplusConfigView"](c.hass)

    async def run():
        response = await view.post(types.SimpleNamespace(json=AsyncMock(return_value={"enabled": True})))
        assert response.status == 200
        assert json.loads(response.text)["config"]["enabled"] is True
        assert c.switch.is_on
        await c.switch.async_turn_off()
        response = await view.get(None)
        assert response.status == 200
        assert json.loads(response.text)["config"]["enabled"] is False

    asyncio.run(run())


def test_two_entries_have_independent_settings_and_cleanup(control):
    c = control
    other_store = types.SimpleNamespace(_data={"solar_surplus_config": {"enabled": True}}, async_save=AsyncMock())
    c.hass.data["power_sync"]["two"] = {"automation_store": other_store}
    other = type(c.switch)(c.hass, types.SimpleNamespace(entry_id="two"))
    asyncio.run(c.switch.async_turn_on())
    asyncio.run(c.switch.async_turn_off())
    assert other.is_on
    other_store.async_save.assert_not_called()
    c.stop.assert_awaited_once_with(c.hass, c.entry)


def test_invalid_setting_does_not_persist_or_change_runtime(control):
    c = control
    with pytest.raises(ValueError):
        asyncio.run(c.update(c.hass, c.entry, c.store, {"household_buffer_kw": "invalid"}))
    assert c.store._data == {}
    c.store.async_save.assert_not_called()
    c.stop.assert_not_called()


def test_switch_is_registered_without_provider_or_brand_gate():
    tree = ast.parse((ROOT / "switch.py").read_text())
    setup = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_setup_entry")
    assert any(isinstance(n, ast.Expr) and "SolarSurplusChargingSwitch(hass, entry)" in ast.unparse(n)
               for n in setup.body)
