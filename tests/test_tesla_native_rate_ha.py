"""Real HA adapter boundary: exact entity, current CAS, fresh report and gates."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1] / 'custom_components/power_sync'
NOW = 1800000000.0
VIN = '5YJ3E1EA7KF000001'


@pytest.fixture
def adapter(monkeypatch):
    package = 'native_rate_ha_probe'
    for name, path in [(package, ROOT), (package+'.optimization', ROOT/'optimization')]:
        module = ModuleType(name); module.__path__ = [str(path)]
        monkeypatch.setitem(sys.modules, name, module)
    m = importlib.import_module(package+'.tesla_native_rate')
    solar = importlib.import_module(package+'.teslemetry_solar')
    monkeypatch.setattr(m.time, 'time', lambda: NOW)
    gate = SimpleNamespace(lock=asyncio.Lock(), owner=None, blocked=lambda _: False)
    async def load(): pass
    gate.load = load
    async def ownership(*_): return gate
    monkeypatch.setattr(solar, 'ownership', ownership)
    stamp = lambda at: datetime.fromtimestamp(at, timezone.utc)
    state = SimpleNamespace(entity_id='number.car_charge_current', state='32',
        attributes={'min': 5, 'max': 32}, last_reported=stamp(NOW))
    registration = SimpleNamespace(platform='teslemetry', config_entry_id='provider', device_id='car', unique_id='amps')
    registry = SimpleNamespace(async_get=lambda _: registration)
    entity_registry = ModuleType('homeassistant.helpers.entity_registry')
    entity_registry.async_get = lambda _: registry
    ha = ModuleType('homeassistant'); ha.__path__ = []
    helpers = ModuleType('homeassistant.helpers'); helpers.__path__ = []
    ha.helpers = helpers
    monkeypatch.setitem(sys.modules, 'homeassistant', ha)
    monkeypatch.setitem(sys.modules, 'homeassistant.helpers', helpers)
    monkeypatch.setattr(helpers, 'entity_registry', entity_registry, raising=False)
    monkeypatch.setitem(sys.modules, entity_registry.__name__, entity_registry)
    calls = []
    update = [True]
    async def async_call(domain, service, data, **kwargs):
        calls.append((domain, service, data))
        if update[0]:
            state.state = str(data['value']); state.last_reported = stamp(NOW+0.1)
    hass = SimpleNamespace(data={}, states=SimpleNamespace(get=lambda _: state),
        services=SimpleNamespace(async_call=async_call))
    entry = SimpleNamespace(entry_id='one', data={}, options={})
    observed = SimpleNamespace(vin=VIN, entity_id=state.entity_id,
        identity=m._entity_identity(registration), current_amps=32)
    async def no_wait(_): pass
    monkeypatch.setattr(m.asyncio, 'sleep', no_wait)
    return SimpleNamespace(m=m, hass=hass, entry=entry, observed=observed, state=state,
        registration=registration, calls=calls, gate=gate, update=update, stamp=stamp)


@pytest.fixture
def live_adapter(adapter, monkeypatch):
    a = adapter
    package = a.m.__package__
    for suffix in ('automations', 'powerwall_local'):
        module = ModuleType(package+'.'+suffix); module.__path__ = [str(ROOT/suffix)]
        monkeypatch.setitem(sys.modules, module.__name__, module)
    utility = ModuleType('homeassistant.util'); dt = ModuleType('homeassistant.util.dt')
    dt.now = lambda: datetime.fromtimestamp(NOW, timezone.utc)
    utility.dt = dt
    monkeypatch.setitem(sys.modules, utility.__name__, utility)
    monkeypatch.setitem(sys.modules, dt.__name__, dt)
    a.entry.data['tesla_energy_site_id'] = 'site'
    a.hass.config = SimpleNamespace(latitude=-27.47, longitude=153.03)
    minute = dt.now().hour*60+dt.now().minute
    rule = dict(enabled=True, start_enabled=True, end_enabled=True,
        start_time=(minute-60)%1440, end_time=(minute+120)%1440,
        days_of_week=127, latitude=-27.47, longitude=153.03)
    data = {'charge_state': {'timestamp': NOW, 'charging_state': 'Charging',
            'charger_voltage': 230, 'charger_phases': 1, 'charger_actual_current': 10,
            'battery_level': 50, 'charge_limit_soc': 80},
            'drive_state': {'timestamp': NOW, 'latitude': -27.47, 'longitude': 153.03}}
    provider = SimpleNamespace(runtime_data=SimpleNamespace(vehicles=[SimpleNamespace(vin=VIN,
        coordinator=SimpleNamespace(last_update_success=True, data=data))]))
    a.hass.config_entries = SimpleNamespace(async_entries=lambda domain: [provider] if domain == 'tesla_fleet' else [])
    snapshot = SimpleNamespace(load_w=5300, solar_w=1000, battery_w=-5000, grid_w=9300,
        raw={'config': {'site_info': {'max_site_meter_power_ac': 15}}})
    reader = SimpleNamespace(cache={VIN: (a.stamp(NOW-20), {'charge_schedule_data': {'charge_schedules': [rule]}})})
    runtime = {'entry': a.entry, 'powerwall_local': {'coordinator': SimpleNamespace(data=snapshot,
        last_update_success=True, last_success_ts=NOW)}, 'optimization_coordinator': SimpleNamespace(
        _tesla_schedule_reader=reader, _get_current_action=lambda: SimpleNamespace(action='charge',power_w=2000))}
    a.hass.data['power_sync'] = {'one': runtime}
    budget_module = importlib.import_module(package+'.native_rate_budget')
    budget = budget_module.SharedRateBudget(None, clock=lambda: NOW); budget.loaded = True
    setting = SimpleNamespace(rate_vins={VIN}, rate_enabled=lambda _: True)
    ports = a.m.NativeRatePorts(a.hass, a.entry, setting, budget)
    ports.controller = SimpleNamespace(sessions={}, stopping=False)
    runtime[a.m.RUNTIME_KEY] = ports.controller
    async def entity(_): return a.state, a.observed.identity
    ports._entity = entity
    return SimpleNamespace(**vars(a), ports=ports, data=data, rule=rule, snapshot=snapshot,
                           runtime=runtime, budget=budget)


@pytest.mark.asyncio
async def test_exact_current_only_write_requires_fresh_report(adapter):
    a = adapter
    assert await a.m._write_current(a.hass, a.entry, VIN, a.observed, 10, lambda: True)
    assert a.calls == [('number','set_value',{'entity_id':'number.car_charge_current','value':10})]


@pytest.mark.asyncio
@pytest.mark.parametrize('state,stopped', [('Starting',False), ('Unknown',False),
    (None,False), ('Stopped',True), ('Complete',True), ('Disconnected',True)])
async def test_cleanup_distinguishes_confirmed_stop_from_unknown_vehicle_state(live_adapter, state, stopped):
    a = live_adapter
    a.ports.controller.sessions[VIN] = {'recovering': True}
    a.data['charge_state']['charging_state'] = state
    a.data['charge_state']['charger_actual_current'] = 0
    observed = await a.ports.read(VIN)
    assert observed is not None
    assert observed.confirmed_stopped is stopped
    assert observed.charging is False


@pytest.mark.asyncio
@pytest.mark.parametrize('actual', [None, 'unknown', float('nan'), True, -1])
async def test_cleanup_missing_physical_current_cannot_be_interpreted_as_zero(live_adapter, actual):
    a = live_adapter
    a.ports.controller.sessions[VIN] = {'recovering': True}
    a.data['charge_state']['charging_state'] = 'Stopped'
    a.data['charge_state']['charger_actual_current'] = actual
    assert await a.ports.read(VIN) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('actual', [0,20])
@pytest.mark.parametrize('metadata', [{'charger_phases':None}, {'charger_voltage':0}])
async def test_cleanup_unknown_power_unit_never_turns_positive_current_into_zero(live_adapter, actual, metadata):
    a = live_adapter
    a.ports.controller.sessions[VIN] = {'recovering': True}
    a.data['charge_state'].update(charging_state='Stopped', charger_actual_current=actual, **metadata)
    observed = await a.ports.read(VIN)
    if actual:
        assert observed is None
    else:
        assert observed.confirmed_stopped and observed.measured_ev_amps == 0
        assert observed.measured_ev_w == 0


@pytest.mark.asyncio
async def test_actual_cleanup_read_reconstructs_whole_site_budget(live_adapter):
    a = live_adapter
    a.state.state = '10'
    a.ports.controller.sessions[VIN] = {'recovering': True}
    a.runtime['powerwall_battery_dispatch'] = SimpleNamespace(charge_generation=None, _last_watts=-6000)
    observed = await a.ports.read(VIN)
    assert observed.non_ev_net_w == 2000  # home5300 - measuredEV2300 - PV1000
    assert observed.planned_battery_w == 6000  # measured5k and accepted6k beat LP2k
    assert observed.import_limit_w == 15000
    assert not a.m.restoration_fits(observed, 32, NOW)
    a.snapshot.load_w = 7300
    assert (await a.ports.read(VIN)).non_ev_net_w == 4000


@pytest.mark.asyncio
async def test_new_provider_schedule_disable_during_lock_wait_invalidates_cached_window(live_adapter):
    a = live_adapter
    observed = await a.ports.read(VIN)
    assert observed.window_id
    a.ports.controller.sessions[VIN] = {'baseline_amps': 32, 'window_id': observed.window_id}
    @asynccontextmanager
    async def lock(_):
        a.data['charge_schedule_data'] = {'timestamp_seconds': NOW,
            'charge_schedules': [{**a.rule, 'enabled': False}]}
        yield
    a.hass.data['power_sync_license_manager'] = SimpleNamespace(control_lock=lock)
    result = await a.ports.write(observed, 10, lambda: True)
    assert not result.submitted
    assert a.calls == []
    assert (await a.ports.read(VIN)).window_id is None


@pytest.mark.asyncio
async def test_native_waits_for_licence_before_acquiring_solar_interlock(adapter):
    a = adapter
    runtime = {}
    a.hass.data['power_sync'] = {'one':runtime}
    ports = a.m.NativeRatePorts(a.hass, a.entry, SimpleNamespace())
    ports.controller = SimpleNamespace(sessions={VIN:{'baseline_amps':32}}, stopping=False)
    runtime[a.m.RUNTIME_KEY] = ports.controller
    observed = a.m.RateObservation(VIN, a.observed.entity_id, a.observed.identity,
        32,5,32,230,True,True,NOW,NOW,'window',NOW-60,NOW+3600,NOW,
        2300,2000,3000,15000,True)
    async def read(_): return observed
    ports.read = read
    licence, waiting = asyncio.Lock(), asyncio.Event()
    @asynccontextmanager
    async def lock(_):
        waiting.set()
        async with licence: yield
    a.hass.data['power_sync_license_manager'] = SimpleNamespace(control_lock=lock)
    async with licence:
        task = asyncio.create_task(ports.write(observed,10,lambda: True))
        await asyncio.wait_for(waiting.wait(),1)
        assert not a.gate.lock.locked()
        async with a.gate.lock: pass  # Managed licence→Solar path can complete.
    assert (await asyncio.wait_for(task,1)).confirmed


@pytest.mark.asyncio
async def test_active_native_forecast_reverts_on_recovery_manual_current_or_new_schedule(live_adapter):
    a = live_adapter
    a.state.state = '5'
    a.data['charge_state']['battery_level'] = 77  # 2 kWh remains, two-hour window.
    observed = await a.ports.read(VIN)
    a.ports.controller.sessions[VIN] = {'baseline_amps':32, 'confirmed_amps':5,
        'window_id':observed.window_id, 'session':'ours'}
    a.ports.permitted = lambda *_: True
    schedules = importlib.import_module(a.m.__package__+'.optimization.tesla_schedules')
    base = schedules.TeslaScheduleForecast(VIN, (2000,0,0), 2)
    start = a.stamp(NOW)
    from datetime import timedelta
    timestamps = [start+timedelta(hours=i) for i in range(3)]
    actual = a.ports.forecast(VIN,base,timestamps,60,NOW)
    assert actual.power_w == (1150,850,0)
    a.ports.controller.sessions[VIN]['recovering'] = True
    assert a.ports.forecast(VIN,base,timestamps,60,NOW) is base
    a.ports.controller.sessions[VIN]['recovering'] = False
    a.state.state = '16'
    assert a.ports.forecast(VIN,base,timestamps,60,NOW) is base
    a.state.state = '5'
    a.data['charge_schedule_data'] = {'timestamp_seconds':NOW,
        'charge_schedules':[{**a.rule,'enabled':False}]}
    assert a.ports.forecast(VIN,base,timestamps,60,NOW) is base


@pytest.mark.asyncio
async def test_ack_without_readback_is_uncertain(adapter):
    a = adapter; a.update[0] = False
    assert not await a.m._write_current(a.hass, a.entry, VIN, a.observed, 10, lambda: True)
    assert len(a.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['stale','identity','manual','gate','range','authorization'])
async def test_provider_boundary_rejects_new_identity_manual_change_and_stale_state(adapter, change):
    a = adapter; allowed = True; amps = 10
    if change == 'stale': a.state.last_reported = a.stamp(NOW-91)
    if change == 'identity': a.registration.device_id = 'replacement'
    if change == 'manual': a.state.state = '16'
    if change == 'gate': a.gate.blocked = lambda _: True
    if change == 'range': amps = 0
    if change == 'authorization': allowed = False
    assert not await a.m._write_current(a.hass, a.entry, VIN, a.observed, amps, lambda: allowed)
    assert a.calls == []


@pytest.mark.asyncio
async def test_licence_revocation_while_waiting_for_command_lock_prevents_write(adapter):
    a = adapter; allowed = [True]
    @asynccontextmanager
    async def lock(_):
        allowed[0] = False
        yield
    a.hass.data['power_sync_license_manager'] = SimpleNamespace(control_lock=lock)
    assert not await a.m._write_current(a.hass, a.entry, VIN, a.observed, 10, lambda: allowed[0])
    assert a.calls == []


@pytest.mark.asyncio
async def test_unchanged_old_report_is_not_restore_confirmation(adapter):
    a = adapter; a.update[0] = False
    assert not await a.m._write_current(a.hass, a.entry, VIN, a.observed, 32, lambda: True, restore=True)
    assert len(a.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['stopped', 'left_home', 'window_ended', 'headroom_lost'])
async def test_rate_is_recalculated_from_fresh_observation_after_licence_wait(adapter, change):
    a = adapter
    runtime = {}
    a.hass.data['power_sync'] = {a.entry.entry_id: runtime}
    ports = a.m.NativeRatePorts(a.hass, a.entry, SimpleNamespace())
    ports.controller = SimpleNamespace(sessions={VIN: {'baseline_amps': 32}}, stopping=False)
    runtime[a.m.RUNTIME_KEY] = ports.controller
    observed = a.m.RateObservation(VIN, a.observed.entity_id, a.observed.identity,
        32, 5, 32, 230, True, True, NOW, NOW, 'native-window', NOW-60,
        NOW+3600, NOW, 2300, 2000, 3000, 15000, True)
    latest = [observed]
    async def read(_): return latest[0]
    ports.read = read
    @asynccontextmanager
    async def lock(_):
        changes = {'stopped': {'charging': False}, 'left_home': {'at_home': False},
                   'window_ended': {'window_end': NOW},
                   'headroom_lost': {'non_ev_net_w': 14000}}
        latest[0] = replace(observed, **changes[change])
        yield
    a.hass.data['power_sync_license_manager'] = SimpleNamespace(control_lock=lock)
    assert not await ports.write(observed, 10, lambda: True)
    assert a.calls == []


def test_replaced_runtime_cannot_inherit_old_controller_authority(adapter):
    a = adapter
    runtime = {}
    a.hass.data['power_sync'] = {a.entry.entry_id: runtime}
    ports = a.m.NativeRatePorts(a.hass, a.entry, SimpleNamespace())
    ports.controller = SimpleNamespace(sessions={VIN: {'session': 'old'}})
    runtime[a.m.RUNTIME_KEY] = ports.controller
    assert ports.current()
    a.hass.data['power_sync'][a.entry.entry_id] = {a.m.RUNTIME_KEY: ports.controller}
    assert not ports.current()
    assert not ports.permitted(VIN, 'old', True)
    assert not ports.permitted(VIN, 'old', False)


def test_other_entry_solar_hold_blocks_owned_rate_cleanup(adapter):
    a = adapter
    runtime = {}
    a.hass.data['power_sync'] = {'one':runtime}
    ports = a.m.NativeRatePorts(a.hass, a.entry, SimpleNamespace())
    ports.controller = SimpleNamespace(sessions={VIN:{'session':'old','pairing':ports.pairing}})
    runtime[a.m.RUNTIME_KEY] = ports.controller
    ports.superseded = lambda *_: False
    assert ports.permitted(VIN, 'old', True)
    a.hass.data['power_sync_native_solar'] = {'other':SimpleNamespace(blocked=lambda vin: vin == VIN)}
    assert not ports.permitted(VIN, 'old', True)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [None, 'no_physical_reduction', 'generation_replaced', 'reservation_changed'])
async def test_increase_lowers_battery_and_requires_two_fresh_physical_samples(adapter, failure):
    a = adapter
    a.state.state = '10'
    events = []
    budget = SimpleNamespace(revision=1, provider_tasks={}, admission_lock=lambda _: asyncio.Lock())
    runtime = {}
    a.hass.data['power_sync'] = {a.entry.entry_id: runtime}
    ports = a.m.NativeRatePorts(a.hass, a.entry, SimpleNamespace(), budget)
    ports.controller = SimpleNamespace(sessions={VIN: {'baseline_amps': 32}}, stopping=False)
    runtime[a.m.RUNTIME_KEY] = ports.controller
    battery = SimpleNamespace(charge_generation=7)
    async def refresh(generation):
        assert generation == 7
        events.append('lower_battery')
        return {'acknowledged_at': NOW-1}
    battery.refresh_current_generation = refresh
    runtime['powerwall_battery_dispatch'] = battery
    observed = a.m.RateObservation(VIN, a.observed.entity_id, a.observed.identity,
        10, 5, 32, 230, True, True, NOW, NOW-2, 'native-window', NOW-60,
        NOW+3600, NOW, 4600, 3000, 9700, 15000, True,
        borrowable_battery_w=9700, measured_ev_w=2300, measured_ev_at=NOW)
    samples = [NOW-0.8, NOW-0.6, NOW-0.4]
    async def read(_):
        events.append('physical_sample')
        if failure == 'generation_replaced': battery.charge_generation = 8
        if failure == 'reservation_changed': budget.revision = 2
        return replace(observed, meter_at=samples.pop(0) if len(samples)>1 else samples[0],
                       planned_battery_w=9700 if failure == 'no_physical_reduction' else 7400)
    ports.read = read
    original = a.hass.services.async_call
    async def write(*args, **kwargs):
        events.append('ev_current')
        return await original(*args, **kwargs)
    a.hass.services.async_call = write
    result = await ports.write(observed, 20, lambda: True)
    assert result.confirmed is (failure is None)
    assert result.submitted is (failure is None)
    if failure is None:
        assert events[:3] == ['lower_battery', 'physical_sample', 'physical_sample']
        assert events.index('ev_current') > 2
        assert len(a.calls) == 1
    else:
        assert a.calls == []
