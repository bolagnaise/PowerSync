"""Expired Boost cleanup can observe native cloud without replaying a command."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from test_ev_boost_timers import BoostHarness, VIN_A, VIN_B
from test_tesla_ev_observation import (
    independent_source, observation, _charge,
    stub_native_vehicle_sources, bind_current_registry_helpers,
)


@pytest.fixture
def cloud_boost(independent_source, stub_native_vehicle_sources,
                bind_current_registry_helpers, monkeypatch):
    def make(domain='tesla_fleet'):
        native, native_entry, vehicle, coordinator = independent_source(domain)
        h = BoostHarness()
        h.entry.options['ev_provider'] = 'both'
        h.now = datetime.fromtimestamp(2_000_000_000, timezone.utc)
        h.hass.config_entries = native.config_entries
        h.hass.entity_registry = native.entity_registry
        h.hass.device_registry = native.device_registry
        vehicle.vin = VIN_A
        vehicle.device.identifiers = {(domain, VIN_A)}
        h.physical[VIN_A] = False
        h.fail_start.add(VIN_A)
        h.observation_overrides[VIN_A] = {'_charging_observed_at': h.now-timedelta(minutes=5)}
        monkeypatch.setattr(observation, 'time', SimpleNamespace(time=lambda: h.now.timestamp()))
        namespace = h.view._independent_boost_stopped.__func__.__globals__
        namespace['independent_tesla_source_binding'] = observation.independent_tesla_source_binding
        namespace['async_read_independent_tesla_vehicle'] = observation.async_read_independent_tesla_vehicle

        def fresh(state='Stopped', current=0, power=0, stamp=None):
            response = {'vin': VIN_A, **_charge(
                h.now.timestamp() if stamp is None else stamp,
                state=state, current=current, power=power)}
            coordinator.api.response = deepcopy(response)
            coordinator.api.refresh_response = {'response': deepcopy(response)}
        return SimpleNamespace(h=h, native=native_entry, vehicle=vehicle,
                               coordinator=coordinator, fresh=fresh)
    return make


@pytest.mark.asyncio
@pytest.mark.parametrize('domain', ['tesla_fleet', 'teslemetry'])
async def test_expired_failed_boost_retires_on_exact_fresh_cloud_stopped_without_stop(cloud_boost, domain):
    p = cloud_boost(domain)
    h = p.h
    response = await h.start(VIN_A, 3)
    assert response['success'] is False
    record = h.runtime['ev_boost_sessions'][VIN_A]
    assert record['unknown_start_outcome'] is True
    original_deadline = record['expires_at']
    h.now = datetime.fromisoformat(original_deadline)+timedelta(seconds=1)
    p.fresh()
    await h.due()
    assert not h.runtime['ev_boost_sessions'] and not h.runtime['ev_boost_timers']
    assert h.store.saved['ev_runtime_state']['boost_sessions'] == {}
    assert h.stops() == [] and h.owner(VIN_A) is None
    assert p.coordinator.api.wake_calls == []
    assert len(p.coordinator.api.refresh_calls) == int(domain == 'teslemetry')
    assert len(p.coordinator.api.calls) == int(domain == 'tesla_fleet')
    assert p.coordinator.data['charge_state']['timestamp'] == 2_000_000_000-10


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['old', 'deadline_equal', 'future', 'charging', 'starting', 'nonzero_current', 'nonzero_power', 'missing_power'])
async def test_native_cleanup_requires_fresh_settled_zero_current_and_power(cloud_boost, case):
    p = cloud_boost()
    h = p.h
    await h.start(VIN_A)
    record = h.runtime['ev_boost_sessions'][VIN_A]
    deadline = datetime.fromisoformat(record['expires_at'])
    h.now = deadline+timedelta(seconds=1)
    p.fresh(state='Charging' if case == 'charging' else 'Starting' if case == 'starting' else 'Stopped',
            current=1 if case == 'nonzero_current' else 0,
            power=1 if case == 'nonzero_power' else 0,
            stamp=(deadline.timestamp()-1 if case == 'old' else deadline.timestamp()
                   if case == 'deadline_equal' else h.now.timestamp()+1 if case == 'future' else None))
    if case == 'missing_power': p.coordinator.api.response['charge_state'].pop('charger_power')
    await h.due()
    assert h.runtime['ev_boost_sessions'][VIN_A] is record
    assert record['expires_at'] == deadline.isoformat()
    assert h.stops() == [] and h.owner(VIN_A) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['source', 'client', 'pairing', 'owner', 'runtime', 'generation', 'superseded', 'timer'])
async def test_native_cleanup_refuses_identity_or_owner_change_during_real_cloud_read(cloud_boost, change):
    p = cloud_boost()
    h = p.h
    await h.start(VIN_A)
    record = h.runtime['ev_boost_sessions'][VIN_A]
    h.now = datetime.fromisoformat(record['expires_at'])+timedelta(seconds=1)
    p.fresh()
    api = p.coordinator.api
    async def mutate():
        if change == 'source': p.vehicle.device.identifiers = {('tesla_fleet', VIN_B)}
        if change == 'client': p.coordinator.api = object()
        if change == 'pairing': h.entry.options['tesla_ble_entity_prefix'] = 'changed'
        if change == 'owner':
            h.runtime['ev_ownership'][VIN_A] = {'owner':'powersync', 'owner_mode':'manual', 'session_id':'new'}
        if change == 'runtime': h.hass.data['power_sync']['entry'] = dict(h.runtime)
        if change == 'generation': record['generation'] = 'new'
        if change == 'superseded': record['superseded'] = True
        if change == 'timer': h.runtime['ev_boost_timers'][VIN_A] = {'record':record}
    api.before_return = mutate
    await h.due()
    assert h.runtime['ev_boost_sessions'][VIN_A] is record
    assert h.stops() == []
    assert h.store.saved['ev_runtime_state']['boost_sessions'][VIN_A]['expires_at'] == record['expires_at']


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['ble_only', 'foreign_owner', 'superseded', 'in_flight', 'before_deadline'])
async def test_native_cleanup_does_not_read_without_eligible_expired_scope(cloud_boost, case):
    p = cloud_boost()
    h = p.h
    if case == 'ble_only': h.entry.options['ev_provider'] = 'tesla_ble'
    # Retain canonical identity while configuring BLE-only observation policy.
    await h.start(VIN_A)
    record = h.runtime['ev_boost_sessions'][VIN_A]
    if case == 'foreign_owner':
        h.runtime['ev_ownership'][VIN_A] = {'owner':'powersync', 'owner_mode':'manual', 'session_id':'other'}
    if case == 'superseded': record['superseded'] = True
    if case == 'in_flight': record['start_in_flight'] = True
    h.now = datetime.fromisoformat(record['expires_at'])+timedelta(seconds=1)
    p.fresh()
    if case == 'before_deadline':
        h.now -= timedelta(seconds=31)
        await h.runtime['ev_boost_timers'][VIN_A]['callback'](h.now)
    else:
        await h.due()
    assert p.coordinator.api.calls == p.coordinator.api.refresh_calls == []
    assert VIN_A in h.runtime['ev_boost_sessions']
    assert h.stops() == []


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['source', 'pairing', 'owner', 'generation'])
async def test_native_cleanup_retirement_rechecks_proof_after_store_await(cloud_boost, change):
    p = cloud_boost()
    h = p.h
    await h.start(VIN_A)
    record = h.runtime['ev_boost_sessions'][VIN_A]
    h.now = datetime.fromisoformat(record['expires_at'])+timedelta(seconds=1)
    p.fresh()
    entered, release = asyncio.Event(), asyncio.Event()
    h.store.pause, h.store.pause_after = (entered, release), h.store.saves+1
    task = asyncio.create_task(h.due())
    await asyncio.wait_for(entered.wait(), timeout=1)
    if change == 'source': p.vehicle.device.identifiers = {('tesla_fleet', VIN_B)}
    if change == 'pairing': h.entry.options['tesla_ble_entity_prefix'] = 'changed'
    if change == 'owner':
        h.runtime['ev_ownership'][VIN_A] = {'owner':'powersync', 'owner_mode':'manual', 'session_id':'other'}
    if change == 'generation': record['generation'] = 'replaced'
    release.set()
    await asyncio.wait_for(task, timeout=1)
    assert h.runtime['ev_boost_sessions'][VIN_A] is record
    assert h.store.saved['ev_runtime_state']['boost_sessions'][VIN_A]['expires_at'] == record['expires_at']
    assert h.stops() == []


@pytest.mark.asyncio
async def test_native_stopped_cleanup_keeps_second_vin_owner_timer_and_deadline(cloud_boost):
    p = cloud_boost()
    h = p.h
    second = await h.start(VIN_B, 3)
    second_timer = h.runtime['ev_boost_timers'][VIN_B]
    second_owner = h.owner(VIN_B)
    await h.start(VIN_A, 1)
    record = h.runtime['ev_boost_sessions'][VIN_A]
    h.now = datetime.fromisoformat(record['expires_at'])+timedelta(seconds=1)
    p.fresh()
    await h.due()
    assert set(h.runtime['ev_boost_sessions']) == {VIN_B}
    assert h.runtime['ev_boost_timers'][VIN_B] is second_timer
    assert h.owner(VIN_B) is second_owner
    assert h.runtime['ev_boost_sessions'][VIN_B]['expires_at'] == second['stops_at']
    assert h.physical[VIN_B] is True and h.stops() == []


@pytest.mark.asyncio
async def test_cancelled_independent_cleanup_read_keeps_original_obligation_and_retry(cloud_boost):
    p = cloud_boost()
    h = p.h
    await h.start(VIN_A)
    record = h.runtime['ev_boost_sessions'][VIN_A]
    h.now = datetime.fromisoformat(record['expires_at'])+timedelta(seconds=1)
    p.fresh()
    entered, release = asyncio.Event(), asyncio.Event()
    async def pause():
        entered.set()
        await release.wait()
    p.coordinator.api.before_return = pause
    task = asyncio.create_task(h.due())
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    release.set()
    await asyncio.sleep(0)
    assert h.runtime['ev_boost_sessions'][VIN_A] is record
    assert h.timers[-1].deadline == h.now+timedelta(seconds=30)
    assert h.runtime['ev_boost_timers'][VIN_A]['stops_at'].isoformat() == record['expires_at']
    assert h.stops() == []


@pytest.mark.asyncio
async def test_cancelled_retirement_checkpoint_keeps_durable_obligation_and_retry(cloud_boost):
    p = cloud_boost()
    h = p.h
    await h.start(VIN_A)
    record = h.runtime['ev_boost_sessions'][VIN_A]
    h.now = datetime.fromisoformat(record['expires_at'])+timedelta(seconds=1)
    p.fresh()
    entered, release = asyncio.Event(), asyncio.Event()
    h.store.pause, h.store.pause_after = (entered, release), h.store.saves+1
    task = asyncio.create_task(h.due())
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError): await task
    assert h.runtime['ev_boost_sessions'][VIN_A] is record
    assert h.store.saved['ev_runtime_state']['boost_sessions'][VIN_A]['expires_at'] == record['expires_at']
    assert h.timers[-1].deadline == h.now+timedelta(seconds=30)
    assert h.stops() == []


@pytest.mark.asyncio
async def test_changed_retirement_proof_cannot_remove_journal_when_followup_save_fails(cloud_boost):
    p = cloud_boost()
    h = p.h
    await h.start(VIN_A)
    record = h.runtime['ev_boost_sessions'][VIN_A]
    h.now = datetime.fromisoformat(record['expires_at'])+timedelta(seconds=1)
    p.fresh()
    entered, release = asyncio.Event(), asyncio.Event()
    h.store.pause, h.store.pause_after = (entered, release), h.store.saves+1
    task = asyncio.create_task(h.due())
    await asyncio.wait_for(entered.wait(), timeout=1)
    p.vehicle.device.identifiers = {('tesla_fleet', VIN_B)}
    h.store.fail_after = h.store.saves+1
    release.set()
    await asyncio.wait_for(task, timeout=1)
    assert h.runtime['ev_boost_sessions'][VIN_A] is record
    assert h.store.saved['ev_runtime_state']['boost_sessions'][VIN_A]['expires_at'] == record['expires_at']
    assert h.stops() == []
