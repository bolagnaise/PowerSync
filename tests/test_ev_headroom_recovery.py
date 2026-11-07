"""Recover the ticket-152 retained Start without relying on new stream data."""
import asyncio
import ast
from copy import deepcopy
from dataclasses import replace
import importlib
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio

from test_native_rate_admission import admission, NOW, VIN


@pytest_asyncio.fixture
async def recovery_case(admission, monkeypatch):
    a = admission
    r = importlib.import_module(a.m.__package__ + '.ev_headroom_recovery')
    t = importlib.import_module(a.m.__package__ + '.native_rate_telemetry')
    now = [NOW]
    monkeypatch.setattr(r.time, 'time', lambda: now[0])
    monkeypatch.setattr(r.time, 'monotonic', lambda: now[0])
    a.budget.clock = lambda: now[0]
    a.budget.armed_sites.clear()
    registry = sys.modules['homeassistant.helpers.entity_registry'].async_get(a.hass)
    for registration in registry.entities.values():
        registration.platform = 'teslemetry'
    a.provider.runtime_data.vehicles[0].device.identifiers = {('teslemetry', VIN)}
    a.hass.config_entries.async_entries = lambda domain: [a.provider] if domain == 'teslemetry' else []
    switch = registry.entities['switch.car_charge']
    record = dict(entry_id='one', site='site', identity=a.m._identity(switch),
        entity_id=switch.entity_id, setting_entity_id=a.state.entity_id,
        target_amps=80, target_w=18400, reserved_w=18400,
        submitted_at=NOW-3600, operation='start', uncertain=True,
        identities=[a.m._identity(switch), a.m._identity(registry.entities[a.state.entity_id])])
    a.store.data = {'entries': {}, 'managed': {VIN: record}}
    a.budget = a.b.SharedRateBudget(a.store, clock=lambda: now[0])
    await a.budget.load()
    a.hass.data[a.b.RUNTIME_KEY] = a.budget
    a.charge.update(charger_actual_current=0, charger_power=0, charging_state='Disconnected',
                    charge_current_request=16)
    observation = t.native_charge_setting(a.hass, VIN, switch.entity_id)['observation']
    # Replay an old stream document and its stale, disconnected HA projection.
    a.charge['timestamp'] = NOW-123
    reads, meters, hooks = [], [], {'read': None, 'meter': None, 'settle': None}

    async def budget_for(_): return a.budget
    async def read(hass, vin, **kwargs):
        assert vin == VIN and kwargs['preferred_entity_id'] == switch.entity_id
        assert kwargs['allow_paid_refresh'] is True and kwargs['authorize']()
        assert kwargs['timeout'] == r.READ_TIMEOUT
        reads.append(kwargs)
        if hooks['read']:
            return await hooks['read'](len(reads), kwargs)
        return replace(observation, source_charge_timestamp=now[0], origin='native_cloud',
                       raw_charge_state=dict(observation.raw_charge_state, timestamp=now[0]))
    async def meter():
        meters.append(now[0])
        a.local.last_success_ts = now[0]
        if hooks['meter']: await hooks['meter']()
    async def settle(seconds):
        now[0] += seconds
        if hooks['settle']: hooks['settle']()
    monkeypatch.setattr(r, 'budget_for', budget_for)
    monkeypatch.setattr(r, 'async_read_independent_tesla_vehicle', read)
    monkeypatch.setattr(r.asyncio, 'sleep', settle)
    a.local.async_request_refresh = meter
    async def forbidden(*args, **kwargs): pytest.fail('Recovery must not submit hardware commands')
    a.hass.services = NS(async_call=forbidden)
    return NS(a=a, r=r, now=now, reads=reads, meters=meters, hooks=hooks,
              observation=observation, registry=registry)


async def recover(c, **kwargs):
    return await c.r.async_recover_ev_headroom(c.a.hass, c.a.entry, VIN,
                                             allow_provider_refresh=True, **kwargs)


@pytest.mark.asyncio
async def test_explicit_recovery_clears_restored_start_durably_without_stream_update(recovery_case):
    c = recovery_case
    assert c.a.budget.managed[VIN]['reserved_w'] == 18400
    other = deepcopy(c.a.budget.managed[VIN])
    other.update(entry_id='other', site='another-site', identity='other:provider:device:entity')
    c.a.budget.managed['OTHER'] = deepcopy(other)
    assert await recover(c) == dict(success=True, vin=VIN, reservation_cleared=True)
    assert len(c.reads) == len(c.meters) == 2
    assert c.reads[1]['updated_after'] == NOW
    assert c.meters[1] - c.meters[0] >= 3
    assert c.a.charge['timestamp'] == NOW-123
    assert c.a.store.data['managed'] == {'OTHER': other}
    assert c.a.budget.unobserved_w('site') == 0
    restarted = c.a.b.SharedRateBudget(c.a.store)
    await restarted.load()
    assert restarted.managed == {'OTHER': other}
    assert not c.reads[0]['authorize']()  # No request retains paid-read authority.


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['owner', 'task', 'recovered_owner', 'hold', 'blocked',
    'armed', 'native_max', 'unloading', 'wrong_entry', 'wrong_site', 'identity',
    'device', 'wrong_vin', 'provider_disabled', 'no_meter', 'busy', 'no_consent'])
async def test_unsafe_scope_never_spends_a_read_or_releases(recovery_case, monkeypatch, reason):
    c, a = recovery_case, recovery_case.a
    if reason == 'owner': monkeypatch.setattr(c.r, '_managed_reconcile_allowed', lambda *_: False)
    if reason == 'task':
        task = asyncio.create_task(asyncio.Event().wait())
        a.budget.managed_tasks[VIN] = task
    if reason == 'recovered_owner': a.runtime['ev_recovered_ownership'] = {VIN: {'owner': 'scheduler'}}
    if reason == 'hold': a.runtime['_tesla_ev_recovery_sidecar_hold'] = 'held'
    if reason == 'blocked':
        ev = importlib.import_module(a.m.__package__ + '.tesla_ev_recovery')
        monkeypatch.setattr(ev, 'get_vehicle_route', lambda *_: {'kind': 'blocked'})
    if reason == 'armed': a.budget.armed_sites.add('site')
    if reason == 'native_max': monkeypatch.setattr(a.budget, 'native_max_claim', lambda *_: {'owner': 'battery'})
    if reason == 'unloading': a.runtime['powerwall_local_dispatch_stopping'] = True
    if reason == 'wrong_entry': a.budget.managed[VIN]['entry_id'] = 'other'
    if reason == 'wrong_site': a.budget.managed[VIN]['site'] = 'other'
    if reason == 'identity': c.registry.entities['switch.car_charge'].unique_id = 'replacement'
    if reason == 'device': c.registry.entities['switch.car_charge'].device_id = 'replacement'
    if reason == 'wrong_vin': a.provider.runtime_data.vehicles[0].vin = '5YJ3E1EA7KF000002'
    if reason == 'provider_disabled': a.entry.options['ev_provider'] = 'tesla_ble'
    if reason == 'no_meter': a.local.async_request_refresh = None
    if reason == 'busy': await a.budget.admission_lock('site').acquire()
    try:
        if reason == 'no_consent':
            answer = await c.r.async_recover_ev_headroom(a.hass, a.entry, VIN)
        else:
            answer = await recover(c)
        assert not answer['success']
        assert c.reads == c.meters == []
        assert VIN in a.budget.managed
    finally:
        if reason == 'task':
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if reason == 'busy': a.budget.admission_lock('site').release()


@pytest.mark.asyncio
@pytest.mark.parametrize('variant', ['missing', 'stale', 'future', 'cached_second', 'charging',
    'nonzero_current', 'nonzero_power', 'meter_skew', 'meter_failed', 'provider_error',
    'runtime_replaced', 'record_replaced', 'identity_rebound', 'new_owner', 'meter_replaced',
    'config_changed', 'blocked_during_read', 'record_mutated', 'save_failed', 'revoked_during_save'])
async def test_bad_readback_or_interrupted_recovery_retains_reservation(recovery_case, monkeypatch, variant):
    c, a = recovery_case, recovery_case.a
    async def read(number, kwargs):
        if variant == 'missing': return None
        if variant == 'provider_error': raise RuntimeError('unavailable')
        stamp = c.now[0]
        if variant == 'stale': stamp -= 20
        if variant == 'future': stamp += 20
        if variant == 'cached_second': stamp = NOW
        if variant == 'runtime_replaced': a.hass.data['power_sync']['one'] = dict(a.runtime)
        if variant == 'record_replaced': a.budget.managed[VIN] = deepcopy(a.budget.managed[VIN])
        if variant == 'identity_rebound': c.registry.entities['switch.car_charge'].unique_id = 'replacement'
        if variant == 'new_owner': a.runtime['ev_recovered_ownership'] = {VIN: {'owner': 'scheduler'}}
        if variant == 'meter_replaced': a.runtime['powerwall_local'] = {'coordinator': NS()}
        if variant == 'config_changed': a.entry.options['ev_provider'] = 'tesla_ble'
        if variant == 'blocked_during_read':
            ev = importlib.import_module(a.m.__package__ + '.tesla_ev_recovery')
            monkeypatch.setattr(ev, 'get_vehicle_route', lambda *_: {'kind': 'blocked'})
        if variant == 'record_mutated': a.budget.managed[VIN]['reserved_w'] += 100
        charge = dict(c.observation.raw_charge_state, timestamp=stamp)
        state = 'Charging' if variant == 'charging' else 'Disconnected'
        amps = 1 if variant == 'nonzero_current' else 0
        watts = 1 if variant == 'nonzero_power' else 0
        charge.update(charging_state=state, charger_actual_current=amps, charger_power=watts)
        return replace(c.observation, source_charge_timestamp=stamp, raw_charge_state=charge,
            charge_state=state, charger_actual_current=amps, charger_power=watts, origin='native_cloud')
    c.hooks['read'] = read
    async def meter():
        if variant == 'meter_skew': a.local.last_success_ts = c.now[0]-6
        if variant == 'meter_failed': raise RuntimeError('meter unavailable')
    c.hooks['meter'] = meter
    if variant == 'save_failed':
        def fail(): raise OSError('disk error')
        a.store.hook = fail
    if variant == 'revoked_during_save':
        a.store.hook = lambda: a.runtime.update(powerwall_local_dispatch_stopping=True)
    answer = await recover(c)
    assert not answer['success']
    assert VIN in a.budget.managed and VIN in a.store.data['managed']
    assert len(c.reads) <= 2
    assert not c.reads[0]['authorize']()


@pytest.mark.asyncio
async def test_failure_cooldown_prevents_repeated_paid_reads(recovery_case):
    c = recovery_case
    async def missing(*_): return None
    c.hooks['read'] = missing
    assert not (await recover(c))['success']
    assert (await recover(c))['error'] == 'recovery_cooldown'
    assert len(c.reads) == 1
    c.now[0] += c.r.COOLDOWN_SECONDS
    assert not (await recover(c))['success']
    assert len(c.reads) == 2


@pytest.mark.asyncio
async def test_cancellation_revokes_read_authority_and_preserves_record(recovery_case):
    c = recovery_case
    entered = asyncio.Event()
    async def wait(*_):
        entered.set()
        await asyncio.Event().wait()
    c.hooks['read'] = wait
    task = asyncio.create_task(recover(c))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert not c.reads[0]['authorize']()
    assert VIN in c.a.budget.managed and VIN in c.a.store.data['managed']
    assert not c.a.budget.admission_lock('site').locked()


@pytest.mark.asyncio
async def test_cancellation_during_shielded_save_restores_durable_reservation(recovery_case):
    c = recovery_case
    saving, release = asyncio.Event(), asyncio.Event()
    original_save = c.a.store.async_save
    async def save(data):
        if VIN not in data['managed']:
            saving.set()
            await release.wait()
        await original_save(data)
    c.a.store.async_save = save
    task = asyncio.create_task(recover(c))
    await saving.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert not c.reads[-1]['authorize']()
    release.set()
    await c.a.budget.drain_storage()
    assert VIN in c.a.budget.managed and VIN in c.a.store.data['managed']
    assert c.a.budget.managed[VIN]['reserved_w'] == 18400


@pytest.mark.asyncio
async def test_recovery_reopens_existing_cloud_tariff_admission_without_command(recovery_case):
    from test_powerwall_battery_dispatch import load_routing, call
    c = recovery_case
    ns, local, client, _ = load_routing(tesla_control_method='tariff')
    ns['site_identity'] = lambda _: 'site'
    ns['hass'].data['power_sync_native_rate_budget'] = c.a.budget
    with pytest.raises(RuntimeError, match='headroom'):
        await ns['_try_local_powerwall_force']('charge', call(power_w=0), 15, 0)
    assert (await recover(c))['success']
    assert await ns['_try_local_powerwall_force']('charge', call(power_w=0), 15, 0) is None
    client.dispatch_battery_power.assert_not_awaited()
    client.get_snapshot.assert_not_awaited()
    assert not local.pending


@pytest.mark.asyncio
async def test_registered_routed_action_targets_exact_entry_and_returns_result(recovery_case, monkeypatch):
    c = recovery_case
    # Extract the real HA wrapper and registration, preserving its entry router.
    tree = ast.parse((Path(__file__).parents[1] / 'custom_components/power_sync/__init__.py').read_text())
    wrapper = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)
                   and node.name == 'handle_recover_ev_headroom')
    registration = next(node for node in ast.walk(tree) if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == 'async_register'
        and len(node.value.args) > 1 and isinstance(node.value.args[1], ast.Constant)
        and node.value.args[1].value == 'recover_ev_headroom')
    routing = importlib.import_module(c.a.m.__package__ + '.licensing.entry_routing')
    handlers = {}
    c.a.hass.services.async_register = lambda domain, name, fn, **kw: handlers.update({name: fn})
    ns = dict(__package__=c.a.m.__package__, hass=c.a.hass, entry=c.a.entry,
        ServiceCall=NS, Any=object, DOMAIN='power_sync', asyncio=asyncio,
        routed_handler=routing.routed_handler, SupportsResponse=NS(OPTIONAL='optional'))
    exec(compile(ast.Module(body=[wrapper, registration], type_ignores=[]), '<recovery-service>', 'exec'), ns)
    routing.register_handlers(c.a.hass, 'one', {'recover_ev_headroom': ns['handle_recover_ev_headroom']})
    answer = await handlers['recover_ev_headroom'](NS(data=dict(entry_id='one', vin=VIN,
                                                             allow_provider_refresh=True)))
    assert answer['success'] and answer['reservation_cleared']
    with pytest.raises(ValueError, match='control_entry_not_available'):
        await handlers['recover_ev_headroom'](NS(data=dict(entry_id='missing', vin=VIN,
                                                          allow_provider_refresh=True)))
