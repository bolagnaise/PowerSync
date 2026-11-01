"""Verified native limits may yield measured battery charging to aggregate EVs."""
import asyncio
from dataclasses import replace
import time
from types import SimpleNamespace as NS

import pytest

from test_native_rate_admission import admission, NOW, VIN
from test_tesla_native_rate_ha import adapter, live_adapter


def qualify(a):
    for key in ('force_charge_state', 'force_discharge_state', 'hold_soc_state', 'self_consumption_state'):
        a.runtime[key] = {'active': False, 'saved_tariff': None, 'expires_at': None}
    a.entry.data.update(powerwall_local_paired=True, powerwall_local_din='DIN',
        powerwall_local_energy_site_id='site', powerwall_local_public_key_der='public')
    a.runtime['entry'] = a.entry
    a.snapshot.grid_w = 15000
    a.snapshot.battery_w = -10000
    a.snapshot.load_w = 5000
    a.snapshot.solar_w = 0
    a.snapshot.operation_mode = 'autonomous'
    a.snapshot.grid_status = 'SystemGridConnected'
    a.snapshot.observed_monotonic = time.monotonic()
    a.snapshot.config_observed_monotonic = a.snapshot.observed_monotonic
    a.snapshot.raw['config']['default_real_mode'] = 'autonomous'
    local = a.runtime['powerwall_local']['coordinator']
    control = NS(site_id='site')
    client = NS(_control_matches_entry=lambda: True, _transport=NS(_site_control=control))
    local.client = client
    a.runtime['powerwall_local']['client'] = client
    a.runtime['powerwall_site_control'] = control
    if hasattr(a, 'ports'):
        a.ports.pairing = a.ports._pairing_identity()
        a.state.state = '10'
    else:
        a.m._EV_SUBMISSION_CONTEXT.get()['params']['owner_mode'] = 'boost'
    return a


@pytest.mark.asyncio
@pytest.mark.parametrize('monitoring', [False, True])
async def test_verified_native_charge_yields_to_manual_ev_without_future_discharge(admission, monitoring):
    a = qualify(admission)
    a.entry.options['monitoring_mode'] = monitoring
    assert await a.m.prepare_ev_submission(a.hass, a.state.entity_id, operation='rate', amps=20)
    assert a.budget.unobserved_w('site') == 2300
    evidence = a.m._site_evidence(a.hass, a.entry, a.runtime, VIN, {})
    assert evidence['native_flexible_battery_w'] == 10000
    assert evidence['battery_generation'] is None
    assert evidence['borrowable_battery_w'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('phase,expected', [('active', 10000), ('applying', 0)])
async def test_active_capable_native_charge_can_lend_measured_power(admission, phase, expected):
    a = qualify(admission)
    a.budget.armed_sites.clear()
    assert await a.budget.claim_native_max(
        'one', 'site', 'native-generation', NOW + 60,
        direction='charge', ev_coexistence=True)
    adapter = NS(entry_id='one', token='native-generation', identity='native-identity',
        current_identity=lambda: 'native-identity', authority=lambda: True, site='site',
        direction='charge', ev_coexistence=True)
    session = NS(adapter=adapter, status=lambda: {
        'phase': phase, 'direction': 'charge', 'active': phase == 'active',
        'id': 'native-generation', 'expires_at': NOW + 60},
        record={'id': 'native-generation', 'phase': phase, 'expires_at': NOW + 60,
        'baseline': {'direction': 'charge'}})
    a.runtime['powerwall_native_max_charge'] = session
    a.runtime['force_charge_state'].update(
        active=True, native_max_charge=True, native_tariff_force=True,
        power_target_basis='native_tariff', command_status='accepted' if phase == 'active' else 'pending')
    evidence = a.m._site_evidence(a.hass, a.entry, a.runtime, VIN, {})
    assert evidence['native_flexible_battery_w'] == expected


def _native_session(a, *, direction='charge', phase='active', token='native-generation'):
    adapter = NS(entry_id='one', token=token, identity='native-identity',
        current_identity=lambda: 'native-identity', authority=lambda: True, site='site',
        direction=direction, ev_coexistence=True)
    record = {'id': token, 'phase': phase, 'expires_at': NOW + 60,
              'baseline': {'direction': direction}}
    return NS(adapter=adapter, pending=True,
        status=lambda: {'phase': phase, 'direction': direction,
            'active': phase == 'active', 'id': token,
            'expires_at': NOW + 60}, record=record)


@pytest.mark.asyncio
@pytest.mark.parametrize('direction', ['charge', 'discharge'])
async def test_native_max_coexistence_proof_requires_exact_active_direction(admission, direction):
    from managed_admission_probe.native_battery_flexibility import native_max_coexistence_proof

    a = qualify(admission)
    a.budget.armed_sites.clear()
    assert await a.budget.claim_native_max(
        'one', 'site', 'native-generation', NOW + 60,
        direction=direction, ev_coexistence=True)
    a.runtime['powerwall_native_max_charge'] = _native_session(a, direction=direction)
    if direction == 'discharge':
        a.runtime['force_discharge_state'].update(
            active=True, native_tariff_force=True, power_target_basis='native_tariff',
            command_status='accepted')
    proof = native_max_coexistence_proof(a.hass, a.entry, a.runtime, 'site', a.budget)
    assert proof and proof[3] == direction
    assert proof == native_max_coexistence_proof(a.hass, a.entry, a.runtime, 'site', a.budget)
    if direction == 'discharge':
        assert a.m._site_evidence(a.hass, a.entry, a.runtime, VIN, {})['native_flexible_battery_w'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['prepared', 'applying', 'restoring', 'settling'])
async def test_native_max_coexistence_proof_denies_non_active_phase(admission, phase):
    from managed_admission_probe.native_battery_flexibility import native_max_coexistence_proof

    a = qualify(admission)
    a.budget.armed_sites.clear()
    assert await a.budget.claim_native_max(
        'one', 'site', 'native-generation', NOW + 60,
        direction='charge', ev_coexistence=True)
    a.runtime['powerwall_native_max_charge'] = _native_session(a, phase=phase)
    a.runtime['force_charge_state'].update(
        active=True, native_max_charge=True, native_tariff_force=True,
        power_target_basis='native_tariff', command_status='pending')
    assert native_max_coexistence_proof(a.hass, a.entry, a.runtime, 'site', a.budget) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('missing', ['session', 'stale_config', 'owner'])
async def test_native_max_coexistence_proof_denies_missing_stale_or_foreign_authority(admission, missing):
    from managed_admission_probe.native_battery_flexibility import native_max_coexistence_proof

    a = qualify(admission)
    a.budget.armed_sites.clear()
    assert await a.budget.claim_native_max(
        'one', 'site', 'native-generation', NOW + 60,
        direction='charge', ev_coexistence=True)
    a.runtime['powerwall_native_max_charge'] = _native_session(a)
    if missing == 'session':
        a.runtime.pop('powerwall_native_max_charge')
    elif missing == 'stale_config':
        a.snapshot.config_observed_monotonic -= 16
    else:
        other = NS(entry_id='two', data={'tesla_energy_site_id': 'site'}, options={})
        a.hass.data['power_sync']['two'] = {
            'entry': other, 'powerwall_site_control': NS(site_id='site')}
    assert native_max_coexistence_proof(a.hass, a.entry, a.runtime, 'site', a.budget) is None


@pytest.mark.asyncio
async def test_native_max_proof_survives_new_normal_samples_without_timestamp_identity(admission):
    from managed_admission_probe.native_battery_flexibility import native_max_coexistence_proof

    a = qualify(admission)
    a.budget.armed_sites.clear()
    assert await a.budget.claim_native_max(
        'one', 'site', 'native-generation', NOW + 60,
        direction='charge', ev_coexistence=True)
    a.runtime['powerwall_native_max_charge'] = _native_session(a)
    before = native_max_coexistence_proof(a.hass, a.entry, a.runtime, 'site', a.budget)
    a.snapshot.observed_monotonic = time.monotonic() - 1
    a.snapshot.config_observed_monotonic = a.snapshot.observed_monotonic
    a.runtime['powerwall_local']['coordinator'].last_success_ts = NOW - 1
    assert native_max_coexistence_proof(a.hass, a.entry, a.runtime, 'site', a.budget) == before


@pytest.mark.asyncio
async def test_native_max_capability_without_live_session_denies_increase_before_ticket(admission):
    a = qualify(admission)
    a.budget.armed_sites.clear()
    assert await a.budget.claim_native_max(
        'one', 'site', 'native-generation', NOW + 60,
        direction='charge', ev_coexistence=True)
    with pytest.raises(a.m.SiteImportAdmissionDenied, match='coexistence authority'):
        await a.m.prepare_ev_submission(
            a.hass, a.state.entity_id, operation='rate', amps=20)
    assert not a.budget.managed


@pytest.mark.asyncio
async def test_native_max_capability_does_not_block_rate_reduction_without_live_session(admission):
    a = qualify(admission)
    a.budget.armed_sites.clear()
    assert await a.budget.claim_native_max(
        'one', 'site', 'native-generation', NOW + 60,
        direction='charge', ev_coexistence=True)
    assert await a.m.prepare_ev_submission(
        a.hass, a.state.entity_id, operation='rate', amps=5)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation,amps', [('start', None), ('rate', 20)])
async def test_native_max_discharge_coexistence_allows_ev_demand_without_battery_credit(
    admission, operation, amps,
):
    a = qualify(admission)
    a.budget.armed_sites.clear()
    assert await a.budget.claim_native_max(
        'one', 'site', 'native-generation', NOW + 60,
        direction='discharge', ev_coexistence=True)
    a.runtime['powerwall_native_max_charge'] = _native_session(a, direction='discharge')
    a.runtime['force_discharge_state'].update(
        active=True, native_tariff_force=True, power_target_basis='native_tariff',
        command_status='accepted')
    a.snapshot.grid_w = 6000
    assert await a.m.prepare_ev_submission(
        a.hass, a.state.entity_id, operation=operation, amps=amps)
    assert a.m._site_evidence(a.hass, a.entry, a.runtime, VIN, {})[
        'native_flexible_battery_w'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('operation,amps', [('start', None), ('rate', 20)])
async def test_native_max_discharge_proof_change_after_ticket_denies_submission(
    admission, monkeypatch, operation, amps,
):
    a = qualify(admission)
    a.budget.armed_sites.clear()
    assert await a.budget.claim_native_max(
        'one', 'site', 'native-generation', NOW + 60,
        direction='discharge', ev_coexistence=True)
    a.runtime['powerwall_native_max_charge'] = _native_session(a, direction='discharge')
    a.runtime['force_discharge_state'].update(
        active=True, native_tariff_force=True, power_target_basis='native_tariff',
        command_status='accepted')
    a.snapshot.grid_w = 6000
    original = a.budget.admit_managed

    async def interleave(*args, **kwargs):
        ticket = await original(*args, **kwargs)
        a.runtime['powerwall_native_max_charge'].adapter.token = 'replacement'
        return ticket

    monkeypatch.setattr(a.budget, 'admit_managed', interleave)
    with pytest.raises(a.m.SiteImportAdmissionDenied):
        await a.m.prepare_ev_submission(
            a.hass, a.state.entity_id, operation=operation, amps=amps)
    assert not a.budget.managed


@pytest.mark.asyncio
@pytest.mark.parametrize('conflict', ['plain_force', 'other_token', 'settings_pending', 'backup_zero'])
async def test_native_charge_exception_preserves_real_control_conflicts(admission, conflict):
    a = qualify(admission)
    a.budget.armed_sites.clear()
    assert await a.budget.claim_native_max('one', 'site', 'native-generation', NOW + 60,
                                         direction='charge', ev_coexistence=True)
    a.runtime['powerwall_native_max_charge'] = NS(
        pending=True, adapter=NS(entry_id='one', token='native-generation'),
        status=lambda: {'phase': 'active', 'direction': 'charge', 'active': True})
    a.runtime['force_charge_state'].update(
        active=True, native_max_charge=True, native_tariff_force=True,
        power_target_basis='native_tariff', command_status='accepted')
    if conflict == 'plain_force':
        a.runtime['force_charge_state']['native_max_charge'] = False
    elif conflict == 'other_token':
        a.runtime['powerwall_native_max_charge'].adapter.token = 'other-generation'
    elif conflict == 'settings_pending':
        a.runtime['powerwall_settings_pending'] = {'site_id': 'site', 'id': 'unresolved-write'}
    else:
        a.runtime['max_backup_saved_reserve'] = 0
    assert a.m._site_evidence(a.hass, a.entry, a.runtime, VIN, {})['native_flexible_battery_w'] == 0


@pytest.mark.asyncio
async def test_closed_native_controller_does_not_block_normal_site_flexibility(admission):
    a = qualify(admission)
    a.runtime['powerwall_native_max_charge'] = NS(pending=False, status=lambda: {})
    assert a.m._site_evidence(a.hass, a.entry, a.runtime, VIN, {})['native_flexible_battery_w'] == 10000


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['stricter_cap', 'missing_config_time', 'stale_config',
    'stale_meter', 'offgrid', 'mode', 'unpaired', 'wrong_client', 'unknown_limit',
    'sentinel', 'force', 'transition', 'pending', 'max_backup', 'dispatch', 'foreign_force'])
async def test_unproven_flexibility_cannot_authorize_at_full_site(admission, invalid):
    a = qualify(admission)
    if invalid == 'stricter_cap': a.entry.options['optimization_max_grid_import_w'] = 10000
    if invalid == 'missing_config_time': a.snapshot.config_observed_monotonic = None
    if invalid == 'stale_config': a.snapshot.config_observed_monotonic -= 16
    if invalid == 'stale_meter': a.snapshot.observed_monotonic -= 16
    if invalid == 'offgrid': a.snapshot.grid_status = 'SystemIslandedActive'
    if invalid == 'mode': a.snapshot.operation_mode = 'backup'
    if invalid == 'unpaired': a.entry.data['powerwall_local_paired'] = False
    if invalid == 'wrong_client': a.runtime['powerwall_local']['coordinator'].client = NS()
    if invalid == 'unknown_limit': a.snapshot.raw['config']['site_info'].pop('max_site_meter_power_ac')
    if invalid == 'sentinel': a.snapshot.raw['config']['site_info']['max_site_meter_power_ac'] = 1e9
    if invalid == 'force': a.runtime['hold_soc_state'] = {'active': True}
    if invalid == 'transition': a.runtime['native_rate_battery_transitions'] = 1
    if invalid == 'pending': a.runtime['powerwall_settings_pending'] = {'site_id': 'site'}
    if invalid == 'max_backup': a.runtime['max_backup_end_ts'] = NOW+60
    if invalid == 'dispatch': a.runtime['powerwall_battery_dispatch'] = NS(
        pending=False, charge_generation=None, prospective_charge_w=10000, _last_watts=-10000)
    if invalid == 'foreign_force':
        entry = NS(entry_id='two', data={'tesla_energy_site_id': 'site'}, options={})
        a.hass.data['power_sync']['two'] = {'entry': entry, 'force_charge_state': {'active': True}}
    with pytest.raises((a.m.SiteImportAdmissionDenied, ValueError)):
        await a.m.prepare_ev_submission(a.hass, a.state.entity_id, operation='rate', amps=20)
    assert not a.budget.managed


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['cap', 'mode', 'owner', 'runtime', 'pairing', 'stale', 'battery', 'coordinator'])
async def test_managed_final_boundary_rechecks_native_proof_after_journal_wait(admission, change):
    a = qualify(admission)
    def revoke():
        if change == 'cap': a.snapshot.raw['config']['site_info']['max_site_meter_power_ac'] = 16
        if change == 'mode': a.snapshot.operation_mode = 'backup'
        if change == 'owner': a.runtime['hold_soc_state'] = {'active': True}
        if change == 'runtime': a.hass.data['power_sync']['one'] = {}
        if change == 'pairing': a.entry.data['powerwall_local_din'] = 'replacement'
        if change == 'stale': a.snapshot.config_observed_monotonic -= 16
        if change == 'battery':
            a.snapshot.battery_w = 0
            a.snapshot.load_w += 10000
        if change == 'coordinator':
            a.runtime['powerwall_local']['coordinator'] = NS(**vars(a.runtime['powerwall_local']['coordinator']))
    a.store.hook = revoke
    with pytest.raises(a.m.SiteImportAdmissionDenied):
        await a.m.prepare_ev_submission(a.hass, a.state.entity_id, operation='rate', amps=20)
    assert not a.budget.managed


@pytest.mark.asyncio
async def test_native_flexible_capacity_is_shared_once_between_two_vins(admission):
    a = qualify(admission)
    assert await a.m.prepare_ev_submission(a.hass, a.state.entity_id, operation='rate', amps=20)
    evidence = a.m._site_evidence(a.hass, a.entry, a.runtime, VIN, {})
    evidence['actual_ev_w'] = 0
    second = '5YJ3E1EA7KF000002'
    record = {'entry_id': 'one', 'site': 'site', 'identity': 'tesla_fleet:other:car:amps',
              'reserved_w': 4600, 'target_w': 4600, 'target_amps': 20, 'operation': 'rate'}
    assert await a.budget.admit_managed(second, record, evidence, lambda: True)
    assert a.budget.unobserved_w('site') == 6900
    third = {**record, 'identity': 'tesla_fleet:third:car:amps', 'reserved_w': 4600}
    assert await a.budget.admit_managed('5YJ3E1EA7KF000003', third, evidence, lambda: True) is None
    a.budget.observe(second, watts=4600, at=NOW, meter_at=NOW)
    evidence['grid_w'] += 4600
    assert evidence['grid_w'] + a.budget.unobserved_w('site') - 10000 == 11900
    assert await a.budget.admit_managed('5YJ3E1EA7KF000003', third, evidence, lambda: True) is None


@pytest.mark.asyncio
async def test_firm_house_and_ev_cannot_borrow_future_battery_discharge(admission):
    a = qualify(admission)
    a.snapshot.grid_w = 25000
    with pytest.raises(a.m.SiteImportAdmissionDenied):
        await a.m.prepare_ev_submission(a.hass, a.state.entity_id, operation='rate', amps=20)
    assert not a.budget.managed


@pytest.mark.asyncio
@pytest.mark.parametrize('restore', [False, True])
async def test_native_planning_and_actual_final_restore_use_same_verified_credit(live_adapter, restore):
    a = qualify(live_adapter)
    observed = await a.ports.read(VIN)
    assert observed.native_flexible_battery_w == 10000
    assert observed.planned_battery_w == 10000
    assert observed.borrowable_battery_w == 0
    assert a.m.decide_rate(observed, 32, NOW)[0] == 32
    assert a.m.restoration_fits(observed, 32, NOW)
    a.ports.controller.sessions[VIN] = {'baseline_amps': 32, 'recovering': restore}
    result = await a.ports.write(observed, 32, lambda: a.ports.current())
    assert result.confirmed and result.submitted
    assert a.calls == [('number', 'set_value', {'entity_id': a.state.entity_id, 'value': 32})]


@pytest.mark.asyncio
@pytest.mark.parametrize('restore', [False, True])
@pytest.mark.parametrize('change', ['cap', 'stricter_cap', 'owner', 'runtime', 'pairing', 'stale', 'battery', 'coordinator'])
async def test_native_final_queue_revocation_sends_zero_provider_commands(live_adapter, restore, change):
    a = qualify(live_adapter)
    observed = await a.ports.read(VIN)
    a.ports.controller.sessions[VIN] = {'baseline_amps': 32, 'recovering': restore}
    lock = a.budget.admission_lock('site')
    await lock.acquire()
    task = asyncio.create_task(a.ports.write(observed, 32, lambda: a.ports.current()))
    await asyncio.sleep(0)
    if change == 'cap': a.snapshot.raw['config']['site_info']['max_site_meter_power_ac'] = 16
    if change == 'stricter_cap': a.entry.options['optimization_max_grid_import_w'] = 10000
    if change == 'owner': a.runtime['hold_soc_state'] = {'active': True}
    if change == 'runtime': a.hass.data['power_sync']['one'] = {}
    if change == 'pairing': a.entry.data['powerwall_local_din'] = 'replacement'
    if change == 'stale': a.snapshot.config_observed_monotonic -= 16
    if change == 'battery':
        a.snapshot.battery_w = 0
        a.snapshot.load_w += 10000
    if change == 'coordinator':
        a.runtime['powerwall_local']['coordinator'] = NS(**vars(a.runtime['powerwall_local']['coordinator']))
    lock.release()
    result = await task
    assert not result.submitted and not result.confirmed
    assert a.calls == []


@pytest.mark.asyncio
async def test_native_capacity_does_not_authorize_automated_monitoring(admission):
    a = qualify(admission)
    a.entry.options['monitoring_mode'] = True
    a.runtime['optimization_coordinator'] = NS(_enabled=True,
        _monitoring_mode_active=lambda: True)
    a.m._EV_SUBMISSION_CONTEXT.get()['params']['owner_mode'] = 'scheduled'
    with pytest.raises(a.m.SiteImportAdmissionDenied):
        await a.m.prepare_ev_submission(a.hass, a.state.entity_id, operation='rate', amps=20)
    assert not a.budget.managed


@pytest.mark.asyncio
@pytest.mark.parametrize('monitoring,enabled', [(False, True), (True, True), (False, False)])
async def test_actual_native_permission_gate_remains_separate_from_flexible_accounting(live_adapter, monitoring, enabled):
    a = qualify(live_adapter)
    a.entry.options['monitoring_mode'] = monitoring
    a.runtime['optimization_coordinator']._enabled = enabled
    a.runtime['optimization_coordinator']._monitoring_mode_active = lambda: monitoring
    observed = await a.ports.read(VIN)
    assert observed.native_flexible_battery_w == 10000
    assert a.ports.permitted(VIN, None, False) is (enabled and not monitoring)


@pytest.mark.asyncio
async def test_native_max_barrier_does_not_become_battery_flexibility(admission):
    a = qualify(admission)
    a.budget.armed_sites.clear()
    assert await a.budget.claim_native_max('battery', 'site', 'max-owner', NOW+60)
    with pytest.raises(a.m.SiteImportAdmissionDenied):
        await a.m.prepare_ev_submission(a.hass, a.state.entity_id, operation='rate', amps=20)
    assert not a.budget.managed


@pytest.mark.asyncio
async def test_managed_planning_allowance_requires_same_fresh_native_proof(admission):
    a = qualify(admission)
    assert a.m.managed_borrowable_w(a.hass, a.entry, VIN, {}) == 10000
    a.entry.options['optimization_max_grid_import_w'] = 10000
    assert a.m.managed_borrowable_w(a.hass, a.entry, VIN, {}) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('cached,failed', [(False, False), (True, False), (False, True)])
async def test_actual_snapshot_reader_never_renews_supplied_or_failed_config(cached, failed):
    from conftest import load_power_sync_function
    from pathlib import Path
    import logging
    root = Path(__file__).parents[1] / 'custom_components/power_sync'
    class Unreachable(Exception): pass
    class Signature(Exception): pass
    ns = {'asyncio': asyncio, 'time': NS(monotonic=lambda: 321.0),
        'is_loopback_host': lambda _: False, '_LOGGER': logging.getLogger(__name__),
        'PowerwallUnreachableError': Unreachable, 'PowerwallSignatureError': Signature,
        '_snapshot_from_dcq': lambda dcq, cfg: NS(soc=50, battery_w=-10000,
            grid_w=15000, load_w=5000, solar_w=0, raw={'config': cfg}), 'math': __import__('math')}
    fn = load_power_sync_function('_read_snapshot', ns, path=root/'powerwall_local/client.py')
    calls = []
    async def dcq(): return {'fresh': True}
    async def config(_):
        calls.append('signed_read')
        if failed: raise ValueError('configuration unavailable')
        return {'site_info': {'max_site_meter_power_ac': 15}}
    client = NS(_local_access_enabled=True, _host='192.0.2.1', _din='DIN',
                _fetch_dcq_local=dcq, _transport=NS(read_config=config))
    result = await fn(client, {'cached': True} if cached else None)
    assert result.observed_monotonic == 321
    assert result.config_observed_monotonic == (None if cached or failed else 321)
    assert calls == ([] if cached else ['signed_read'])


@pytest.mark.asyncio
@pytest.mark.parametrize('malformed', [-1, float('nan'), True, 10001])
async def test_budget_never_accepts_more_native_credit_than_measured(admission, malformed):
    a = qualify(admission)
    evidence = a.m._site_evidence(a.hass, a.entry, a.runtime, VIN, {})
    evidence['native_flexible_battery_w'] = malformed
    record = {'entry_id': 'one', 'site': 'site', 'identity': 'tesla_fleet:other:car:amps',
              'reserved_w': 4600, 'target_w': 4600, 'target_amps': 20, 'operation': 'rate'}
    assert await a.budget.admit_managed(VIN, record, evidence, lambda: True) is None
    assert not a.budget.managed


@pytest.mark.asyncio
async def test_unproven_core_observation_cannot_borrow_native_charge(live_adapter):
    a = qualify(live_adapter)
    observed = await a.ports.read(VIN)
    unproven = replace(observed, native_battery_proof=None)
    assert not a.m.restoration_fits(unproven, 32, NOW)
    assert a.m.decide_rate(unproven, 32, NOW)[0] <= 10


@pytest.mark.asyncio
@pytest.mark.parametrize('related', [False, True])
async def test_only_same_site_transition_removes_native_credit(admission, related):
    a = qualify(admission)
    entry = NS(entry_id='two', data={'tesla_energy_site_id': 'site' if related else 'other'}, options={})
    a.hass.data['power_sync']['two'] = {'entry': entry, 'native_rate_battery_transitions': 1}
    if related:
        with pytest.raises(a.m.SiteImportAdmissionDenied):
            await a.m.prepare_ev_submission(a.hass, a.state.entity_id, operation='rate', amps=20)
    else:
        assert await a.m.prepare_ev_submission(a.hass, a.state.entity_id, operation='rate', amps=20)


def test_snapshot_config_provenance_is_appended_and_defaults_unknown():
    import ast
    from dataclasses import dataclass
    from pathlib import Path
    from typing import Any
    root = Path(__file__).parents[1] / 'custom_components/power_sync'
    node = next(node for node in ast.parse((root/'powerwall_local/client.py').read_text()).body
                if isinstance(node, ast.ClassDef) and node.name == 'PowerwallSnapshot')
    ns = {'dataclass': dataclass, 'Any': Any, '__name__': __name__}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(root/'powerwall_local/client.py'), 'exec'), ns)
    snapshot = ns['PowerwallSnapshot'](50, 0, -10000, 15000, 5000,
        'SystemGridConnected', 'autonomous', 0, {})
    assert snapshot.config_observed_monotonic is None
    assert snapshot.grid_charging_enabled is None
