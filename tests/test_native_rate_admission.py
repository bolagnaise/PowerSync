"""The final managed Tesla boundary commits prospective draw before handoff."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1] / 'custom_components/power_sync'
NOW = 1800000000.0
VIN = '5YJ3E1EA7KF000001'


class Store:
    def __init__(self): self.data = None; self.hook = None
    async def async_load(self): return deepcopy(self.data)
    async def async_save(self, data):
        if self.hook: self.hook()
        self.data = deepcopy(data)


@pytest.fixture
def admission(monkeypatch):
    package = 'managed_admission_probe'
    for name, path in [(package, ROOT), (package+'.optimization', ROOT/'optimization'),
                       (package+'.automations', ROOT/'automations'),
                       (package+'.powerwall_local', ROOT/'powerwall_local')]:
        module = ModuleType(name); module.__path__ = [str(path)]
        monkeypatch.setitem(sys.modules, name, module)
    m = importlib.import_module(package+'.native_rate_admission')
    b = importlib.import_module(package+'.native_rate_budget')
    monkeypatch.setattr(m.time, 'time', lambda: NOW)
    stamp = datetime.fromtimestamp(NOW, timezone.utc)
    state = SimpleNamespace(entity_id='number.car_charge_current', state='10',
                            attributes={'min': 0, 'max': 32}, last_reported=stamp)
    def registration(entity, unique):
        return SimpleNamespace(entity_id=entity, unique_id=unique, device_id='car',
                               platform='tesla_fleet', config_entry_id='provider', disabled_by=None)
    number = registration(state.entity_id, 'amps')
    switch = registration('switch.car_charge', 'charging')
    registry = SimpleNamespace(entities={number.entity_id: number, switch.entity_id: switch})
    registry.async_get = registry.entities.get
    er = ModuleType('homeassistant.helpers.entity_registry'); er.async_get = lambda _: registry
    ha = ModuleType('homeassistant'); helpers = ModuleType('homeassistant.helpers')
    ha.__path__ = []; helpers.__path__ = []; ha.helpers = helpers; helpers.entity_registry = er
    for name, module in [('homeassistant',ha), ('homeassistant.helpers',helpers),
                         ('homeassistant.helpers.entity_registry',er)]:
        monkeypatch.setitem(sys.modules, name, module)
    charge = {'timestamp': NOW, 'charger_voltage': 230, 'charger_phases': 1,
              'charger_actual_current': 10, 'charging_state': 'Charging'}
    provider = SimpleNamespace(runtime_data=SimpleNamespace(vehicles=[SimpleNamespace(vin=VIN,
        coordinator=SimpleNamespace(last_update_success=True, data={'charge_state': charge}))]))
    snapshot = SimpleNamespace(grid_w=6000, load_w=5000, solar_w=0, battery_w=-1000,
        raw={'config': {'site_info': {'max_site_meter_power_ac': 15, 'min_site_meter_power_ac': -10}}})
    entry = SimpleNamespace(entry_id='one', data={'tesla_energy_site_id':'site'}, options={})
    runtime = {'powerwall_local': {'coordinator': SimpleNamespace(data=snapshot,
        last_success_ts=NOW, last_update_success=True)}}
    store = Store(); budget = b.SharedRateBudget(store, clock=lambda: NOW)
    asyncio.run(budget.load())
    budget.armed_sites.add('site')
    hass = SimpleNamespace(data={b.RUNTIME_KEY: budget, 'power_sync': {'one': runtime}},
        states=SimpleNamespace(get=lambda entity: state if entity == state.entity_id else None),
        config_entries=SimpleNamespace(async_entries=lambda domain: [provider] if domain=='tesla_fleet' else []))
    context = {'entry':entry,'runtime':runtime,'vin':VIN,'params':{},'hass':hass}
    token = m._EV_SUBMISSION_CONTEXT.set(context)
    yield SimpleNamespace(m=m, b=b, hass=hass, entry=entry, runtime=runtime, state=state,
                          snapshot=snapshot, charge=charge, budget=budget, store=store)
    m._EV_SUBMISSION_CONTEXT.reset(token)


@pytest.mark.asyncio
async def test_current_and_start_reuse_one_vin_demand_without_double_counting(admission):
    a = admission
    assert await a.m.prepare_ev_submission(a.hass, a.state.entity_id, operation='rate', amps=20)
    assert a.store.data['managed'][VIN]['reserved_w'] == 4600
    assert await a.m.prepare_ev_submission(a.hass, 'switch.car_charge', operation='start')
    assert len(a.budget.managed) == 1
    assert a.budget.managed[VIN]['reserved_w'] == 4600


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', ['esphome', 'tesla_fleet'])
@pytest.mark.parametrize('operation', ['start', 'rate'])
@pytest.mark.parametrize('restart', [False, True])
async def test_legacy_start_without_current_does_not_poison_later_commands(
    admission, platform, operation, restart,
):
    a = admission
    registry = sys.modules['homeassistant.helpers.entity_registry'].async_get(a.hass)
    for registration in registry.entities.values():
        registration.platform = platform

    # HA normalizes entity IDs with lower(); unlike dict.get, it rejects None.
    def get_state(entity_id):
        entity_id = entity_id.lower()
        return a.state if entity_id == a.state.entity_id else None

    a.hass.states.get = get_state
    a.budget.armed_sites.clear()
    a.state.state = 'unavailable'
    assert await a.m.prepare_ev_submission(a.hass, 'switch.car_charge', operation='start')
    assert a.budget.managed[VIN]['setting_entity_id'] is None
    reserved = a.budget.managed[VIN]['reserved_w']

    if restart:
        a.budget = a.b.SharedRateBudget(a.store, clock=lambda: NOW)
        await a.budget.load()
        a.hass.data[a.b.RUNTIME_KEY] = a.budget

    # Missing readback must retain the uncertain demand, not abort all commands
    # or release capacity on the strength of a missing current setting.
    await a.m.refresh_managed_records(a.hass, a.budget)
    assert a.budget.managed[VIN]['reserved_w'] == reserved
    a.state.state = '10'
    assert await a.m.prepare_ev_submission(
        a.hass,
        'switch.car_charge' if operation == 'start' else a.state.entity_id,
        operation=operation,
        amps=10 if operation == 'rate' else None,
    )
    assert a.budget.managed[VIN]['reserved_w'] >= reserved


@pytest.mark.asyncio
async def test_start_cannot_energize_unknown_or_zero_programmed_current(admission):
    a = admission
    for setting in ('unknown', '0'):
        a.state.state = setting
        assert not await a.m.prepare_ev_submission(a.hass, 'switch.car_charge', operation='start')
    assert not a.budget.managed


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['full_grid', 'stale_car', 'missing_limit', 'pending_battery'])
async def test_increase_rejects_missing_headroom_and_unverified_evidence(admission, failure):
    a = admission
    if failure == 'full_grid': a.snapshot.grid_w = 14900
    if failure == 'stale_car': a.charge['timestamp'] = NOW-16
    if failure == 'missing_limit': a.snapshot.raw['config']['site_info'].pop('max_site_meter_power_ac')
    if failure == 'pending_battery':
        a.runtime['powerwall_battery_dispatch'] = SimpleNamespace(charge_generation=None, _last_watts=-10000)
    with pytest.raises(a.m.SiteImportAdmissionDenied):
        await a.m.prepare_ev_submission(a.hass, a.state.entity_id, operation='rate', amps=20)
    assert not a.budget.managed


@pytest.mark.asyncio
async def test_manual_setting_change_during_durable_commit_prevents_submission(admission):
    a = admission
    a.store.hook = lambda: setattr(a.state, 'state', '16')
    with pytest.raises(a.m.SiteImportAdmissionDenied):
        await a.m.prepare_ev_submission(a.hass, a.state.entity_id, operation='rate', amps=20)
    assert VIN not in a.budget.managed  # No provider submission was possible.


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['revision', 'token'])
async def test_native_max_reduction_rechecks_ownership_after_admission(admission, monkeypatch, mutation):
    a = admission
    a.budget.armed_sites.clear()
    assert await a.budget.claim_native_max('battery', 'site', 'original', NOW+60)
    original = a.budget.admit_managed
    async def interleave(*args, **kwargs):
        ticket = await original(*args, **kwargs)
        assert ticket
        if mutation == 'revision':
            a.budget.revision += 1
        else:
            a.budget.entries['native_max:battery']['native_max']['token'] = 'replacement'
        return ticket
    monkeypatch.setattr(a.budget, 'admit_managed', interleave)
    with pytest.raises(a.m.SiteImportAdmissionDenied, match='ownership changed'):
        await a.m.prepare_ev_submission(a.hass, a.state.entity_id, operation='rate', amps=9)
    assert VIN not in a.budget.managed
    assert a.budget.native_max_claim('site')


@pytest.mark.asyncio
async def test_missing_phase_metadata_cannot_authorize_an_increase(admission):
    a = admission; a.charge.pop('charger_phases')
    assert not await a.m.prepare_ev_submission(a.hass, a.state.entity_id, operation='rate', amps=20)
    assert not a.budget.managed


@pytest.mark.asyncio
async def test_managed_increase_reserves_then_yields_battery_before_handoff(admission, monkeypatch):
    a = admission
    clock = [NOW]
    monkeypatch.setattr(a.m.time, 'time', lambda: clock[0])
    a.budget.clock = lambda: clock[0]
    a.snapshot.grid_w = 14900
    a.snapshot.battery_w = -10000
    calls = []
    async def refresh(generation):
        calls.append(generation)
        # The future EV draw is accounted for before this battery command.
        assert a.budget.unobserved_w('site') == 2300
        assert a.store.data['managed'][VIN]['reserved_w'] == 4600
        battery._last_watts = -7700
        return {'acknowledged_at': clock[0]}
    battery = SimpleNamespace(charge_generation=7, _last_watts=-10000,
                              refresh_current_generation=refresh)
    a.runtime['powerwall_battery_dispatch'] = battery
    reports = []
    async def next_report(_):
        clock[0] += 0.5
        a.snapshot.grid_w = 12600
        a.snapshot.battery_w = -7700
        a.runtime['powerwall_local']['coordinator'].last_success_ts = clock[0]
        a.charge['timestamp'] = clock[0]
        reports.append(clock[0])
    monkeypatch.setattr(a.m.asyncio, 'sleep', next_report)
    assert await a.m.prepare_ev_submission(a.hass, a.state.entity_id, operation='rate', amps=20)
    assert calls == [7]
    assert len(reports) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['no_physical_headroom', 'generation_replaced', 'pairing_replaced',
    'reservation_changed', 'monitoring', 'licence', 'owner'])
async def test_managed_borrow_never_submits_without_stable_physical_capacity(admission, monkeypatch, failure):
    a = admission
    a.m._EV_SUBMISSION_CONTEXT.get()['params']['owner_mode'] = 'scheduled'
    allowed = [True]
    a.hass.data['power_sync_license_manager'] = SimpleNamespace(permits=lambda _: allowed[0])
    a.snapshot.grid_w = 14900
    a.snapshot.battery_w = -10000
    async def refresh(generation):
        if failure == 'generation_replaced': battery.charge_generation = generation+1
        if failure == 'pairing_replaced': a.entry.options['powerwall_local_din'] = 'replacement'
        if failure == 'reservation_changed': a.budget.revision += 1
        if failure == 'monitoring': a.entry.options['monitoring_mode'] = True
        if failure == 'licence': allowed[0] = False
        if failure == 'owner': a.runtime['ev_ownership'] = {VIN:{'owner':'powersync','owner_mode':'manual','session_id':'new'}}
        return {'acknowledged_at': NOW}
    battery = SimpleNamespace(charge_generation=7, _last_watts=-10000,
                              refresh_current_generation=refresh)
    a.runtime['powerwall_battery_dispatch'] = battery
    async def no_new_report(_): pass
    monkeypatch.setattr(a.m.asyncio, 'sleep', no_new_report)
    with pytest.raises(a.m.SiteImportAdmissionDenied):
        await a.m.prepare_ev_submission(a.hass, a.state.entity_id, operation='rate', amps=20)
    assert VIN not in a.budget.managed


def test_first_enable_withholds_native_authority_for_unjournaled_existing_managed_session(admission):
    a = admission
    a.runtime['entry'] = a.entry
    a.runtime['ev_ownership'] = {'OTHER': {'owner':'powersync','owner_mode':'scheduled'}}
    assert 'existing managed' in a.m.native_coexistence_reason(a.hass, 'site', a.budget)
    a.budget.managed['OTHER'] = {'entry_id':'one','site':'site'}
    assert a.m.native_coexistence_reason(a.hass, 'site', a.budget) is None
    a.runtime['ev_recovered_ownership'] = {'UNRECORDED': {'owner':'powersync'}}
    assert a.m.native_coexistence_reason(a.hass, 'site', a.budget)


def test_unsupported_charger_configuration_withholds_native_feature(admission):
    a = admission
    a.runtime['automation_store'] = SimpleNamespace(_data={'vehicle_charging_configs': [
        {'charger_type': 'zaptec', 'vehicle_id': 'other'}]})
    assert 'charger controller' in a.m.native_coexistence_reason(a.hass, 'site', a.budget)


@pytest.mark.asyncio
async def test_proven_unsent_increase_rolls_back_only_its_ticket(admission):
    a = admission
    assert await a.m.prepare_ev_submission(a.hass, a.state.entity_id, operation='rate', amps=10)
    previous = deepcopy(a.budget.managed[VIN])
    a.store.hook = lambda: setattr(a.state, 'state', '16')
    with pytest.raises(a.m.SiteImportAdmissionDenied):
        await a.m.prepare_ev_submission(a.hass, a.state.entity_id, operation='rate', amps=20)
    assert a.budget.managed[VIN] == previous
    assert a.store.data['managed'][VIN] == previous


@pytest.mark.asyncio
@pytest.mark.parametrize('revoked', ['monitoring', 'licence', 'owner'])
async def test_final_managed_authority_is_rechecked_after_journal_wait(admission, revoked):
    a = admission
    a.m._EV_SUBMISSION_CONTEXT.get()['params']['owner_mode'] = 'scheduled'
    allowed = [True]
    a.hass.data['power_sync_license_manager'] = SimpleNamespace(permits=lambda _: allowed[0])
    def revoke():
        if revoked == 'monitoring': a.entry.options['monitoring_mode'] = True
        if revoked == 'licence': allowed[0] = False
        if revoked == 'owner': a.runtime['ev_ownership'] = {VIN:{'owner':'powersync','owner_mode':'manual','session_id':'new'}}
    a.store.hook = revoke
    with pytest.raises(a.m.SiteImportAdmissionDenied):
        await a.m.prepare_ev_submission(a.hass, a.state.entity_id, operation='rate', amps=20)
    assert not a.budget.managed


@pytest.mark.asyncio
async def test_manual_ev_action_retains_its_existing_licence_exemption(admission):
    a = admission
    a.hass.data['power_sync_license_manager'] = SimpleNamespace(permits=lambda _: False)
    assert await a.m.prepare_ev_submission(a.hass, a.state.entity_id, operation='rate', amps=20)


@pytest.mark.asyncio
async def test_pending_manual_battery_command_reserves_capacity_before_ack(admission):
    a = admission
    a.runtime['powerwall_battery_dispatch'] = SimpleNamespace(
        charge_generation=None, _last_watts=0, prospective_charge_w=10000)
    with pytest.raises(a.m.SiteImportAdmissionDenied):
        await a.m.prepare_ev_submission(a.hass, a.state.entity_id, operation='rate', amps=20)
    assert not a.budget.managed


@pytest.mark.asyncio
async def test_battery_handoff_withholds_native_authority_until_legacy_state_is_restored(admission):
    a = admission
    async def force(_):
        assert a.m.native_coexistence_reason(a.hass, 'site', a.budget)
        a.runtime['force_charge_state'] = {'active':True}
    await a.m.guard_battery_transition(a.hass, a.entry, force)(None)
    assert a.runtime['native_rate_battery_transitions'] == 0
    assert a.m.native_coexistence_reason(a.hass, 'site', a.budget)
    a.runtime['force_charge_state']['active'] = False
    assert a.m.native_coexistence_reason(a.hass, 'site', a.budget) is None


def route_lock(a):
    module=importlib.import_module(a.m.__package__+'.powerwall_local.route_lock')
    lock=module.BatteryRouteLock()
    a.runtime['powerwall_battery_dispatch_route_lock']=lock
    return lock


@pytest.mark.asyncio
@pytest.mark.parametrize('direction', ['charge', 'discharge'])
@pytest.mark.parametrize('source', ['optimizer', 'user'])
async def test_failed_tariff_cleanup_finishes_inside_battery_command(admission, direction, source):
    """A failed tariff must reach restore without reacquiring its optimizer lock."""
    import ast
    import logging
    from datetime import timedelta
    from unittest.mock import AsyncMock
    from test_ha_optimizer_runtime import Harness, guard

    a = admission
    harness = Harness()
    await harness.activate()
    manager = harness.client
    a.hass.data['power_sync_license_manager'] = manager
    lock = route_lock(a)
    owner = SimpleNamespace(hass=a.hass, entry_id=a.entry.entry_id)
    state = {'active': False, 'saved_tariff': {'name': 'original'}}
    restored = []

    async def restore(call):
        assert lock.locked() and manager.control_lock(a.entry.entry_id).locked()
        assert state['active'] and state['saved_tariff'] == {'name': 'original'}
        restored.append(call.data)
        state['active'] = False

    tree = ast.parse((ROOT / '__init__.py').read_text())
    name = f'_cleanup_failed_tesla_force_{direction}'
    helper = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    call = SimpleNamespace(data={'source': source, '_licensing_generation': 1}, context=None)
    now = datetime(2026, 10, 31, 0, 0, tzinfo=timezone.utc)
    namespace = {
        '__package__': a.m.__package__, 'hass': a.hass, 'entry': a.entry,
        '_command_generation': [1], '_restore_gen': 1, 'source': source, 'call': call,
        f'force_{direction}_state': state, 'dt_util': SimpleNamespace(utcnow=lambda: now),
        'timedelta': timedelta, '_LOGGER': logging.getLogger(__name__),
        '_tesla_force_retry_expiry': lambda state, expiry, **kwargs: expiry,
        'persist_force_mode_state': AsyncMock(return_value=True),
        'handle_restore_normal': a.m.guard_battery_transition(a.hass, a.entry, restore),
        'ServiceCall': lambda domain, service, data, context: SimpleNamespace(data=data, context=context),
        'DOMAIN': 'power_sync', 'SERVICE_RESTORE_NORMAL': 'restore_normal',
        'async_dispatcher_send': lambda *args: None,
    }
    exec(compile(ast.Module(body=[helper], type_ignores=[]), str(ROOT / '__init__.py'), 'exec'), namespace)

    async def failed_force(_):
        await namespace[name]('tariff upload failed after prerequisite writes')
        return False

    service = a.m.guard_battery_transition(a.hass, a.entry, failed_force)
    async def request():
        if source == 'optimizer':
            return await guard.fenced_call(owner, lambda: service(call), f'force_{direction}')
        return await service(call)

    assert await asyncio.wait_for(request(), 0.5) is False
    assert len(restored) == 1 and not state['active']
    assert restored[0]['_allow_monitoring_restore'] is True
    assert restored[0]['_tesla_restore_generation'] == 1
    assert not lock.locked() and not manager.control_lock(a.entry.entry_id).locked()
    assert a.runtime['native_rate_battery_transitions'] == 0
    assert not manager._manual_command_owners


@pytest.mark.asyncio
async def test_manual_discharge_does_not_deadlock_optimizer_waiting_for_battery_route(admission):
    from test_ha_optimizer_runtime import Harness, guard

    a = admission
    harness = Harness()
    await harness.activate()
    manager = harness.client
    a.hass.data['power_sync_license_manager'] = manager
    owner = SimpleNamespace(hass=a.hass, entry_id=a.entry.entry_id)
    lock = route_lock(a)
    optimizer_owns_control = asyncio.Event()
    manual_started = asyncio.Event()
    events = []
    old_epoch = manager.entries.get(a.entry.entry_id, {}).get('generation', 0)

    async def optimizer_handler(call):
        events.append('optimizer')
        return True

    async def manual_handler(call):
        # This is the real manual handler's ownership handoff, after its
        # validation and inside the production battery-transition guard.
        await manager.relinquish(a.entry.entry_id)
        events.append('manual')
        return True

    optimizer_service = a.m.guard_battery_transition(a.hass, a.entry, optimizer_handler)
    manual_service = a.m.guard_battery_transition(a.hass, a.entry, manual_handler)

    async def optimizer_request():
        async def send():
            optimizer_owns_control.set()
            await manual_started.wait()
            # Let the manual route enter (or queue for control ownership)
            # before the optimizer requests its route lock.
            await asyncio.sleep(0)
            return await optimizer_service(SimpleNamespace(data={'source': 'optimizer'}))
        return await guard.fenced_call(owner, send, 'force_discharge')

    async def manual_request():
        await optimizer_owns_control.wait()
        manual_started.set()
        return await manual_service(SimpleNamespace(data={'source': 'user'}))

    tasks = [asyncio.create_task(optimizer_request()), asyncio.create_task(manual_request())]
    try:
        assert await asyncio.wait_for(asyncio.gather(*tasks), 1) == [True, True]
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert events == ['optimizer', 'manual']
    assert not lock.locked() and not manager.control_lock(a.entry.entry_id).locked()
    assert a.runtime['native_rate_battery_transitions'] == 0
    assert not guard.service_allowed(a.hass, {
        'source': 'optimizer', 'entry_id': a.entry.entry_id,
        '_licensing_generation': manager.admission.generation,
        '_licensing_entry_generation': old_epoch,
    }, 'handle_force_discharge')


@pytest.mark.asyncio
async def test_manual_wait_rechecks_setup_and_cleans_counters(admission):
    from test_ha_optimizer_runtime import Harness
    a = admission
    harness = Harness(); await harness.activate()
    manager = harness.client
    a.hass.data['power_sync_license_manager'] = manager
    lock = route_lock(a)
    events = []
    async def hardware(_): events.append('write')
    handler = a.m.guard_battery_transition(a.hass, a.entry, hardware)
    async with manager.control_lock(a.entry.entry_id):
        queued = asyncio.create_task(handler(None))
        await asyncio.sleep(0)
        assert not lock.locked()
        a.hass.data['power_sync'][a.entry.entry_id] = {}
    with pytest.raises(a.m.SiteImportAdmissionDenied, match='setup changed'):
        await queued
    assert not events and a.runtime['native_rate_battery_transitions'] == 0
    assert not lock.locked() and not manager.control_lock(a.entry.entry_id).locked()
    assert not manager._manual_command_owners


@pytest.mark.asyncio
async def test_manual_validation_and_nested_cleanup_preserve_ownership(admission):
    from test_ha_optimizer_runtime import Harness
    a = admission
    harness = Harness(); await harness.activate()
    manager = harness.client
    # Manual controls remain available without optimizer entitlement.
    manager.admission.suspend()
    a.hass.data['power_sync_license_manager'] = manager
    lock = route_lock(a)
    async def invalid(_): raise ValueError('invalid duration')
    with pytest.raises(ValueError, match='invalid duration'):
        await a.m.guard_battery_transition(a.hass, a.entry, invalid)(None)
    assert a.entry.entry_id not in manager.entries
    async def cleanup(_):
        await manager.relinquish(a.entry.entry_id)
    nested = a.m.guard_battery_transition(a.hass, a.entry, cleanup)
    async def manual(_):
        await nested(None)
    await asyncio.wait_for(a.m.guard_battery_transition(a.hass, a.entry, manual)(None), 1)
    assert manager.entries[a.entry.entry_id]['generation'] == 1
    assert not lock.locked() and not manager.control_lock(a.entry.entry_id).locked()
    assert not manager._manual_command_owners


@pytest.mark.asyncio
async def test_battery_route_serializes_competitors_but_allows_same_task_cleanup(admission):
    a=admission;lock=route_lock(a);entered=asyncio.Event();release=asyncio.Event();events=[]
    async def cleanup(_):events.append('cleanup')
    nested=a.m.guard_battery_transition(a.hass,a.entry,cleanup)
    async def first(_):
        events.append('first');entered.set();await release.wait()
        await nested(None);events.append('first_done')
    async def second(_):events.append('second')
    one=asyncio.create_task(a.m.guard_battery_transition(a.hass,a.entry,first)(None))
    await entered.wait()
    two=asyncio.create_task(a.m.guard_battery_transition(a.hass,a.entry,second)(None))
    await asyncio.sleep(0);assert events==['first']
    release.set();await asyncio.wait_for(asyncio.gather(one,two),1)
    assert events==['first','cleanup','first_done','second'] and not lock.locked()
    assert a.runtime['native_rate_battery_transitions']==0


@pytest.mark.asyncio
async def test_service_during_startup_waits_for_restore_and_old_handler_cannot_enter(admission):
    a=admission;lock=route_lock(a);events=[];entered=asyncio.Event();finish=asyncio.Event()
    a.runtime['native_max_startup_pending']=True
    async def hardware(_):events.append('control')
    handler=a.m.guard_battery_transition(a.hass,a.entry,hardware)
    async def recovery():
        async with lock:
            entered.set();await finish.wait();events.append('restore')
            a.runtime.pop('native_max_startup_pending')
    restore=asyncio.create_task(recovery());await entered.wait()
    service=asyncio.create_task(handler(None));await asyncio.sleep(0)
    assert events==[]
    finish.set();await asyncio.gather(restore,service)
    assert events==['restore','control']
    replacement={'native_max_startup_pending':True}
    a.hass.data['power_sync'][a.entry.entry_id]=replacement
    with pytest.raises(a.m.SiteImportAdmissionDenied,match='replaced setup'):
        await handler(None)
    assert events==['restore','control']


@pytest.mark.asyncio
async def test_cancelled_waiting_battery_command_does_not_take_or_poison_route(admission):
    a=admission;lock=route_lock(a);events=[]
    async def hardware(_):events.append('control')
    handler=a.m.guard_battery_transition(a.hass,a.entry,hardware)
    async with lock:
        queued=asyncio.create_task(handler(None));await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):await queued
    assert not events and a.runtime['native_rate_battery_transitions']==0
    await handler(None)
    assert events==['control'] and not lock.locked()
