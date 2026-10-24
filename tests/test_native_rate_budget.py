"""Durable shared claims and prospective demand survive races and restart."""
import asyncio
from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest

path = Path(__file__).parents[1] / 'custom_components/power_sync/native_rate_budget.py'
spec = importlib.util.spec_from_file_location('native_budget_probe', path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
NOW = 1800000000.0


class Store:
    def __init__(self): self.data = None; self.fail = False
    async def async_load(self): return deepcopy(self.data)
    async def async_save(self, data):
        if self.fail: raise OSError('disk unavailable')
        self.data = deepcopy(data)


def session(identity='provider:car', **changes):
    return dict(identity=identity, baseline_amps=32, attempted_amps=20,
        confirmed_amps=10, watts_per_amp=230, uncertain=True, **changes)


@pytest.mark.asyncio
async def test_concurrent_entries_cannot_claim_same_vin_or_provider_identity():
    for same_vin in (False, True):
        store = Store(); budget = m.SharedRateBudget(store, clock=lambda: NOW)
        one = m.EntryRateJournal(budget, 'one', 'site')
        two = m.EntryRateJournal(budget, 'two', 'site')
        results = await asyncio.gather(
            one.async_save({'sessions': {'VIN': session()}}),
            two.async_save({'sessions': {'VIN' if same_vin else 'OTHER': session()}}),
            return_exceptions=True)
        assert sum(isinstance(r, ValueError) for r in results) == 1
        assert sum(len(e['sessions']) for e in store.data['entries'].values()) == 1


@pytest.mark.asyncio
async def test_pending_draw_is_reserved_until_aligned_and_returns_when_car_draw_drops():
    budget = m.SharedRateBudget(Store(), clock=lambda: NOW)
    journal = m.EntryRateJournal(budget, 'one', 'site')
    await journal.async_save({'sessions': {'VIN': session()}})
    assert budget.unobserved_w('site') == 4600
    budget.observe('VIN', watts=4600, at=NOW, meter_at=NOW)
    assert budget.unobserved_w('site') == 0
    budget.observe('VIN', watts=2300, at=NOW, meter_at=NOW)
    assert budget.unobserved_w('site') == 2300
    budget.observe('VIN', watts=4600, at=NOW-8, meter_at=NOW)
    assert budget.unobserved_w('site') == 4600
    assert budget.unobserved_w('other-site') == 0


@pytest.mark.asyncio
async def test_restart_keeps_full_reservation_and_cleanup_claim():
    store = Store(); budget = m.SharedRateBudget(store, clock=lambda: NOW)
    journal = m.EntryRateJournal(budget, 'one', 'site')
    record = session(); record['recovering'] = True
    await journal.async_save({'sessions': {'VIN': record}})
    resumed = m.SharedRateBudget(store, clock=lambda: NOW)
    await resumed.load()
    assert resumed.unobserved_w('site') == 7360
    with pytest.raises(ValueError, match='another site'):
        await m.EntryRateJournal(resumed, 'one', 'replacement-site').async_save({'sessions': {'VIN': record}})
    with pytest.raises(ValueError, match='Another PowerSync'):
        await m.EntryRateJournal(resumed, 'two', 'site').async_save({'sessions': {'VIN': record}})


@pytest.mark.asyncio
async def test_failed_save_cannot_publish_new_reservation_or_drop_existing_one():
    store = Store(); budget = m.SharedRateBudget(store, clock=lambda: NOW)
    journal = m.EntryRateJournal(budget, 'one', 'site')
    await journal.async_save({'sessions': {'VIN': session()}})
    revision = budget.revision
    store.fail = True
    with pytest.raises(OSError):
        await journal.async_save({'sessions': {}})
    assert budget.revision == revision
    assert budget.unobserved_w('site') == 4600


@pytest.mark.asyncio
async def test_confirmed_lower_setting_releases_only_its_unused_demand():
    budget = m.SharedRateBudget(Store(), clock=lambda: NOW)
    record = session(); record['uncertain'] = False
    await m.EntryRateJournal(budget, 'one', 'site').async_save({'sessions': {'VIN': record}})
    assert budget.unobserved_w('site') == 2300


def managed_evidence(**changes):
    return dict(grid_w=9000, cap_w=15000, actual_ev_w=0, battery_pending_w=0,
                vehicle_at=NOW, meter_at=NOW, **changes)


@pytest.mark.asyncio
async def test_managed_and_native_cannot_both_spend_same_physical_headroom():
    budget = m.SharedRateBudget(Store(), clock=lambda: NOW)
    budget.armed_sites.add('site')
    record = {'site': 'site', 'identity': 'provider:managed-car', 'reserved_w': 4600}
    result = await budget.admit_managed('MANAGED', record, managed_evidence(), lambda: True)
    assert result is not None
    native = session(identity='provider:native-car'); native['uncertain'] = False
    await m.EntryRateJournal(budget, 'one', 'site').async_save({'sessions': {'NATIVE': native}})
    assert budget.unobserved_w('site') == 6900
    second = {'site': 'site', 'identity': 'provider:other-car', 'reserved_w': 2300}
    assert await budget.admit_managed('OTHER', second, managed_evidence(), lambda: True) is None


@pytest.mark.asyncio
async def test_concurrent_managed_commands_reserve_before_second_admission():
    budget = m.SharedRateBudget(Store(), clock=lambda: NOW)
    budget.armed_sites.add('site')
    def record(vin): return {'site': 'site', 'identity': vin, 'reserved_w': 4600}
    results = await asyncio.gather(*(budget.admit_managed(vin, record(vin), managed_evidence(), lambda: True)
                                     for vin in ('ONE', 'TWO')))
    assert sum(r is not None for r in results) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('native_first', [True, False])
async def test_start_switch_and_amp_number_aliases_cannot_claim_different_vins(native_first):
    budget = m.SharedRateBudget(Store(), clock=lambda: NOW)
    journal = m.EntryRateJournal(budget, 'one', 'site')
    native = {'sessions': {'NATIVE': session(identity='esphome:bridge:car:charging_amps')}}
    managed = {'site': 'site', 'identity': 'esphome:bridge:car:charge_switch', 'reserved_w': 4600}
    if native_first:
        await journal.async_save(native)
        assert await budget.admit_managed('OTHER', managed, {}, lambda: True) is None
    else:
        assert await budget.admit_managed('OTHER', managed, {}, lambda: True)
        with pytest.raises(ValueError, match='Managed EV demand'):
            await journal.async_save(native)


@pytest.mark.asyncio
async def test_fallback_retains_old_provider_device_exclusion():
    budget = m.SharedRateBudget(Store(), clock=lambda: NOW)
    await budget.admit_managed('VIN', {'site':'site','identity':'esphome:bridge:car:amps',
        'reserved_w':4600}, {}, lambda: True)
    await budget.admit_managed('VIN', {'site':'site','identity':'tesla_fleet:provider:car2:amps',
        'reserved_w':2300}, {}, lambda: True)
    with pytest.raises(ValueError, match='Managed EV demand'):
        await m.EntryRateJournal(budget, 'two', 'site').async_save({'sessions': {
            'OTHER': session(identity='esphome:bridge:car:charge_switch')}})


@pytest.mark.asyncio
async def test_cancel_during_managed_commit_restores_only_the_unsent_ticket():
    entered, release = asyncio.Event(), asyncio.Event()
    class SlowStore(Store):
        pause = False
        async def async_save(self, data):
            if self.pause:
                self.pause = False
                entered.set()
                await release.wait()
            await super().async_save(data)
    store = SlowStore(); budget = m.SharedRateBudget(store, clock=lambda: NOW)
    record = {'site':'site','identity':'provider:car','reserved_w':2300}
    await budget.admit_managed('VIN', record, {}, lambda: True)
    previous = deepcopy(budget.managed['VIN'])
    store.pause = True
    task = asyncio.create_task(budget.admit_managed('VIN', {**record,'reserved_w':4600}, {}, lambda: True))
    await entered.wait()
    task.cancel(); release.set()
    with pytest.raises(asyncio.CancelledError): await task
    await budget.drain_storage()
    assert budget.managed['VIN'] == previous
    assert store.data['managed']['VIN'] == previous


@pytest.mark.asyncio
async def test_uncertain_managed_draw_cannot_be_erased_by_provider_fallback_or_restart():
    store = Store(); budget = m.SharedRateBudget(store, clock=lambda: NOW)
    record = {'site': 'site', 'identity': 'ble:car', 'reserved_w': 4600}
    await budget.admit_managed('VIN', record, {}, lambda: True)  # feature off, legacy permission
    smaller = {'site': 'site', 'identity': 'fleet:car', 'reserved_w': 2300}
    await budget.admit_managed('VIN', smaller, {}, lambda: True)
    resumed = m.SharedRateBudget(store, clock=lambda: NOW); await resumed.load()
    resumed.armed_sites.add('site')
    assert resumed.unobserved_w('site') == 4600
    with pytest.raises(ValueError, match='not yet reconciled'):
        await m.EntryRateJournal(resumed, 'one', 'site').async_save({'sessions': {'VIN': session()}})


@pytest.mark.asyncio
async def test_managed_command_cannot_overtake_native_cleanup():
    budget = m.SharedRateBudget(Store(), clock=lambda: NOW)
    record = session(); record['recovering'] = True
    await m.EntryRateJournal(budget, 'one', 'site').async_save({'sessions': {'VIN': record}})
    assert await budget.admit_managed('VIN', {'site': 'site', 'identity': 'managed:car', 'reserved_w': 4600},
                                      managed_evidence(), lambda: True) is None


@pytest.mark.asyncio
async def test_managed_demand_reconciles_only_after_two_fresh_settled_reports():
    now = [NOW]
    budget = m.SharedRateBudget(Store(), clock=lambda: now[0])
    record = {'site': 'site', 'identity': 'provider:car', 'entry_id': 'one',
              'target_amps': 20, 'target_w': 4600, 'reserved_w': 7360,
              'submitted_at': NOW, 'uncertain': True}
    await budget.admit_managed('VIN', record, {}, lambda: True)
    def sample(at, **changes):
        return dict(identity='provider:car', amps=20, vehicle_at=at, meter_at=at,
                    setting_at=at, actual_w=4600, charging=True, owner_active=True, **changes)
    for at in (NOW+1, NOW+2):
        now[0] = at
        await budget.reconcile_managed('VIN', sample(at))
        assert budget.managed['VIN']['reserved_w'] == 7360
    now[0] = NOW+5
    await budget.reconcile_managed('VIN', sample(now[0]))
    assert budget.managed['VIN']['reserved_w'] == 4600
    assert budget.managed['VIN']['uncertain'] is False
    budget.armed_sites.add('site')
    budget.observe('VIN', watts=1000, at=now[0], meter_at=now[0])
    assert budget.unobserved_w('site') == 3600
    stopped = sample(NOW+7); stopped.update(charging=False, owner_active=False, actual_w=0)
    now[0] = NOW+7; await budget.reconcile_managed('VIN', stopped)
    assert 'VIN' in budget.managed
    stopped.update(vehicle_at=NOW+11, meter_at=NOW+11, setting_at=NOW+11)
    now[0] = NOW+11; await budget.reconcile_managed('VIN', stopped)
    assert 'VIN' not in budget.managed


@pytest.mark.asyncio
async def test_inflight_managed_provider_task_prevents_releasing_its_reservation():
    now = [NOW]
    budget = m.SharedRateBudget(Store(), clock=lambda: now[0])
    record = {'site':'site','identity':'provider:car','reserved_w':4600,
              'target_w':4600,'target_amps':20,'submitted_at':NOW}
    await budget.admit_managed('VIN', record, {}, lambda: True)
    event = asyncio.Event()
    task = asyncio.create_task(event.wait()); budget.managed_tasks['VIN'] = task
    for at in (NOW+1, NOW+5):
        now[0] = at
        await budget.reconcile_managed('VIN', {'identity':'provider:car','amps':20,
            'vehicle_at':at,'meter_at':at,'setting_at':at,'actual_w':0,
            'charging':False,'owner_active':False})
    assert 'VIN' in budget.managed
    event.set(); await task
