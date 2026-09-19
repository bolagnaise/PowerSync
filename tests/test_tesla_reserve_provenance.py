"""Reserve targets must not be converted using unrelated local/cloud samples."""
from __future__ import annotations

import ast
import asyncio
import logging
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1] / 'custom_components/power_sync'


def extract(path, name, env):
    node = next(n for n in ast.walk(ast.parse((ROOT / path).read_text()))
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    node.body = [n for n in node.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
    tree = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(tree), str(ROOT / path), 'exec'), env)
    return env[name]


@pytest.mark.parametrize('offset,cloud,age,pending', [
    (5, 20, 1800, None),  # Reported variant: stale poll corrupts known offset.
    (None, 20, 1800, None),  # Independent first-write bootstrap defect.
    (15, 50, 0, 40),  # Already poisoned offset, matching pending raw55/user40.
    (5, 30, 0, None),  # Recent samples still have no common setting revision.
    (10, 25, 0, None),  # Never assume the default offset is correct.
    (None, None, None, None),
])
@pytest.mark.parametrize('target', [0, 40, 80, 100])
def test_reserve_write_uses_user_scale_and_independent_confirmation(offset, cloud, age, pending, target):
    writes, cloud_calls = [], []
    coord = NS(_site_info_cache={'backup_reserve_percent': cloud},
               _site_info_last_fetch=0 if age is None else time.monotonic() - age)
    def invalidate():
        coord._site_info_cache = None
        coord._site_info_last_fetch = 0
    coord.invalidate_site_info_cache = invalidate
    data = {'tesla_coordinator': coord, 'powerwall_local_low_soe_reserve_pct': offset}
    if pending is not None:
        data.update(powerwall_local_backup_reserve_write_local_pct=55,
                    powerwall_local_backup_reserve_write_user_pct=pending)
    class Transport:
        async def read_config(self, din):
            return {'site_info': {'backup_reserve_percent': writes[-1] if writes else 35}}
        async def write_config(self, din, patch):
            writes.append(patch['site_info.backup_reserve_percent'])
            return True
    async def dispatch(hass, entry, *, local_call, cloud_call, **kwargs):
        return await local_call(Transport()) or await cloud_call()
    async def cloud_write(session, site, token, provider, percent, **kwargs):
        cloud_calls.append((site, percent))
        return site != 'unconfirmed'
    env = dict(hass=NS(data={'power_sync': {'entry': data}}),
               entry=NS(entry_id='entry', data={'din': 'test-din'}),
               DOMAIN='power_sync', CONF_POWERWALL_LOCAL_DIN='din', CONF_FLEET_API_BASE_URL='base',
               async_get_clientsession=lambda _: None, get_tesla_api_base_url=lambda *a: 'https://example.invalid',
               dispatch_powerwall_write=dispatch, _tesla_force_set_backup_reserve_cloud=cloud_write,
               _LOGGER=logging.getLogger(__name__), asyncio=asyncio)
    exec(compile((ROOT / 'powerwall_local/normalization.py').read_text(), '<normalization>', 'exec'), env)
    apply = extract('__init__.py', '_tesla_force_apply_backup_reserve_unlocked', env)
    result = asyncio.run(apply([('site-a', 'fake', 'fake'), ('unconfirmed', 'fake', 'fake')], target, reason='test'))
    assert writes == [], 'An unknown conversion must never reach the local actuator'
    assert cloud_calls == [('site-a', target), ('unconfirmed', target)]
    assert result == {'confirmed_sites': ['site-a'], 'accepted_sites': [], 'failed_sites': ['unconfirmed']}
    assert not any(k.startswith('powerwall_local_backup_reserve_write') or k == 'powerwall_local_low_soe_reserve_pct' for k in data)
    assert coord._site_info_cache is None


@pytest.mark.parametrize('status,observed,confirmed', [
    (200, 40, True), (200, 50, False), (200, None, False),
    (200, True, False), (403, 40, False), (503, 40, False),
])
def test_cloud_acceptance_requires_independent_user_scale_readback(status, observed, confirmed):
    writes, reads = [], []
    class Response:
        def __init__(self, status, data):
            self.status, self.data = status, data
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def json(self):
            return self.data
        async def text(self):
            return 'synthetic response'
    class Session:
        def post(self, url, *, json, **kwargs):
            writes.append((url, json))
            return Response(status, {})
        def get(self, url, **kwargs):
            reads.append(url)
            return Response(200, {'response': {'backup_reserve_percent': observed}})
    async def sleep(_):
        pass
    env = dict(entry=NS(data={}), CONF_FLEET_API_BASE_URL='base',
               get_tesla_api_base_url=lambda *args: 'https://example.invalid',
               aiohttp=NS(ClientTimeout=lambda **kw: None, ClientError=RuntimeError),
               asyncio=NS(sleep=sleep, TimeoutError=TimeoutError),
               _LOGGER=logging.getLogger(__name__))
    for name in ['_tesla_force_read_backup_reserve', '_tesla_force_confirm_backup_reserve',
                 '_tesla_force_set_backup_reserve_cloud']:
        extract('__init__.py', name, env)
    result = asyncio.run(env['_tesla_force_set_backup_reserve_cloud'](
        Session(), 'site-a', 'fake-token', 'fake-provider', 40, reason='test'))
    assert result is confirmed
    assert all(url.endswith('/energy_sites/site-a/backup') and payload == {'backup_reserve_percent': 40}
               for url, payload in writes)
    assert all(url.endswith('/energy_sites/site-a/site_info') for url in reads)
    assert len(writes) == (1 if confirmed or status == 403 else 3)
    assert len(reads) == (1 if confirmed else 12 if status == 200 else 0)
