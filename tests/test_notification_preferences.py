"""Device preferences survive restart and filter the actual Expo destination list."""

from __future__ import annotations

import ast
import copy
import json
import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"
PACKAGE = "power_sync_notification_fixture"


class MemoryStore:
    def __init__(self, hass, *args, **kwargs):
        self.hass = hass

    async def async_load(self):
        return copy.deepcopy(self.hass.saved)

    async def async_save(self, value):
        self.hass.saved = copy.deepcopy(value)


@pytest.fixture
def notifications(monkeypatch):
    package = ModuleType(PACKAGE)
    package.__path__ = []
    monkeypatch.setitem(sys.modules, PACKAGE, package)
    constants = ModuleType(f"{PACKAGE}.const")
    constants.DOMAIN = "power_sync"
    monkeypatch.setitem(sys.modules, constants.__name__, constants)
    module = ModuleType(f"{PACKAGE}.notification_preferences")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    tree = ast.parse((ROOT / "notification_preferences.py").read_text())
    omitted = {"homeassistant.components.http", "homeassistant.core", "homeassistant.helpers.storage"}
    tree.body = [
        node for node in tree.body
        if not isinstance(node, ast.ImportFrom) or node.module not in omitted
    ]
    module.__dict__.update(HomeAssistantView=object, HomeAssistant=object, Store=MemoryStore)
    exec(compile(tree, "notification_preferences.py", "exec"), module.__dict__)
    return module


def _sender():
    path = ROOT / "automations" / "actions.py"
    tree = ast.parse(path.read_text())
    tree.body = [node for node in tree.body if getattr(node, "name", None) == "_send_expo_push"]
    namespace = {
        "__package__": f"{PACKAGE}.automations",
        "HomeAssistant": object,
        "_LOGGER": logging.getLogger(__name__),
    }
    exec(compile(tree, str(path), "exec"), namespace)
    return namespace["_send_expo_push"]


@pytest.mark.asyncio
async def test_device_preferences_filter_expo_after_restart(monkeypatch, notifications):
    muted = "ExponentPushToken[muted]"
    active = "ExponentPushToken[active]"
    hass = SimpleNamespace(data={}, saved=None)
    prefs = await notifications.async_get_notification_preferences(hass)
    disabled = notifications.normalize_disabled_notification_types(["grid_outage", "grid_outage", "future_type"])
    await prefs.async_set_device_types(muted, disabled)
    hass.data = {"power_sync": {"push_tokens": {
        muted: {"token": muted}, active: {"token": active},
    }}}
    reloaded = await notifications.async_get_notification_preferences(hass)
    assert reloaded.disabled_by_token == {muted: ["future_type", "grid_outage"]}

    destinations = []
    response = MagicContext(SimpleNamespace(status=200, text=AsyncMock(return_value="{}"), json=AsyncMock(return_value={})))

    def post(_url, *, json, **_kwargs):
        destinations.extend(message["to"] for message in json)
        return response

    monkeypatch.setattr(aiohttp, "ClientSession", lambda: MagicContext(SimpleNamespace(post=post)))
    sender = _sender()
    await sender(hass, "Grid outage", "An outage", notification_type="grid_outage")
    assert destinations == [active]
    destinations.clear()
    await sender(hass, "Grid restored", "Restored", notification_type="grid_restored")
    assert destinations == [muted, active]
    destinations.clear()

    await reloaded.async_set_device_types(muted, [])
    await sender(hass, "Grid outage", "An outage", notification_type="grid_outage")
    assert destinations == [muted, active]


class MagicContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return None


class Request(dict):
    def __init__(self, payload=None, *, admin=True):
        super().__init__(hass_user=SimpleNamespace(is_admin=admin))
        self.payload = payload

    async def json(self):
        return self.payload


@pytest.mark.asyncio
async def test_ha_update_notice_requires_admin_and_survives_restart(notifications):
    hass = SimpleNamespace(data={}, saved=None)
    view = notifications.NotificationSettingsView(hass)
    assert json.loads((await view.get(Request(admin=False))).body) == {
        "update_alerts_enabled": True, "can_edit": False,
    }
    assert (await view.post(Request({"update_alerts_enabled": False}, admin=False))).status == 403
    assert (await view.post(Request({"update_alerts_enabled": "false"}))).status == 400
    assert notifications.update_alerts_enabled(hass)
    assert (await view.post(Request({"update_alerts_enabled": False}))).status == 200

    restarted = SimpleNamespace(data={}, saved=hass.saved)
    await notifications.async_get_notification_preferences(restarted)
    assert not notifications.update_alerts_enabled(restarted)
    assert notifications.push_notification_enabled(restarted, "ExponentPushToken[active]", "automation")
    assert (await notifications.NotificationSettingsView(restarted).post(Request({"update_alerts_enabled": True}))).status == 200
    assert notifications.update_alerts_enabled(restarted)


@pytest.mark.parametrize("value", [None, "automation", [1], ["Automation"], [" "]])
def test_rejects_malformed_notification_preferences(notifications, value):
    with pytest.raises(ValueError, match="invalid_disabled_notification_types"):
        notifications.normalize_disabled_notification_types(value)


@pytest.mark.asyncio
async def test_power_sync_update_entity_emits_only_when_update_alert_enabled(notifications):
    path = ROOT / "update.py"
    tree = ast.parse(path.read_text())
    tree.body = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_check_and_notify"
    ]
    namespace = {"__package__": PACKAGE, "_LOGGER": logging.getLogger(__name__)}
    exec(compile(tree, str(path), "exec"), namespace)
    send = AsyncMock()
    scheduled = []
    hass = SimpleNamespace(data={}, saved=None, services=SimpleNamespace(async_call=send),
                           async_create_task=scheduled.append)
    entity = SimpleNamespace(
        hass=hass,
        coordinator=SimpleNamespace(data={"latest_version": "2.12.1901"}),
        _installed="2.12.1900", _notified_version=None,
    )
    preferences = await notifications.async_get_notification_preferences(hass)
    await preferences.async_set_update_alerts(False)
    namespace["_check_and_notify"](entity)
    assert not scheduled

    await preferences.async_set_update_alerts(True)
    namespace["_check_and_notify"](entity)
    assert len(scheduled) == 1
    await scheduled.pop()
    args = send.await_args.args
    assert args[:2] == ("persistent_notification", "create")
    assert args[2]["notification_id"] == "power_sync_update_2.12.1901"
