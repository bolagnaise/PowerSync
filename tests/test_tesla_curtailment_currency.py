"""Execute production curtailment predicates at the actual queued HTTP boundary."""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from test_tesla_control_path_matrix import controls

SOURCE = Path(__file__).parents[1] / 'custom_components/power_sync/__init__.py'


def final_guard(handler_name, *, restore, namespace):
    setup = next(n for n in ast.parse(SOURCE.read_text()).body
                 if isinstance(n, ast.AsyncFunctionDef) and n.name == 'async_setup_entry')
    handler = next(n for n in setup.body if isinstance(n, ast.AsyncFunctionDef) and n.name == handler_name)
    entry_data = next(n for n in handler.body if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == 'entry_data' for t in n.targets))
    start = next(i for i, n in enumerate(handler.body) if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Subscript) and 'tesla_curtailment_epoch' in ast.unparse(t) for t in n.targets))
    stop = next(i for i, n in enumerate(handler.body) if isinstance(n, ast.FunctionDef)
                and n.name == 'curtailment_restore_current')
    calls = [n for n in ast.walk(handler) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == 'post']
    call = next(n for n in calls if ('restore_rule' in ast.unparse(n)) == restore)
    guard = next(k.value for k in call.keywords if k.arg == '_power_sync_is_current')
    tree = ast.Module(body=[entry_data] + handler.body[start:stop + 1] + [ast.Assign(
        targets=[ast.Name(id='guard', ctx=ast.Store())], value=guard)], type_ignores=[])
    exec(compile(ast.fix_missing_locations(tree), '<actual curtailment final guard>', 'exec'), namespace)
    return namespace['guard']


@pytest.mark.asyncio
@pytest.mark.parametrize('handler', ['handle_solar_curtailment_check', 'handle_solar_curtailment_with_websocket_data'])
@pytest.mark.parametrize('restore', [False, True])
@pytest.mark.parametrize('change', ['current', 'runtime', 'epoch', 'command', 'site', 'disabled', 'monitoring', 'ac_only', 'ev', 'manual'])
async def test_curtailment_queued_write_rechecks_actual_admission(controls, handler, restore, change):
    runtime = {'manual_export_override': False}
    entry = NS(entry_id='entry', data={'tesla_energy_site_id': 'site'})
    hass = NS(data={'power_sync': {'entry': runtime}})
    flags = {'enabled': True, 'monitoring_allowed': True, 'ac_only': False}
    generation = [0]
    ns = dict(hass=hass, entry=entry, DOMAIN='power_sync', CONF_TESLA_ENERGY_SITE_ID='tesla_energy_site_id',
              _command_generation=generation,
              _effective_solar_curtailment_enabled=lambda: flags['enabled'],
              _monitoring_mode_allows_curtailment=lambda enabled: flags['monitoring_allowed'],
              _ac_only_solar_curtailment_enabled=lambda: flags['ac_only'], restore_rule='battery_ok')
    guard = final_guard(handler, restore=restore, namespace=ns)
    held, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
    site = controls.site.coordinator_for(hass, site_id='site')
    sent = []
    class Reply:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
    raw = NS(post=lambda *args, **kwargs: (sent.append(True) or Reply()))
    session = controls.http.controlled_session(hass, raw)
    async def hold():
        async with site.write_context('preceding mutation'):
            held.set()
            await release.wait()
    async def send():
        queued.set()
        async with session.post('https://tesla.test/api/1/energy_sites/site/grid_import_export',
                                _power_sync_is_current=guard):
            pass
    holder = asyncio.create_task(hold())
    await held.wait()
    writer = asyncio.create_task(send())
    await queued.wait()
    await asyncio.sleep(0)
    if change == 'runtime': hass.data['power_sync']['entry'] = {}
    elif change == 'epoch': runtime['tesla_curtailment_epoch'] += 1
    elif change == 'command': generation[0] += 1
    elif change == 'site': entry.data['tesla_energy_site_id'] = 'replacement'
    elif change == 'disabled': flags['enabled'] = False
    elif change == 'monitoring': flags['monitoring_allowed'] = False
    elif change == 'ac_only': flags['ac_only'] = True
    elif change == 'ev': runtime['ev_curtailment_override'] = True
    elif change == 'manual': runtime.update(manual_export_override=True, manual_export_rule='never')
    release.set()
    await holder
    revoked = change != 'current' and (change != 'manual' or restore)
    if revoked:
        with pytest.raises(controls.site.ControlRequestStale): await writer
        assert not sent
    else:
        await writer
        assert sent == [True]
