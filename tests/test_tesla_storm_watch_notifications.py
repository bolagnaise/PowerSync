"""Storm Watch activation alerts use observed protection, without poll spam."""
import asyncio
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

ROOT = Path(__file__).parents[1] / "custom_components/power_sync"


@pytest.fixture
def probe(monkeypatch):
    package = "storm_alert_probe"
    root = ModuleType(package)
    root.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, package, root)
    const = ModuleType(package + ".const")
    const.DOMAIN = "power_sync"
    monkeypatch.setitem(sys.modules, const.__name__, const)
    actions = ModuleType(package + ".automations.actions")
    actions._send_expo_push = AsyncMock()
    monkeypatch.setitem(sys.modules, actions.__name__, actions)
    persistent = SimpleNamespace(async_create=Mock(), async_dismiss=Mock())
    components = ModuleType("homeassistant.components")
    components.persistent_notification = persistent
    monkeypatch.setitem(sys.modules, components.__name__, components)
    module = importlib.import_module(package + ".tesla_storm_watch")
    queued = []
    hass = SimpleNamespace(data={"power_sync": {"push_tokens": {"device": {}}}},
                           async_create_task=queued.append)
    yield SimpleNamespace(
        hass=hass, persistent=persistent, push=actions._send_expo_push, queued=queued,
        update=lambda active, entry="one", site="site": module.update_storm_watch_alert(
            hass, entry, site, active))
    for pending in queued:
        pending.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_sample", [False, True])
async def test_activation_notifies_once_including_already_active_startup(probe, first_sample):
    p = probe
    p.update(first_sample)
    p.update(True)
    p.update(True)
    p.hass.data["power_sync"]["one"] = {"replacement_runtime": True}
    p.update(True)
    p.persistent.async_create.assert_called_once()
    assert len(p.queued) == 1
    await p.queued[0]
    p.push.assert_awaited_once()
    assert p.push.await_args.args[1] == "Storm Watch Active"
    assert "Tesla has activated" in p.push.await_args.args[2]
    assert "pause normal battery discharge" in p.push.await_args.args[2]


@pytest.mark.asyncio
async def test_explicit_clear_dismisses_and_rearms_but_supersedes_old_push(probe):
    p = probe
    p.update(True)
    p.update(False)
    p.update(False)
    p.persistent.async_dismiss.assert_called_once()
    p.update(True)
    await asyncio.gather(*p.queued)
    p.push.assert_awaited_once()
    assert p.persistent.async_create.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [None, "false", "true", 0, 1, {}, []])
async def test_unknown_or_malformed_state_neither_activates_nor_rearms(probe, unknown):
    p = probe
    p.update(unknown)
    p.persistent.async_create.assert_not_called()
    p.update(True)
    p.update(unknown)
    p.update(True)
    await p.queued[0]
    p.push.assert_awaited_once()
    p.persistent.async_create.assert_called_once()
    p.persistent.async_dismiss.assert_not_called()


@pytest.mark.asyncio
async def test_sites_and_entries_have_independent_notifications(probe):
    p = probe
    p.update(True)
    p.update(True, site="other-site")
    p.update(True, entry="two")
    p.update(False)
    await asyncio.gather(*p.queued)
    assert p.push.await_count == 2
    ids = [c.kwargs["notification_id"] for c in p.persistent.async_create.call_args_list]
    assert len(set(ids)) == 3
    assert p.persistent.async_dismiss.call_args.args[1] == ids[0]


def test_home_assistant_alert_does_not_require_mobile_registration(probe):
    p = probe
    p.hass.data["power_sync"]["push_tokens"] = {}
    p.update(True)
    p.persistent.async_create.assert_called_once()
    assert not p.queued


@pytest.mark.asyncio
async def test_notification_errors_do_not_break_telemetry_or_spam_retries(probe):
    p = probe
    p.persistent.async_create.side_effect = RuntimeError("notification unavailable")
    p.push.side_effect = RuntimeError("push unavailable")
    p.update(True)
    await p.queued[0]
    p.update(True)
    p.persistent.async_create.assert_called_once()
    p.push.assert_awaited_once()


def test_failed_task_submission_closes_coroutine_and_retains_deduplication(probe):
    p = probe
    p.hass.async_create_task = Mock(side_effect=RuntimeError("stopping"))
    p.update(True)
    p.update(True)
    p.hass.async_create_task.assert_called_once()
    p.persistent.async_create.assert_called_once()
