"""Execute the real charge-kick lifecycle with fake transport and time."""
from __future__ import annotations

import ast
import asyncio
import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def kick_harness(monkeypatch):
    source = Path('custom_components/power_sync/__init__.py').read_text()
    node = next(n for n in ast.walk(ast.parse(source))
                if isinstance(n, ast.AsyncFunctionDef) and n.name == '_tesla_charge_kick')

    async def run(failure=None, failing_bounce=2, supersede=None, charging=False,
                  full=False, restore_fails=False):
        mode = 'autonomous'
        bounce = 0
        elapsed = 0
        polls = 0
        writes, restores, pushes, pending = [], [], [], []
        command_generation = [1]
        force_state = {'active': True, 'saved_operation_mode': 'autonomous'}

        class Response:
            def __init__(self, status=200, data=None):
                self.status, self.data = status, data
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def json(self):
                return self.data
            async def text(self):
                return 'synthetic failure'

        class Session:
            def post(self, url, *, json, **kwargs):
                nonlocal mode, bounce
                requested = json['default_real_mode']
                if requested == 'self_consumption':
                    bounce += 1
                writes.append((bounce, requested))
                if bounce == failing_bounce and requested == 'autonomous' and failure == 'post':
                    return Response(503)
                mode = requested
                return Response()
            def get(self, url, **kwargs):
                observed = ('self_consumption' if bounce == failing_bounce and failure == 'readback'
                            else mode)
                return Response(data={'response': {'default_real_mode': observed}})

        async def sleep(seconds):
            nonlocal elapsed
            elapsed += seconds
            if supersede == 'bounce' and bounce == 2 and seconds == 5:
                command_generation[0] += 1

        async def live_status():
            nonlocal polls
            polls += 1
            return {'battery_power': -1000 if charging else 0,
                    'battery_soc': 100 if full else 50}

        async def grid(*args, **kwargs):
            return not (bounce == failing_bounce and failure == 'grid')

        async def restore(domain, service, payload, **kwargs):
            nonlocal mode
            restores.append((domain, service, payload, kwargs))
            if restore_fails:
                raise RuntimeError('restore pending')
            mode = force_state['saved_operation_mode']
            force_state['active'] = False
            command_generation[0] += 1

        async def push(*args):
            pushes.append(args[-1])
            if supersede == 'notification':
                command_generation[0] += 1

        actions = ModuleType('custom_components.power_sync.automations.actions')
        actions._send_expo_push = push
        monkeypatch.setitem(sys.modules, actions.__name__, actions)
        hass = SimpleNamespace(data={'power_sync': {'entry': {}}},
                               services=SimpleNamespace(async_call=restore),
                               async_create_task=pending.append)
        namespace = dict(__package__='custom_components.power_sync',
                         _LOGGER=logging.getLogger(__name__),
                         asyncio=SimpleNamespace(sleep=sleep),
                         aiohttp=SimpleNamespace(ClientTimeout=lambda **kw: None),
                         hass=hass, entry=SimpleNamespace(entry_id='entry', data={'site': 'test'}),
                         DOMAIN='power_sync', CONF_TESLA_ENERGY_SITE_ID='site',
                         CONF_FLEET_API_BASE_URL='base', SERVICE_RESTORE_NORMAL='restore_normal',
                         _tesla_charge_kick_generation=[1], _command_generation=command_generation,
                         _tesla_operation_generation=[1],
                         _demand_grid_charging_protection_active=lambda: False,
                         get_live_status=live_status, token_getter=lambda: ('fake', 'fake'),
                         async_get_clientsession=lambda _: Session(),
                         get_tesla_api_base_url=lambda *args: 'https://example.invalid',
                         _tesla_force_apply_grid_charging=grid,
                         _tesla_force_result_all_confirmed=lambda result, targets: result,
                         _tesla_force_result_all_grid_field_absent_safe=lambda *args: False)
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<charge-kick>', 'exec'), namespace)
        await namespace['_tesla_charge_kick']('force_charge', kick_generation=1,
                                              command_generation=1, operation_generation=1,
                                              initial_delay_seconds=60)
        while pending:
            await pending.pop(0)
        return SimpleNamespace(mode=mode, writes=writes, restores=restores,
                               pushes=pushes, polls=polls, elapsed=elapsed,
                               force_state=force_state)
    return run


@pytest.mark.parametrize('failure', ['post', 'readback', 'grid'])
@pytest.mark.parametrize('failing_bounce', [1, 2])
def test_failed_bounce_restores_and_stops_verification(kick_harness, failure, failing_bounce):
    result = asyncio.run(kick_harness(failure=failure, failing_bounce=failing_bounce))
    assert len(result.restores) == 1
    assert result.mode == 'autonomous'
    assert not result.force_state['active']
    assert result.polls == (1 if failing_bounce == 1 else 3)
    assert len(result.pushes) == 1
    assert 'within 5 minutes' not in result.pushes[0]


def test_failed_restore_keeps_force_state_and_stops_old_verification(kick_harness):
    result = asyncio.run(kick_harness(failure='post', restore_fails=True))
    assert len(result.restores) == 1
    assert result.force_state['active']
    assert result.force_state['saved_operation_mode'] == 'autonomous'
    assert result.polls == 3


@pytest.mark.parametrize('supersede', ['bounce', 'notification'])
def test_superseded_retry_never_restores_new_owner(kick_harness, supersede):
    result = asyncio.run(kick_harness(failure='post', supersede=supersede))
    assert not result.restores
    if supersede == 'bounce':
        assert result.writes[-1] == (2, 'self_consumption')
        assert not result.pushes


@pytest.mark.parametrize('charging,full', [(True, False), (False, True), (False, False)])
def test_successful_kick_controls(kick_harness, charging, full):
    result = asyncio.run(kick_harness(charging=charging, full=full))
    assert not result.restores
    assert result.mode == 'autonomous'
    if charging:
        assert not result.writes and not result.pushes
    elif full:
        assert len(result.writes) == 2 and not result.pushes
    else:
        assert len(result.writes) == 4
        assert len(result.pushes) == 1 and 'within 5 minutes' in result.pushes[0]
