"""Exercise the shipped advanced HTTP and administrator service boundaries."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import web
import pytest

ROOT = Path(__file__).resolve().parents[1]


def harness():
    source = ast.parse((ROOT / 'custom_components/power_sync/powerwall_local/views.py').read_text())
    names = {'_advanced_control_entry', 'PowerwallBatteryOutputView', 'PowerwallBatteryOutputStopView', 'PowerwallNativeSiteLimitsView', 'PowerwallBackupPreparationView'}
    tree = ast.Module(body=[n for n in source.body if getattr(n, 'name', None) in names], type_ignores=[])
    context = lambda **kw: SimpleNamespace(**kw)
    namespace = {'web': web, 'HomeAssistantView': object, 'HomeAssistantError': RuntimeError, 'DOMAIN': 'power_sync', 'Context': context}
    exec(compile(tree, '<advanced-views>', 'exec'), namespace)
    entries = [SimpleNamespace(entry_id=key, domain='power_sync') for key in ('a', 'b')]
    controls = {key: SimpleNamespace(read=AsyncMock(return_value={'site': key}), execute=AsyncMock(return_value={'success': True}), end=AsyncMock(return_value={'success': True})) for key in ('a', 'b')}
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda _: entries, async_get_entry=lambda key: next((e for e in entries if e.entry_id == key), None)),
                           data={'power_sync': {key: {'native_site_limits_control': controls[key], 'battery_output_control': controls[key], 'backup_preparation_control': controls[key]} for key in controls}})
    return namespace, hass, controls


class Request(dict):
    def __init__(self, data=None, user=None, query=None):
        super().__init__(hass_user=user)
        self.data, self.query = data, query or {}

    async def json(self):
        return self.data


@pytest.mark.asyncio
async def test_reads_refuse_ambiguous_entries_and_select_exact_site():
    n, hass, controls = harness()
    for name in ('PowerwallBatteryOutputView', 'PowerwallNativeSiteLimitsView', 'PowerwallBackupPreparationView'):
        view = n[name](hass)
        assert view.requires_auth is True
        assert (await view.get(Request())).status == 409
        assert (await view.get(Request(query={'entry_id': 'missing'}))).status == 409
        result = await view.get(Request(query={'entry_id': 'b'}))
        assert json.loads(result.body) == {'success': True, 'site': 'b', 'entry_id': 'b'}
    controls['a'].read.assert_not_awaited()
    hass.config_entries.async_entries = lambda _: [hass.config_entries.async_get_entry('a')]
    result = await n['PowerwallBatteryOutputView'](hass).get(Request())
    assert json.loads(result.body) == {'success': True, 'site': 'a', 'entry_id': 'a'}


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['approve', 'rebaseline'])
async def test_native_authority_changes_require_admin_before_controller(operation):
    n, hass, controls = harness()
    view = n['PowerwallNativeSiteLimitsView'](hass)
    body = {'entry_id': 'b', 'operation': operation}
    for user in (None, SimpleNamespace(id='user', is_admin=False)):
        assert (await view.post(Request(body, user))).status == 403
    controls['b'].execute.assert_not_awaited()
    assert (await view.post(Request(body, SimpleNamespace(id='admin', is_admin=True)))).status == 200
    controls['b'].execute.assert_awaited_once_with(body, actor='admin')
    controls['a'].execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_owned_stop_uses_exact_site_and_operation_without_revision():
    n, hass, controls = harness()
    body = {'entry_id': 'b', 'operation_id': 'owned-operation'}
    view = n['PowerwallBatteryOutputStopView'](hass)
    assert view.requires_auth is True
    assert (await view.post(Request(body, SimpleNamespace(id='user')))).status == 200
    sent, context = controls['b'].end.await_args.args
    assert sent == body and context.user_id == 'user'
    controls['a'].end.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_cannot_bypass_administrator_approval():
    source = ast.parse((ROOT / 'custom_components/power_sync/__init__.py').read_text())
    node = next(n for n in ast.walk(source) if isinstance(n, ast.AsyncFunctionDef) and n.name == 'handle_set_native_site_limits')
    auth = AsyncMock(return_value=SimpleNamespace(id='admin', is_admin=True))
    control = SimpleNamespace(execute=AsyncMock(return_value={'success': True}))
    namespace = {'hass': SimpleNamespace(auth=SimpleNamespace(async_get_user=auth)), '_limits_control': control, 'HomeAssistantError': RuntimeError, 'ServiceCall': object}
    source_helper = next(n for n in ast.walk(source) if isinstance(n, ast.FunctionDef) and n.name == '_control_call_source')
    exec(compile(ast.Module(body=[source_helper, node], type_ignores=[]), '<native-limit-service>', 'exec'), namespace)
    call = SimpleNamespace(data={'operation': 'approve'}, context=SimpleNamespace(user_id=None))
    with pytest.raises(RuntimeError, match='administrator'):
        await namespace['handle_set_native_site_limits'](call)
    control.execute.assert_not_awaited()
    call.context.user_id = 'admin'
    assert await namespace['handle_set_native_site_limits'](call) == {'success': True}
    control.execute.assert_awaited_once_with({'operation': 'approve'}, actor='admin', automatic=False)
    control.execute.reset_mock()
    call.data = {'export_limit_w': 0}
    call.context.user_id = None
    assert await namespace['handle_set_native_site_limits'](call) == {'success': True}
    control.execute.assert_awaited_once_with({'export_limit_w': 0}, actor=None, automatic=True)


@pytest.mark.asyncio
async def test_backup_writes_select_exact_entry_and_preserve_authenticated_context():
    n, hass, controls = harness()
    view = n['PowerwallBackupPreparationView'](hass)
    user = SimpleNamespace(id='owner')
    assert (await view.post(Request({'request_id':'start','duration_minutes':60}, user))).status == 409
    body = {'entry_id':'b','request_id':'start','duration_minutes':60}
    assert (await view.post(Request(body, user))).status == 200
    args = controls['b'].execute.await_args.args
    assert args[0] == body and args[1].user_id == 'owner'
    controls['a'].execute.assert_not_awaited()
