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
