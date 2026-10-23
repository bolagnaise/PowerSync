"""Native permission switch: real readback, no optimistic PV claims."""
import ast
import asyncio
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock
import pytest


def fixture(monkeypatch):
    class Entity:
        _context = SimpleNamespace(user_id="owner")
        def async_write_ha_state(self): pass
    client = SimpleNamespace(local_access_enabled=True, get_on_grid_solar_curtailment=AsyncMock(return_value=True))
    views = ModuleType("permission_switch_fixture.powerwall_local.views")
    views.ensure_client = AsyncMock(return_value=client)
    monkeypatch.setitem(sys.modules, views.__name__, views)
    namespace = dict(__name__="permission_switch_fixture.switch", __package__="permission_switch_fixture",
        SwitchEntity=Entity, HomeAssistant=object, ConfigEntry=object, EntityCategory=SimpleNamespace(CONFIG="config"),
        Any=object, DOMAIN="power_sync", CONF_POWERWALL_LOCAL_PAIRED="paired", SENSOR_FAMILY_BATTERY="battery",
        family_device_info=lambda *args: {}, _LOGGER=SimpleNamespace(debug=lambda *args, **kwargs: None))
    path = Path(__file__).resolve().parents[1] / "custom_components/power_sync/switch.py"
    node = next(n for n in ast.parse(path.read_text()).body if isinstance(n, ast.ClassDef) and n.name == "OnGridSolarCurtailmentSwitch")
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    entry = SimpleNamespace(entry_id="target", data={"paired": True})
    hass = SimpleNamespace(data={"power_sync": {"target": {}}}, services=SimpleNamespace(async_call=AsyncMock()))
    return namespace[node.name](hass, entry), client


def test_switch_reads_permission_and_unknown_is_unavailable(monkeypatch):
    switch, client = fixture(monkeypatch)
    assert switch.is_on is None and not switch.available
    asyncio.run(switch.async_update())
    assert switch.is_on is True and switch.available
    client.get_on_grid_solar_curtailment.return_value = False
    asyncio.run(switch.async_update())
    assert switch.is_on is False and switch.available
    client.get_on_grid_solar_curtailment.side_effect = TimeoutError()
    asyncio.run(switch.async_update())
    assert switch.is_on is None and not switch.available


def test_switch_routes_exact_entry_and_user_context_without_optimism(monkeypatch):
    switch, client = fixture(monkeypatch)
    client.get_on_grid_solar_curtailment.return_value = False
    asyncio.run(switch.async_turn_on())
    switch.hass.services.async_call.assert_awaited_once_with("power_sync", "powerwall_set_on_grid_solar_curtailment",
        {"entry_id": "target", "enabled": True}, blocking=True, context=switch._context)
    assert switch.is_on is False, "the requested state must not replace actual gateway readback"
    switch.hass.services.async_call.side_effect = RuntimeError("readback failed")
    client.get_on_grid_solar_curtailment.side_effect = TimeoutError()
    with pytest.raises(RuntimeError): asyncio.run(switch.async_turn_off())
    assert not switch.available


def test_late_read_and_reload_cannot_publish_stale_permission(monkeypatch):
    async def run():
        switch, client = fixture(monkeypatch)
        arrived, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def read():
            nonlocal calls
            calls += 1
            if calls == 1:
                arrived.set(); await release.wait(); return True
            return False
        client.get_on_grid_solar_curtailment.side_effect = read
        first = asyncio.create_task(switch.async_update()); await arrived.wait()
        await switch.async_update(); release.set(); await first
        assert switch.is_on is False
        async def reload():
            switch.hass.data["power_sync"]["target"] = {}
            return True
        client.get_on_grid_solar_curtailment.side_effect = reload
        await switch.async_update()
        assert not switch.available
    asyncio.run(run())
