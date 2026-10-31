"""Older Tesla controls must yield to a newer settings owner before more I/O."""
from __future__ import annotations

import ast
import asyncio
import copy
import __future__
import logging
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

SOURCE = Path(__file__).parents[1] / 'custom_components/power_sync/__init__.py'


@lru_cache(maxsize=1)
def source_tree():
    return ast.parse(SOURCE.read_text())


def load(name, env):
    node = copy.deepcopy(next(n for n in ast.walk(source_tree())
                if isinstance(n, ast.AsyncFunctionDef) and n.name == name))
    node.decorator_list = []
    node.body = [n for n in node.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                 str(SOURCE), 'exec', flags=__future__.annotations.compiler_flag), env)
    return env[name]


@pytest.mark.parametrize('caller,target,owner', [
    ('force charge', 100, 'command'),
    ('set backup reserve', 20, 'command'),
    ('set backup reserve', 20, 'reserve'),
    ('set backup reserve', 20, 'kick'),
])
@pytest.mark.parametrize('moment', ['lock', 'readback', 'retry', 'current'])
def test_ordinary_reserve_stops_before_replaying_a_superseded_target(caller, target, moment, owner):
    async def scenario():
        writes = []
        generation, reserve_generation, kick = [1], [1], [1]
        lock = asyncio.Lock()

        def supersede():
            {'command': generation, 'reserve': reserve_generation, 'kick': kick}[owner][0] += 1

        class Response:
            status = 503 if moment == 'retry' else 200
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def text(self):
                return 'synthetic'

        class Session:
            def post(self, url, *, json, **kwargs):
                writes.append(json['backup_reserve_percent'])
                return Response()

        async def confirm(*args, **kwargs):
            if moment == 'readback':
                supersede()
                return False
            return True

        async def sleep(_):
            if moment == 'retry':
                supersede()

        env = dict(asyncio=NS(current_task=asyncio.current_task, sleep=sleep,
                              TimeoutError=TimeoutError),
                   aiohttp=NS(ClientTimeout=lambda **kw: None, ClientError=RuntimeError),
                   hass=NS(data={'power_sync': {'entry': {}}}),
                   entry=NS(entry_id='entry', data={}), DOMAIN='power_sync',
                   CONF_FLEET_API_BASE_URL='base',
                   async_get_clientsession=lambda _: Session(),
                   get_tesla_api_base_url=lambda *a: 'https://example.invalid',
                   _tesla_force_confirm_backup_reserve=confirm,
                   _tesla_reserve_write_lock=lock, _tesla_reserve_write_tasks=set(),
                   _tesla_reserve_pulse_runtime={}, _command_generation=generation,
                   _tesla_reserve_generation=reserve_generation,
                   _tesla_charge_kick_generation=kick, _restore_gen=1, _reserve_gen=1,
                   _force_charge_kick_gen=1,
                   backup_reserve_command_generation=1, backup_reserve_kick_generation=1,
                   backup_reserve_generation=1,
                   _local_powerwall_generation_current=lambda: True,
                   _local_powerwall_runtime={}, _monitoring_mode_should_block_control=lambda _: False,
                   call=NS(data={}), site_configs=[('site', 'fake', 'fleet')], percent=target,
                   _LOGGER=logging.getLogger(__name__))
        for name in ('_tesla_force_set_backup_reserve_cloud',
                     '_tesla_force_apply_backup_reserve_unlocked',
                     '_tesla_force_apply_backup_reserve'):
            load(name, env)
        # Execute the production caller's actual reserve assignment so an
        # unthreaded ownership predicate cannot be hidden by a helper-only test.
        assignment = copy.deepcopy(next(n for n in ast.walk(source_tree())
                          if isinstance(n, ast.Assign) and isinstance(n.value, ast.Await)
                          and isinstance(n.value.value, ast.Call)
                          and isinstance(n.value.value.func, ast.Name)
                          and n.value.value.func.id == '_tesla_force_apply_backup_reserve'
                          and any(k.arg == 'reason' and isinstance(k.value, ast.Constant)
                                  and k.value.value == caller for k in n.value.value.keywords)))
        function = ast.AsyncFunctionDef(name='run',
            args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
            body=[assignment, ast.Return(value=ast.Name(id='reserve_result', ctx=ast.Load()))],
            decorator_list=[])
        exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
                     '<reserve caller>', 'exec'), env)
        if moment == 'lock':
            await lock.acquire()
        task = asyncio.create_task(env['run']())
        if moment == 'lock':
            await asyncio.sleep(0)
            supersede()
            lock.release()
        result = await task
        assert writes == ([] if moment == 'lock' else [target])
        assert result['confirmed_sites'] == (['site'] if moment == 'current' else [])
        assert not env['_tesla_reserve_write_tasks']

    asyncio.run(scenario())


def restore_admission(env):
    """Execute the real restore entry through its first asynchronous boundary."""
    node = copy.deepcopy(next(n for n in ast.walk(source_tree())
                if isinstance(n, ast.AsyncFunctionDef) and n.name == 'handle_restore_normal'))
    stop = next(i for i, n in enumerate(node.body) if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == 'solaredge_restore_coordinator' for t in n.targets))
    node.body = [n for n in node.body[:stop] if not isinstance(n, ast.ImportFrom)]
    node.body.append(ast.Expr(value=ast.Call(func=ast.Name(id='admitted', ctx=ast.Load()),
                                           args=[], keywords=[])))
    node.decorator_list = []
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                 '<restore admission>', 'exec', flags=__future__.annotations.compiler_flag), env)
    return env['handle_restore_normal']


@pytest.mark.parametrize('moment', ['queued', 'stopping', 'current'])
@pytest.mark.parametrize('owner', ['command', 'kick', 'operation'])
def test_restore_rejects_stale_cleanup_before_claiming_newer_controls(moment, owner):
    generations = {key: [1] for key in ('command', 'kick', 'operation')}
    generation = generations[owner]
    admitted = []

    async def stop():
        if moment == 'stopping':
            generation[0] += 1
        return False

    env = dict(_command_generation=generations['command'],
               _tesla_charge_kick_generation=generations['kick'],
               _tesla_operation_generation=generations['operation'],
               _control_call_source=lambda _: 'force_cleanup',
               _stop_local_powerwall_dispatch=stop, NativeMaxRestorePending=RuntimeError,
               _LOGGER=logging.getLogger(__name__), admitted=lambda: admitted.append(True))
    restore = restore_admission(env)
    if moment == 'queued':
        generation[0] += 1
    asyncio.run(restore(NS(context=NS(user_id=None, parent_id=None),
                           data={'_tesla_restore_generation': 1,
                                 '_tesla_restore_kick_generation': 1,
                                 '_tesla_restore_operation_generation': 1})))
    assert admitted == ([True] if moment == 'current' else [])
