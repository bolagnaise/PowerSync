"""Native acquisitions, rather than equal HA projections, fence EV demand."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import importlib
import sys
from types import SimpleNamespace as NS

import pytest

from test_native_rate_admission import admission, NOW, VIN
from test_tesla_native_battery_flexibility import qualify, _native_session


def telemetry():
    return importlib.import_module('managed_admission_probe.native_rate_telemetry')


@pytest.mark.parametrize('field,value', [
    ('charge_current_request', True), ('charge_current_request', '10'),
    ('charge_current_request', 10.5), ('charge_current_request', -1),
    ('charge_current_request', 81), ('charge_current_request', float('nan')),
    ('timestamp', NOW - 16), ('timestamp', NOW + 1), ('timestamp', True),
    ('charger_actual_current', False), ('charger_actual_current', float('inf')),
    ('charger_power', '0'), ('charger_power', -1), ('charging_state', 'Unknown'),
])
def test_native_current_requires_typed_fresh_atomic_document(admission, field, value):
    a = admission
    a.charge[field] = value
    assert telemetry().native_charge_setting(a.hass, VIN, a.state.entity_id) is None


@pytest.mark.parametrize('change', ['vin', 'provider', 'device', 'runtime', 'coordinator', 'disabled', 'identifiers'])
def test_native_current_pin_rejects_source_replacement(admission, change):
    a = admission
    t = telemetry()
    captured = t.native_charge_setting(a.hass, VIN, a.state.entity_id)
    assert captured is not None
    registration = sys.modules['homeassistant.helpers.entity_registry'].async_get(a.hass).async_get(a.state.entity_id)
    vehicle = a.provider.runtime_data.vehicles[0]
    if change == 'vin': vehicle.vin = '5YJ3E1EA7KF000002'
    if change == 'provider': registration.config_entry_id = 'replacement'
    if change == 'device': vehicle.device = NS(id='car', identifiers={('tesla_fleet', VIN)})
    if change == 'runtime': a.provider.runtime_data = NS(vehicles=[vehicle])
    if change == 'coordinator': vehicle.coordinator = NS(last_update_success=True, data={'charge_state': a.charge})
    if change == 'disabled': registration.disabled_by = 'user'
    if change == 'identifiers': vehicle.device.identifiers = {('tesla_fleet', '5YJ3E1EA7KF000002')}
    assert not t.native_charge_setting_current(a.hass, VIN, a.state.entity_id, captured)


@pytest.mark.parametrize('domain', ['tesla_fleet', 'teslemetry'])
def test_native_current_accepts_exact_provider_in_composite_ha_device_identifiers(admission, domain):
    a = admission
    for registration in sys.modules['homeassistant.helpers.entity_registry'].async_get(a.hass).entities.values():
        registration.platform = domain
    a.provider.runtime_data.vehicles[0].device.identifiers = {
        ('tesla_fleet', VIN), ('teslemetry', VIN), ('esphome', 'bridge'),
        ('unrelated', 'identifier', 'extra')}
    a.hass.config_entries.async_entries = lambda requested: [a.provider] if requested == domain else []
    assert telemetry().native_charge_setting(a.hass, VIN, a.state.entity_id)['amps'] == 10


@pytest.mark.asyncio
async def test_native_current_identity_cannot_rebind_during_budget_persistence(admission):
    a = admission
    device = a.provider.runtime_data.vehicles[0].device
    a.store.hook = lambda: setattr(device, 'identifiers', {('tesla_fleet', '5YJ3E1EA7KF000002')})
    with pytest.raises(a.m.SiteImportAdmissionDenied):
        await a.m.prepare_ev_submission(a.hass, 'switch.car_charge', operation='start')
    assert not a.budget.managed


@pytest.mark.asyncio
@pytest.mark.parametrize('domain', ['tesla_fleet', 'teslemetry'])
async def test_explicit_read_reuses_qualified_cloud_reader_without_publishing_cache(admission, monkeypatch, domain):
    a = admission
    t = telemetry()
    for registration in sys.modules['homeassistant.helpers.entity_registry'].async_get(a.hass).entities.values():
        registration.platform = domain
    a.provider.runtime_data.vehicles[0].device.identifiers = {(domain, VIN)}
    a.hass.config_entries.async_entries = lambda requested: [a.provider] if requested == domain else []
    a.charge.update(charger_actual_current=0, charger_power=0, charging_state='Stopped')
    observation = t.native_charge_setting(a.hass, VIN, a.state.entity_id)['observation']
    a.charge['timestamp'] = NOW - 1200  # The real Teslemetry cache contract.
    module = importlib.import_module('managed_admission_probe.tesla_ev_observation')
    calls = []
    async def ordinary(*args, **kwargs):
        calls.append(('ordinary', kwargs)); return None
    async def independent(*args, **kwargs):
        assert kwargs['authorize']()
        calls.append(('independent', kwargs)); return observation
    monkeypatch.setattr(module, 'async_read_tesla_vehicle', ordinary)
    monkeypatch.setattr(module, 'async_read_independent_tesla_vehicle', independent)
    report = await t.async_refresh_native_charge_setting(
        a.hass, a.entry, VIN, a.state.entity_id, authorize=lambda: True)
    assert report['amps'] == 10 and report['at'] == NOW
    assert t.native_charge_setting_current(a.hass, VIN, a.state.entity_id, report)
    assert a.charge['timestamp'] == NOW - 1200
    assert [call[0] for call in calls] == ['ordinary', 'independent']
    assert calls[1][1]['preferred_entity_id'] == a.state.entity_id
    assert calls[1][1]['allow_paid_refresh'] is (domain == 'teslemetry')


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['policy', 'source', 'runtime', 'owner', 'deadline', 'identifiers'])
async def test_stale_native_read_refuses_fallback_after_scope_changes(admission, monkeypatch, change):
    a = admission
    t = telemetry()
    a.charge['timestamp'] = NOW - 1200
    module = importlib.import_module('managed_admission_probe.tesla_ev_observation')
    current = [True]
    clock = [NOW]
    monkeypatch.setattr(a.m.time, 'time', lambda: clock[0])
    calls = []
    async def ordinary(*args, **kwargs):
        calls.append('ordinary')
        if change == 'policy': a.entry.options['ev_provider'] = 'tesla_ble'
        if change == 'source': a.provider.runtime_data = NS(vehicles=[])
        if change == 'runtime': a.hass.data['power_sync']['one'] = dict(a.runtime)
        if change == 'owner': current[0] = False
        if change == 'deadline': clock[0] += 2
        if change == 'identifiers': a.provider.runtime_data.vehicles[0].device.identifiers = {('tesla_fleet', '5YJ3E1EA7KF000002')}
        return None
    async def independent(*args, **kwargs):
        calls.append('independent'); raise AssertionError('scope already revoked')
    monkeypatch.setattr(module, 'async_read_tesla_vehicle', ordinary)
    monkeypatch.setattr(module, 'async_read_independent_tesla_vehicle', independent)
    assert await t.async_refresh_native_charge_setting(
        a.hass, a.entry, VIN, a.state.entity_id, authorize=lambda: current[0], deadline=NOW+1) is None
    assert calls == ['ordinary']


@pytest.mark.asyncio
async def test_native_max_reconciliation_explicitly_refreshes_cached_cloud_twice(admission, monkeypatch):
    a = admission
    t = telemetry()
    now = [NOW]
    monkeypatch.setattr(a.m.time, 'time', lambda: now[0])
    a.budget.clock = lambda: now[0]
    a.charge.update(charger_actual_current=0, charger_power=0, charging_state='Stopped',
                    charge_current_request=15)
    observation = t.native_charge_setting(a.hass, VIN, a.state.entity_id)['observation']
    a.charge['timestamp'] = NOW-1200
    a.state.last_reported = datetime.fromtimestamp(NOW-300, timezone.utc)
    a.budget.armed_sites.clear()
    await a.budget.admit_managed(VIN, {
        'entry_id': 'one', 'site': 'site', 'identity': 'tesla_fleet:provider:car:amps',
        'entity_id': a.state.entity_id, 'setting_entity_id': a.state.entity_id,
        'target_amps': 5, 'target_w': 4500, 'reserved_w': 28800,
        'submitted_at': NOW-100, 'operation': 'rate', 'uncertain': True,
    }, {}, lambda: True)
    module = importlib.import_module('managed_admission_probe.tesla_ev_observation')
    reads = []
    async def ordinary(*args, **kwargs): return None
    async def independent(*args, **kwargs):
        assert kwargs['authorize']()
        reads.append(now[0])
        raw = dict(observation.raw_charge_state, timestamp=now[0])
        return replace(observation, source_charge_timestamp=now[0], raw_charge_state=raw,
                       origin='native_cloud')
    async def refresh_meter(): a.local.last_success_ts = now[0]
    async def settle(seconds): now[0] += seconds
    a.local.async_request_refresh = refresh_meter
    monkeypatch.setattr(module, 'async_read_tesla_vehicle', ordinary)
    monkeypatch.setattr(module, 'async_read_independent_tesla_vehicle', independent)
    monkeypatch.setattr(a.m.asyncio, 'sleep', settle)
    assert await a.m.reconcile_managed_for_native_max(a.hass, a.budget, 'site')
    assert reads == [NOW, NOW+3]
    assert VIN not in a.budget.managed
    assert a.charge['timestamp'] == NOW-1200
    assert a.state.last_reported.timestamp() == NOW-300


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['stale_source', 'future_source', 'wrong_vin', 'rebound', 'hazard', 'task', 'owner', 'meter'])
async def test_native_managed_reconciliation_retains_uncertain_demand_without_exact_settlement(admission, monkeypatch, change):
    a = admission
    now = [NOW]
    monkeypatch.setattr(a.m.time, 'time', lambda: now[0])
    a.budget.clock = lambda: now[0]
    a.budget.armed_sites.clear()
    a.charge.update(charger_actual_current=0, charger_power=0, charging_state='Stopped',
                    charge_current_request=15)
    await a.budget.admit_managed(VIN, {
        'entry_id': 'one', 'site': 'site', 'identity': 'tesla_fleet:provider:car:amps',
        'entity_id': a.state.entity_id, 'setting_entity_id': a.state.entity_id,
        'target_amps': 5, 'target_w': 4500, 'reserved_w': 28800,
        'submitted_at': NOW-100, 'operation': 'rate', 'uncertain': True,
    }, {}, lambda: True)
    await a.m.refresh_managed_records(a.hass, a.budget)
    now[0] += 4
    a.charge['timestamp'] = now[0]
    a.local.last_success_ts = now[0]
    if change == 'stale_source': a.charge['timestamp'] = NOW-16
    if change == 'future_source': a.charge['timestamp'] = now[0]+1
    if change == 'wrong_vin': a.provider.runtime_data.vehicles[0].coordinator.data['vin'] = '5YJ3E1EA7KF000002'
    if change == 'rebound': a.provider.runtime_data.vehicles[0].device.id = 'replacement'
    if change == 'hazard': a.runtime['_tesla_ev_recovery_sidecar_hold'] = 'unresolved'
    if change == 'task': a.budget.managed_tasks[VIN] = asyncio.current_task()
    if change == 'owner': a.runtime['ev_ownership'] = {VIN: {'owner': 'powersync', 'session_id': 'live'}}
    if change == 'meter': a.local.last_success_ts = NOW-16
    await a.m.refresh_managed_records(a.hass, a.budget)
    assert a.budget.managed[VIN]['reserved_w'] == 28800


async def ble_attempt(a, monkeypatch, *, requested=10):
    """Real durable BLE attempt, exact native fallback, and stale local setting."""
    registry = sys.modules['homeassistant.helpers.entity_registry'].async_get(a.hass)
    for entity in ('switch.bridge_charger', 'number.bridge_charging_amps',
                   'switch.bridge_ble_connection', 'binary_sensor.bridge_ble_status'):
        registry.entities[entity] = NS(entity_id=entity, unique_id=entity,
            device_id='bridge', platform='esphome', config_entry_id='ble', disabled_by=None)
    ble_state = NS(entity_id='number.bridge_charging_amps', state=str(requested), attributes={},
                   last_reported=datetime.fromtimestamp(NOW-300, timezone.utc))
    states = {a.state.entity_id: a.state, ble_state.entity_id: ble_state}
    a.hass.states.get = states.get
    a.entry.options.update(ev_provider='both', tesla_ble_entity_prefix='bridge',
                           tesla_ble_vehicle_mapping={VIN: 'bridge'})
    a.charge.update(charge_current_request=requested, charger_actual_current=0,
                    charger_power=0, charging_state='Stopped')
    recovery = importlib.import_module('managed_admission_probe.tesla_ev_recovery')
    ownership = importlib.import_module('managed_admission_probe.automations.ev_ownership')
    receipts = []
    async def save(*args, **kwargs):
        receipts.append(deepcopy(a.runtime.get('ev_transport_recovery'))); return True
    monkeypatch.setattr(ownership, 'persist_ev_runtime_state', save)
    ticket = await recovery.async_begin_attempt(a.hass, a.entry, VIN,
        {'kind': 'ble', 'prefix': 'bridge', 'entity_id': 'switch.bridge_charger',
         'fallback': {'kind': 'ha', 'entity_id': 'switch.car_charge'}},
        'start', 'switch.bridge_charger')
    assert ticket is not None and receipts
    a.m._EV_SUBMISSION_CONTEXT.get()['params']['_tesla_ble_native_observation_target'] = {
        'entity_id': 'switch.car_charge', 'identity': ticket.route['fallback']['entity_identity'],
        'attempt_id': ticket.attempt_id}
    return ticket, ble_state, registry


@pytest.mark.asyncio
@pytest.mark.parametrize('requested', [10, 15])
async def test_ble_first_start_under_native_max_uses_only_its_durable_native_fallback(admission, monkeypatch, requested):
    a = qualify(admission)
    a.budget.armed_sites.clear()
    assert await a.budget.claim_native_max('one', 'site', 'native-generation', NOW+60,
                                           direction='charge', ev_coexistence=True)
    a.runtime['powerwall_native_max_charge'] = _native_session(a)
    ticket, local, _registry = await ble_attempt(a, monkeypatch, requested=requested)
    assert await a.m.prepare_ev_submission(a.hass, 'switch.bridge_charger', operation='start')
    record = a.budget.managed[VIN]
    assert record['target_amps'] == requested
    assert record['identity'].startswith('esphome:ble:bridge:')
    assert record['entity_id'] == 'switch.bridge_charger'
    assert record['native_max_ev_coexistence'] is True
    assert record['target_w'] == requested * 230
    assert local.last_reported.timestamp() == NOW-300
    assert ticket.route['kind'] == 'ble'
    assert a.snapshot.grid_w == 15000 and a.snapshot.battery_w == -10000


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['attempt', 'provider', 'fallback', 'vin', 'pairing', 'hold'])
async def test_ble_stale_setting_cannot_borrow_another_native_producer(admission, monkeypatch, change):
    a = admission
    ticket, _local, registry = await ble_attempt(a, monkeypatch)
    marker = a.m._EV_SUBMISSION_CONTEXT.get()['params']['_tesla_ble_native_observation_target']
    if change == 'attempt': marker['attempt_id'] = 'different'
    if change == 'provider': a.entry.options['ev_provider'] = 'tesla_ble'
    if change == 'fallback': registry.entities['switch.car_charge'].config_entry_id = 'other'
    if change == 'vin': a.provider.runtime_data.vehicles[0].vin = '5YJ3E1EA7KF000002'
    if change == 'pairing': a.entry.options['tesla_ble_vehicle_mapping'] = {VIN: 'replacement'}
    if change == 'hold': a.runtime['_tesla_ev_recovery_sidecar_hold'] = 'held'
    assert not await a.m.prepare_ev_submission(a.hass, 'switch.bridge_charger', operation='start')
    assert not a.budget.managed


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['attempt', 'provider', 'fallback', 'runtime'])
async def test_ble_native_setting_scope_is_rechecked_after_budget_save(admission, monkeypatch, change):
    a = admission
    ticket, _local, registry = await ble_attempt(a, monkeypatch)
    marker = a.m._EV_SUBMISSION_CONTEXT.get()['params']['_tesla_ble_native_observation_target']
    def mutate():
        if change == 'attempt': marker['attempt_id'] = 'superseded'
        if change == 'provider': a.entry.options['ev_provider'] = 'tesla_ble'
        if change == 'fallback': registry.entities['switch.car_charge'].device_id = 'replacement'
        if change == 'runtime': a.hass.data['power_sync']['one'] = dict(a.runtime)
    a.store.hook = mutate
    with pytest.raises(a.m.SiteImportAdmissionDenied):
        await a.m.prepare_ev_submission(a.hass, 'switch.bridge_charger', operation='start')
    assert not a.budget.managed
