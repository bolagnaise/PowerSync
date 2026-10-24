"""Current-only leases: grid budget, stale data, takeover and durable cleanup."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

path = Path(__file__).parents[1] / 'custom_components/power_sync/optimization/tesla_native_rate.py'
spec = importlib.util.spec_from_file_location('native_rate_probe', path)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)
schedule_spec = importlib.util.spec_from_file_location('native_rate_schedule_probe', path.parent/'tesla_schedules.py')
schedules = importlib.util.module_from_spec(schedule_spec)
sys.modules[schedule_spec.name] = schedules
schedule_spec.loader.exec_module(schedules)
NOW = 1800000000.0
VIN = '5YJ3E1EA7KF000001'


def observation(**changes):
    return replace(m.RateObservation(VIN, 'number.car_charge_current', 'provider:car',
        32, 5, 32, 230, True, True, NOW, NOW, 'window', NOW-60, NOW+3600,
        NOW, 2300, 2000, 3000, 15000, True), **changes)


def test_governed_load_forecast_uses_same_integer_current_and_remaining_energy_once():
    start = datetime.fromtimestamp(NOW, timezone.utc)
    timestamps = [start+timedelta(minutes=30*i) for i in range(3)]
    base = schedules.TeslaScheduleForecast(VIN, (4600,0,0), 2.3)
    actual = m.governed_forecast(base, observation(), 32, timestamps, 30, NOW)
    assert actual.power_w == (2300,2300,0)
    assert sum(actual.power_w)*0.5 == 2300
    assert actual.energy_needed_kwh == 2.3
    for changes in ({'vehicle_at':NOW-100}, {'at_home':False}, {'window_id':None}):
        assert m.governed_forecast(base, observation(**changes), 32, timestamps, 30, NOW) is base


def test_limited_native_forecast_keeps_deadline_shortfall_visible():
    start = datetime.fromtimestamp(NOW, timezone.utc)
    timestamps = [start, start+timedelta(minutes=30)]
    observed = observation(remaining_grid_wh=10000, import_limit_w=10000)
    base = schedules.TeslaScheduleForecast(VIN, (7360,7360), 10)
    actual = m.governed_forecast(base, observed, 32, timestamps, 30, NOW)
    target, reason = m.decide_rate(observed, 32, NOW)
    assert target == 21 and reason == 'limited'
    assert actual.power_w == (4830,4830)
    assert sum(actual.power_w)*0.5 < actual.energy_needed_kwh*1000


class Store:
    def __init__(self): self.data = None; self.fail = False
    async def async_load(self): return deepcopy(self.data)
    async def async_save(self, data):
        if self.fail: raise OSError('disk unavailable')
        self.data = deepcopy(data)


class Ports:
    def __init__(self):
        self.observed = observation(); self.owner = None; self.writes = []
        self.confirm = True; self.allowed = True; self.before_write = None
    async def read(self, vin): return self.observed
    def enabled_vins(self): return [VIN]
    def permitted(self, vin, session, restore):
        return self.allowed and self.owner in (None, session)
    def superseded(self, vin, session): return self.owner not in (None, session)
    def claim(self, vin, session):
        if self.owner is not None: return False
        self.owner = session; return True
    def release(self, vin, session):
        if self.owner == session: self.owner = None
    async def write(self, observed, amps, authorize):
        if self.before_write: self.before_write()
        if not authorize(): return False
        self.writes.append(amps)
        # Submission can affect hardware even when confirmation is lost.
        self.observed = replace(self.observed, current_amps=amps)
        return self.confirm


def controller():
    store, ports = Store(), Ports()
    return m.NativeRateController(store, ports, clock=lambda: NOW), store, ports


def test_rate_accounts_for_home_battery_and_remaining_deadline():
    assert m.decide_rate(observation(), 32, NOW) == (10, 'ready')
    assert m.decide_rate(observation(remaining_grid_wh=10000, import_limit_w=10000), 32, NOW) == (21, 'limited')
    assert m.decide_rate(observation(remaining_grid_wh=10000, non_ev_net_w=8000, import_limit_w=10000), 32, NOW) == (None, 'minimum_current_does_not_fit')
    assert m.decide_rate(observation(import_limit_w=0), 32, NOW)[0] is None


@pytest.mark.parametrize('changes', [dict(enabled=False), dict(charging=False), dict(at_home=False),
    dict(vehicle_at=NOW-91), dict(meter_at=NOW-16), dict(window_id=None),
    dict(window_end=NOW), dict(window_start=NOW+1), dict(schedule_at=NOW-901),
    dict(watts_per_amp=float('nan')), dict(import_limit_w=None), dict(remaining_grid_wh=0)])
@pytest.mark.asyncio
async def test_ineligible_observations_never_claim_or_write(changes):
    c, store, ports = controller(); ports.observed = observation(**changes)
    await c.tick()
    assert ports.writes == [] and ports.owner is None and not c.sessions


@pytest.mark.asyncio
async def test_baseline_is_durable_before_write_and_stop_restores():
    c, store, ports = controller()
    ports.before_write = lambda: (_ for _ in ()).throw(AssertionError()) if not store.data['sessions'][VIN]['uncertain'] else None
    await c.tick()
    assert ports.writes == [10] and c.status[VIN]['state'] == 'ready'
    ports.before_write = None
    await c.stop()
    assert ports.writes == [10, 32] and not c.sessions and ports.owner is None
    assert store.data['suppressed'][VIN] == 'window'


@pytest.mark.asyncio
async def test_persistence_failure_prevents_first_command():
    c, store, ports = controller(); store.fail = True
    await c.tick()
    assert not ports.writes and ports.owner is None


@pytest.mark.asyncio
@pytest.mark.parametrize('manual_amps', [16, 32])
async def test_manual_current_and_new_owner_win_without_reacquisition(manual_amps):
    c, store, ports = controller(); await c.tick()
    ports.observed = replace(ports.observed, current_amps=manual_amps)
    await c.tick(); await c.tick()
    assert ports.writes == [10] and c.status[VIN]['state'] == 'window_released'
    assert not c.sessions


@pytest.mark.asyncio
async def test_takeover_between_planning_and_final_write_blocks_command():
    c, store, ports = controller()
    ports.before_write = lambda: setattr(ports, 'owner', 'manual')
    await c.tick(); await c.tick()
    assert ports.writes == [] and ports.owner == 'manual' and not c.sessions


@pytest.mark.asyncio
async def test_stale_or_missing_reading_changes_session_to_cleanup_only():
    c, store, ports = controller(); await c.tick()
    ports.observed = None; await c.tick()
    assert c.sessions[VIN]['recovering'] is True
    ports.observed = observation(current_amps=10)
    await c.tick(); await c.tick()
    assert ports.writes == [10, 32] and not c.sessions


@pytest.mark.asyncio
async def test_restart_restores_baseline_and_never_replays_rate():
    c, store, ports = controller(); await c.tick()
    resumed = m.NativeRateController(store, ports, clock=lambda: NOW)
    await resumed.tick(); await resumed.tick()
    assert ports.writes == [10, 32] and not resumed.sessions


@pytest.mark.asyncio
async def test_uncertain_write_keeps_cleanup_and_bounded_attempt_budget_on_restart():
    c, store, ports = controller(); ports.confirm = False
    await c.tick()
    for _ in range(5): await c.tick()
    assert ports.writes == [10, 32, 32, 32]
    assert c.sessions[VIN]['restore_attempts'] == 3
    resumed = m.NativeRateController(store, ports, clock=lambda: NOW)
    await resumed.tick()
    assert len(ports.writes) == 4 and VIN in resumed.sessions


@pytest.mark.asyncio
async def test_entity_identity_change_cannot_restore_another_vehicle():
    c, store, ports = controller(); await c.tick()
    ports.observed = replace(ports.observed, identity='replacement-device')
    await c.tick()
    assert ports.writes == [10] and c.sessions[VIN]['recovering']


@pytest.mark.asyncio
async def test_insufficient_headroom_does_not_increase_current_to_saved_baseline():
    c, store, ports = controller(); await c.tick()
    ports.observed = replace(ports.observed, import_limit_w=3000)
    await c.tick()
    assert ports.writes == [10] and VIN in c.sessions


@pytest.mark.asyncio
async def test_stop_defers_restoration_instead_of_violating_hard_import_limit():
    c, store, ports = controller(); await c.tick()
    ports.observed = replace(ports.observed, import_limit_w=3000)
    await c.stop()
    assert ports.writes == [10] and VIN in c.sessions
    assert c.status[VIN]['reason'] == 'import_headroom_unavailable'
    ports.observed = replace(ports.observed, import_limit_w=15000)
    await c.tick()
    assert ports.writes == [10,32] and not c.sessions


@pytest.mark.asyncio
async def test_cancelled_write_is_cleanup_only_and_cannot_be_overtaken():
    c, store, ports = controller(); entered = asyncio.Event(); finish = asyncio.Event()
    write = ports.write
    async def delayed(*args):
        entered.set(); await finish.wait(); return await write(*args)
    ports.write = delayed
    running = asyncio.create_task(c.tick()); await entered.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError): await running
    assert c.sessions[VIN]['recovering'] and store.data['sessions'][VIN]['uncertain']
    await c.stop()
    assert ports.writes == [] and c.status[VIN]['reason'] == 'command_in_flight'
    finish.set(); await asyncio.gather(*list(c.inflight.values()))
    await c.tick()
    assert ports.writes == [10,32] and not c.sessions


@pytest.mark.asyncio
async def test_two_cars_are_isolated_and_ticks_are_serialized():
    c, store, ports = controller()
    other = '5YJ3E1EA7KF000002'
    ports.enabled_vins = lambda: [VIN, other]
    async def read(vin): return ports.observed if vin == VIN else None
    ports.read = read
    await asyncio.gather(c.tick(), c.tick())
    assert ports.writes == [10] and list(c.sessions) == [VIN]


@pytest.mark.asyncio
async def test_pre_submission_denial_keeps_confirmed_setting_and_window_authority():
    c, store, ports = controller(); await c.tick()
    original = ports.write
    now = NOW+61
    c.clock = lambda: now
    ports.observed = replace(ports.observed, remaining_grid_wh=4600,
                             vehicle_at=now, meter_at=now)
    async def denied(*_): return m.RateWriteResult(False, False)
    ports.write = denied
    await c.tick()
    assert ports.writes == [10]
    assert not c.sessions[VIN]['uncertain'] and not c.sessions[VIN]['recovering']
    assert c.sessions[VIN]['attempted_amps'] == 10
    assert c.status[VIN]['state'] == 'waiting_for_site_headroom'
    ports.write = original
    await c.stop()
    assert ports.writes == [10, 32] and not c.sessions


@pytest.mark.asyncio
async def test_restore_denied_before_submission_does_not_spend_hardware_retry_budget():
    c, store, ports = controller(); await c.tick()
    async def denied(*_): return m.RateWriteResult(False, False)
    ports.write = denied
    await c.stop()
    assert c.sessions[VIN]['restore_attempts'] == 0
    assert c.status[VIN]['state'] == 'restore_pending'
    assert c.status[VIN]['reason'] == 'waiting_for_site_headroom'
    assert ports.writes == [10]


@pytest.mark.asyncio
async def test_battery_handoff_waits_without_abandoning_owned_native_window():
    c, store, ports = controller(); await c.tick()
    session = deepcopy(c.sessions[VIN])
    ports.waiting_reason = lambda: 'battery_control_changing'
    await c.tick()
    assert c.sessions[VIN] == session and ports.writes == [10]
    assert c.status[VIN]['state'] == 'waiting_for_site_headroom'
    ports.waiting_reason = lambda: None
    await c.tick()
    assert not c.sessions[VIN]['recovering']
