"""Scheduled native discharge refusals must reach the user without alert spam."""
import ast
import asyncio
import importlib
import logging
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

ROOT = Path(__file__).parents[1] / 'custom_components/power_sync'


@pytest.mark.asyncio
@pytest.mark.parametrize('direction,source,allowed,expected', [
    ('discharge', 'optimizer', False, 'native_export_rule_invalid'),
    ('charge', 'optimizer', False, 'unused'),
    ('discharge', 'user', False, 'unused'),
    ('discharge', 'optimizer', True, None),
    ('discharge', 'user', True, None),
])
async def test_native_route_notifies_scheduled_discharge_admission_failure(
        monkeypatch, direction, source, allowed, expected):
    package = 'native_alert_route_probe'
    parent = ModuleType(package)
    parent.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, package, parent)
    host = ModuleType(package + '.powerwall_host')
    host.local_battery_dispatch_configured = lambda _: True
    monkeypatch.setitem(sys.modules, host.__name__, host)
    alerts = []
    notifications = ModuleType(package + '.powerwall_local.native_max_notifications')
    notifications.update_native_discharge_alert = lambda *args: alerts.append(args)
    monkeypatch.setitem(sys.modules, notifications.__name__, notifications)
    adapter = SimpleNamespace(guard=AsyncMock(return_value=allowed),
                              guard_denial_reason='native_export_rule_invalid')
    controller = SimpleNamespace(pending=False, record=None, adapter=adapter,
                                 start=AsyncMock(), stop=AsyncMock())
    hass = SimpleNamespace()
    scope = dict(__package__=package, _LOGGER=logging.getLogger(__name__),
        _control_call_source=lambda call: source,
        _powerwall_prefers_native_tariff=lambda direction: True,
        entry=SimpleNamespace(entry_id='one', data={}), hass=hass,
        _get_tesla_site_configs=lambda *_: [('site', 'token', 'provider')],
        _local_powerwall_runtime={}, _local_powerwall_route_lock=asyncio.Lock(),
        _stop_local_powerwall_dispatch=AsyncMock(),
        force_charge_state={}, force_discharge_state={},
        hold_soc_state={}, self_consumption_state={},
        _get_native_max_charge=AsyncMock(return_value=controller),
        _native_max_context={}, HomeAssistantError=RuntimeError,
        _create_discharge_tariff=Mock(side_effect=RuntimeError('tariff boundary')))
    tree = ast.parse((ROOT / '__init__.py').read_text())
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
                and n.name == '_try_native_powerwall_force')
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<native-route>', 'exec'), scope)
    with pytest.raises(RuntimeError, match='tariff boundary' if allowed else 'native_export_rule_invalid'):
        await scope['_try_native_powerwall_force'](
            direction, SimpleNamespace(data={'power_w': 0}), 5, 2000)
    assert alerts == ([(hass, 'one', expected)] if expected != 'unused' else [])
    controller.start.assert_not_awaited()


@pytest.fixture
def notification_probe(monkeypatch):
    package = 'native_alert_helper_probe'
    for name, path in [(package, ROOT),
                       (package + '.powerwall_local', ROOT / 'powerwall_local')]:
        module = ModuleType(name)
        module.__path__ = [str(path)]
        monkeypatch.setitem(sys.modules, name, module)
    constants = ModuleType(package + '.const')
    constants.DOMAIN = 'power_sync'
    monkeypatch.setitem(sys.modules, constants.__name__, constants)
    actions = ModuleType(package + '.automations.actions')
    actions._send_expo_push = AsyncMock()
    monkeypatch.setitem(sys.modules, actions.__name__, actions)
    persistent = SimpleNamespace(async_create=Mock(), async_dismiss=Mock())
    components = ModuleType('homeassistant.components')
    components.persistent_notification = persistent
    monkeypatch.setitem(sys.modules, components.__name__, components)
    module = importlib.import_module(package + '.powerwall_local.native_max_notifications')
    queued = []
    hass = SimpleNamespace(data={'power_sync': {'push_tokens': {'device': {}}}},
                           async_create_task=queued.append)
    yield SimpleNamespace(hass=hass, update=lambda reason, entry='one':
                          module.update_native_discharge_alert(hass, entry, reason),
                          persistent=persistent, push=actions._send_expo_push,
                          queued=queued)
    for pending in queued:
        pending.close()


@pytest.mark.asyncio
async def test_retries_and_integration_reload_share_one_notification(notification_probe):
    p = notification_probe
    p.update('native_export_rule_invalid')
    p.hass.data['power_sync']['one'] = {'replacement_runtime': True}
    p.update('native_export_rule_invalid')
    p.update('native_export_rule_invalid')
    p.persistent.async_create.assert_called_once()
    assert len(p.queued) == 1
    await p.queued[0]
    p.push.assert_awaited_once()
    assert 'did not start' in p.push.await_args.args[2]
    assert 'Grid Export' in p.push.await_args.args[2]


@pytest.mark.asyncio
async def test_recovery_clears_alert_and_supersedes_queued_push(notification_probe):
    p = notification_probe
    p.update('native_export_rule_invalid')
    p.update(None)
    p.update(None)
    p.persistent.async_dismiss.assert_called_once_with(
        p.hass, 'power_sync_one_native_discharge_blocked')
    await p.queued[0]
    p.push.assert_not_awaited()
    p.update('native_export_rule_invalid')
    await p.queued[1]
    assert p.persistent.async_create.call_count == 2
    p.push.assert_awaited_once()


@pytest.mark.asyncio
async def test_distinct_entries_get_independent_alerts(notification_probe):
    p = notification_probe
    p.update('native_export_rule_invalid')
    p.update('native_export_rule_invalid', entry='two')
    p.update(None)
    await asyncio.gather(*p.queued)
    p.push.assert_awaited_once()
    ids = [c.kwargs['notification_id'] for c in p.persistent.async_create.call_args_list]
    assert len(set(ids)) == 2


def test_ordinary_guard_refusals_do_not_generate_alerts(notification_probe):
    p = notification_probe
    for reason in ['discharge_reserve_floor_reached', 'native_snapshot_stale',
                   'managed_ev_ownership_present', None]:
        p.update(reason)
    p.persistent.async_create.assert_not_called()
    assert not p.queued


def test_home_assistant_alert_does_not_require_mobile_tokens(notification_probe):
    p = notification_probe
    p.hass.data['power_sync']['push_tokens'] = {}
    p.update('native_export_rule_invalid')
    p.persistent.async_create.assert_called_once()
    assert not p.queued


@pytest.mark.asyncio
async def test_notification_failures_do_not_escape_or_repeat(notification_probe):
    p = notification_probe
    p.persistent.async_create.side_effect = RuntimeError('notification unavailable')
    p.push.side_effect = OSError('push unavailable')
    p.update('native_export_rule_invalid')
    await p.queued[0]
    p.update('native_export_rule_invalid')
    p.persistent.async_create.assert_called_once()
    p.push.assert_awaited_once()


def test_push_scheduling_failure_cannot_replace_control_error(notification_probe):
    p = notification_probe
    p.hass.async_create_task = Mock(side_effect=RuntimeError('setup stopping'))
    p.update('native_export_rule_invalid')
    p.persistent.async_create.assert_called_once()
    assert not p.queued
