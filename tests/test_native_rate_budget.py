"""Durable shared claims and prospective demand survive races and restart."""
import asyncio
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

path = Path(__file__).parents[1] / 'custom_components/power_sync/native_rate_budget.py'
spec = importlib.util.spec_from_file_location('native_budget_probe', path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
NOW = 1800000000.0


@pytest.mark.asyncio
@pytest.mark.parametrize('already_at_baseline', [False, True])
@pytest.mark.parametrize('settlement_times', [(1, 4), (30, 60)])
async def test_restored_setpoint_keeps_battery_headroom_through_ramp_and_restart(already_at_baseline, settlement_times):
    root = path.parent
    spec = importlib.util.spec_from_file_location('settlement_controller', root/'optimization/tesla_native_rate.py')
    rate = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = rate
    spec.loader.exec_module(rate)
    spec = importlib.util.spec_from_file_location('settlement_battery', root/'powerwall_local/battery_dispatch.py')
    battery = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(battery)
    clock = [NOW]
    store = Store()
    budget = m.SharedRateBudget(store, clock=lambda: clock[0])
    journal = m.EntryRateJournal(budget, 'entry', 'site')
    record = session(recovering=True)
    record.update(session='lease', entity_id='number.current', window_id='window', uncertain=False)
    await journal.async_save({'sessions': {'VIN': record}})

    class Ports:
        observed = rate.RateObservation('VIN', 'number.current', 'provider:car',
            32 if already_at_baseline else 10, 5, 32, 230, True, True,
            NOW, NOW, 'window', NOW-60, NOW+3600, NOW, 2300,
            1000, 1000, 10000, False, measured_ev_w=2300, measured_ev_at=NOW)
        writes = []
        async def read(self, _): return self.observed
        def enabled_vins(self): return []
        def permitted(self, *_): return True
        def superseded(self, *_): return False
        def release(self, *_): pass
        async def write(self, observed, amps, authorize):
            assert authorize()
            self.writes.append(amps)
            self.observed = replace(self.observed, current_amps=amps)
            return rate.RateWriteResult(True, True)

    ports = Ports()
    controller = rate.NativeRateController(journal, ports, clock=lambda: clock[0])
    await controller.tick()
    assert controller.sessions['VIN']['restore_settling']
    expected_writes = [] if already_at_baseline else [32]
    assert ports.writes == expected_writes

    # A battery renewal sees the restored 32 A envelope, even though the
    # acknowledged number entity says 32 A and the car still draws only 10 A.
    budget.observe('VIN', watts=2300, at=NOW, meter_at=NOW)
    snapshot = SimpleNamespace(soc=60, grid_status='SystemGridConnected',
        load_w=3300, solar_w=0, battery_w=-1000, grid_w=4300,
        backup_reserve_percent=20, grid_charging_enabled=True,
        raw={'config': {'site_info': {'max_site_meter_power_ac': 10000}}})
    config = dict(optimization_backup_reserve=.2, optimization_max_charge_w=10000,
                  optimization_max_grid_import_w=10000)
    def renewal():
        return battery.battery_watts('charge', 10000, snapshot,
            dict(config, native_ev_unobserved_w=budget.unobserved_w('site')),
            target_basis='grid_import')
    assert budget.unobserved_w('site') == 5060
    assert renewal() == -1640
    await controller.tick()
    assert ports.writes == expected_writes and controller.sessions

    # Storage recovery cannot mistake the acknowledged setting for settlement.
    budget = m.SharedRateBudget(store, clock=lambda: clock[0])
    journal = m.EntryRateJournal(budget, 'entry', 'site')
    controller = rate.NativeRateController(journal, ports, clock=lambda: clock[0])
    await controller.tick()
    assert budget.unobserved_w('site') == 7360
    budget.observe('VIN', watts=2300, at=NOW, meter_at=NOW)
    assert renewal() == -1640
    assert controller.sessions and ports.writes == expected_writes

    for index, delta in enumerate(settlement_times):
        clock[0] = NOW+delta
        ports.observed = replace(ports.observed, vehicle_at=clock[0],
            measured_ev_at=clock[0], meter_at=clock[0], measured_ev_w=7360)
        budget.observe('VIN', watts=7360, at=clock[0], meter_at=clock[0])
        await controller.tick()
        assert bool(controller.sessions) == (index == 0)
    assert budget.unobserved_w('site') == 0
    assert ports.writes == expected_writes


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


@pytest.mark.asyncio
async def test_native_max_is_durable_distinct_from_ev_and_releases_by_exact_token():
    store = Store(); budget = m.SharedRateBudget(store, clock=lambda: NOW)
    assert await budget.claim_native_max('battery', 'site', 'generation', NOW+60)
    assert not budget.site_armed('site')
    revision = budget.revision
    assert await budget.claim_native_max('battery', 'site', 'generation', NOW+60)
    assert budget.revision == revision
    restarted = m.SharedRateBudget(store, clock=lambda: NOW+120)
    await restarted.load()
    assert restarted.native_max_claim('site')['token'] == 'generation'
    assert restarted.native_max_claim('site')['direction'] == 'charge'
    assert not await restarted.claim_native_max('other', 'site', 'new', NOW+180)
    assert not await restarted.release_native_max('battery', 'site', 'wrong')
    with pytest.raises(ValueError, match='Native Max owns'):
        await m.EntryRateJournal(restarted, 'ev', 'site').async_save({'sessions': {'VIN': session()}})
    assert await restarted.release_native_max('battery', 'site', 'generation')
    assert restarted.native_max_claim('site') is None
    assert store.data['entries'] == {}


@pytest.mark.asyncio
async def test_discharge_native_max_and_managed_ev_can_arrive_in_either_order():
    record = {'site': 'site', 'identity': 'provider:car', 'reserved_w': 3450,
              'operation': 'rate', 'target_amps': 15, 'uncertain': True,
              'submitted_at': NOW - 10800}
    evidence = {'rate_reduction': False, 'current_amps': 10,
                'grid_w': 0, 'cap_w': 15000, 'actual_ev_w': 2300,
                'battery_pending_w': 0, 'vehicle_at': NOW, 'meter_at': NOW,
                'native_flexible_battery_w': 0}

    claim_first = m.SharedRateBudget(Store(), clock=lambda: NOW)
    assert await claim_first.claim_native_max(
        'battery', 'site', 'generation', NOW + 60, direction='discharge',
        ev_coexistence=True)
    ticket = await claim_first.admit_managed(
        'VIN', record, evidence, lambda: True)
    assert ticket is not None
    assert ticket['record']['native_max_token'] == 'generation'
    assert ticket['record']['native_max_direction'] == 'discharge'

    managed_first = m.SharedRateBudget(Store(), clock=lambda: NOW)
    assert await managed_first.admit_managed(
        'VIN', record, evidence, lambda: True)
    assert await managed_first.claim_native_max(
        'battery', 'site', 'generation', NOW + 60, direction='discharge',
        ev_coexistence=True)
    assert managed_first.native_max_claim('site')['direction'] == 'discharge'


@pytest.mark.asyncio
async def test_charge_native_max_and_managed_ev_can_arrive_in_either_order():
    record = {'site': 'site', 'identity': 'provider:car', 'reserved_w': 3450,
              'operation': 'rate', 'target_amps': 15, 'uncertain': True}
    evidence = {'rate_reduction': False, 'current_amps': 10,
                'grid_w': 0, 'cap_w': 15000, 'actual_ev_w': 2300,
                'battery_pending_w': 0, 'vehicle_at': NOW, 'meter_at': NOW,
                'native_flexible_battery_w': 0}
    claim_first = m.SharedRateBudget(Store(), clock=lambda: NOW)
    assert await claim_first.claim_native_max(
        'battery', 'site', 'generation', NOW + 60, direction='charge',
        ev_coexistence=True)
    ticket = await claim_first.admit_managed('VIN', record, evidence, lambda: True)
    assert ticket is not None
    assert ticket['record']['native_max_ev_coexistence'] is True

    managed_first = m.SharedRateBudget(Store(), clock=lambda: NOW)
    assert await managed_first.admit_managed('VIN', record, evidence, lambda: True)
    assert await managed_first.claim_native_max(
        'battery', 'site', 'generation', NOW + 60, direction='charge',
        ev_coexistence=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('native_first', [True, False])
async def test_capable_native_max_coexists_with_native_rate_in_either_order(native_first):
    budget = m.SharedRateBudget(Store(), clock=lambda: NOW)
    journal = m.EntryRateJournal(budget, 'ev', 'site')
    native = {'sessions': {'NATIVE': session(identity='provider:native-car')}}
    if native_first:
        await journal.async_save(native)
        assert await budget.claim_native_max(
            'battery', 'site', 'generation', NOW + 60,
            direction='charge', ev_coexistence=True)
    else:
        assert await budget.claim_native_max(
            'battery', 'site', 'generation', NOW + 60,
            direction='charge', ev_coexistence=True)
        await journal.async_save(native)
    assert budget.native_max_claim('site')['ev_coexistence'] is True
    assert 'NATIVE' in budget.entries['ev'].get('sessions', {})


@pytest.mark.asyncio
async def test_native_max_legacy_or_charge_claim_remains_conservative_for_ev_increase():
    store = Store()
    budget = m.SharedRateBudget(store, clock=lambda: NOW)
    assert await budget.claim_native_max('battery', 'site', 'generation', NOW + 60)
    record = {'site': 'site', 'identity': 'provider:car', 'reserved_w': 3450,
              'operation': 'rate', 'target_amps': 15}
    assert await budget.admit_managed(
        'VIN', record, {'rate_reduction': False, 'current_amps': 10}, lambda: True) is None

    legacy_store = Store()
    legacy_store.data = {'entries': {'native_max:battery': {
        'site': 'site', 'sessions': {}, 'native_max': {
            'entry_id': 'battery', 'token': 'legacy', 'expires_at': NOW + 60,
        }}}, 'managed': {}}
    legacy = m.SharedRateBudget(legacy_store, clock=lambda: NOW)
    await legacy.load()
    assert legacy.native_max_claim('site').get('direction') is None
    assert await legacy.admit_managed(
        'VIN', record, {'rate_reduction': False, 'current_amps': 10}, lambda: True) is None
    with pytest.raises(ValueError, match='Invalid native Max site claim'):
        await budget.claim_native_max(
            'other', 'other-site', 'bad-direction', NOW + 60, direction='unknown')


@pytest.mark.asyncio
async def test_preparing_discharge_claim_blocks_ev_increases_until_exact_upgrade():
    store = Store()
    budget = m.SharedRateBudget(store, clock=lambda: NOW)
    existing = {'site': 'site', 'identity': 'provider:existing', 'reserved_w': 2300,
                'operation': 'rate', 'target_amps': 10}
    evidence = {'rate_reduction': False, 'current_amps': 10}
    assert await budget.admit_managed('VIN', existing, evidence, lambda: True)
    assert await budget.claim_native_max(
        'battery', 'site', 'generation', NOW + 60, direction='discharge',
        ev_coexistence=False, preparing=True)
    assert budget.native_max_claim('site')['preparing'] is True
    increase = {'site': 'site', 'identity': 'provider:new', 'reserved_w': 2300,
                'operation': 'rate', 'target_amps': 20}
    assert await budget.admit_managed('VIN2', increase,
        {'rate_reduction': False, 'current_amps': 10}, lambda: True) is None
    assert await budget.admit_managed(
        'VIN3', {**increase, 'identity': 'provider:reduction'},
        {'rate_reduction': True, 'current_amps': 20}, lambda: True) is not None
    assert await budget.claim_native_max(
        'battery', 'site', 'generation', NOW + 60, direction='discharge',
        ev_coexistence=True)
    assert budget.native_max_claim('site')['preparing'] is False
    assert budget.native_max_claim('site')['ev_coexistence'] is True


@pytest.mark.asyncio
async def test_preparing_claim_rejects_contradictory_coexistence_and_journal_flags():
    budget = m.SharedRateBudget(Store(), clock=lambda: NOW)
    with pytest.raises(ValueError, match='Native Max reservation journal is invalid'):
        await budget.claim_native_max(
            'battery', 'site', 'generation', NOW + 60, direction='discharge',
            ev_coexistence=True, preparing=True)

    store = Store()
    store.data = {'entries': {'native_max:battery': {
        'site': 'site', 'sessions': {}, 'native_max': {
            'entry_id': 'battery', 'token': 'generation', 'expires_at': NOW + 60,
            'direction': 'discharge', 'ev_coexistence': True, 'preparing': True}}},
        'managed': {}}
    with pytest.raises(ValueError, match='Native Max reservation journal is invalid'):
        await m.SharedRateBudget(store, clock=lambda: NOW).load()


@pytest.mark.asyncio
async def test_preparing_claim_cannot_upgrade_after_deadline():
    clock = [NOW]
    budget = m.SharedRateBudget(Store(), clock=lambda: clock[0])
    assert await budget.claim_native_max(
        'battery', 'site', 'generation', NOW + 60, direction='discharge',
        preparing=True)
    clock[0] = NOW + 60
    assert not await budget.claim_native_max(
        'battery', 'site', 'generation', NOW + 60, direction='discharge',
        ev_coexistence=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('owner', ['native', 'managed', 'armed'])
async def test_native_max_cannot_overtake_ev_acquisition_or_cleanup(owner):
    budget = m.SharedRateBudget(Store(), clock=lambda: NOW)
    if owner == 'native':
        await m.EntryRateJournal(budget, 'ev', 'site').async_save({'sessions': {'VIN': session()}})
    elif owner == 'managed':
        await budget.admit_managed('VIN', {'site':'site', 'identity':'provider:car',
            'reserved_w':0}, {}, lambda: True)
    else:
        budget.armed_sites.add('site')
    assert not await budget.claim_native_max('battery', 'site', 'generation', NOW+60)
    assert await budget.claim_native_max('battery', 'another-site', 'generation', NOW+60)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation,current,target,expected', [
    ('start', 20, 20, False), ('rate', 20, 21, False),
    ('rate', 20, 19, True), ('rate', 20, 0, True),
    ('rate', None, 10, False), ('rate', True, 1, False)])
async def test_native_max_allows_only_identified_ev_rate_reductions(operation,current,target,expected):
    store = Store(); budget = m.SharedRateBudget(store, clock=lambda: NOW)
    assert await budget.claim_native_max('battery', 'site', 'generation', NOW+60)
    record = {'site':'site', 'identity':'provider:car', 'entry_id':'ev',
              'reserved_w':4600, 'operation':operation, 'target_amps':target}
    ticket = await budget.admit_managed('VIN', record,
        {'rate_reduction': operation == 'rate' and current is not None and target <= current,
         'current_amps': current}, lambda: True)
    assert (ticket is not None) is expected
    if ticket:
        assert ticket['record']['native_max_token'] == 'generation'
        restarted = m.SharedRateBudget(store, clock=lambda: NOW)
        await restarted.load()
        assert restarted.native_max_claim('site')
        await budget.abort_managed(ticket)
        assert budget.managed == {}
        assert store.data['entries']['native_max:battery']['native_max']['token'] == 'generation'


@pytest.mark.asyncio
async def test_native_max_and_prospective_managed_start_have_one_winner():
    budget = m.SharedRateBudget(Store(), clock=lambda: NOW)
    claim, ticket = await asyncio.gather(
        budget.claim_native_max('battery','site','generation',NOW+60),
        budget.admit_managed('VIN', {'site':'site','identity':'provider:car','reserved_w':4600,
            'operation':'start','target_amps':20}, {}, lambda: True))
    assert int(claim) + int(ticket is not None) == 1


@pytest.mark.asyncio
async def test_native_max_failed_persistence_never_publishes_or_releases_owner():
    store = Store(); budget = m.SharedRateBudget(store, clock=lambda: NOW)
    store.fail = True
    with pytest.raises(OSError):
        await budget.claim_native_max('battery','site','generation',NOW+60)
    assert budget.native_max_claim('site') is None
    store.fail = False
    assert await budget.claim_native_max('battery','site','generation',NOW+60)
    revision = budget.revision
    store.fail = True
    with pytest.raises(OSError):
        await budget.release_native_max('battery','site','generation')
    assert budget.native_max_claim('site')['token'] == 'generation'
    assert budget.revision == revision
