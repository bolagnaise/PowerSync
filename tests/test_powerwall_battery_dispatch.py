"""Local power dispatch: export semantics, lease failure and ownership recovery."""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('pw_dispatch_under_test', ROOT / 'custom_components/power_sync/powerwall_local/battery_dispatch.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def snapshot(**kw):
    return SimpleNamespace(**dict(dict(soc=60, grid_status='SystemGridConnected', load_w=1000, solar_w=200,
                                     battery_w=0, grid_w=800, backup_reserve_percent=20, grid_charging_enabled=True,
                                     raw={'config': {'site_info': {'max_site_meter_power_ac': 1e9, 'min_site_meter_power_ac': -1e9}}}), **kw))


def config(**kw):
    return dict(dict(optimization_backup_reserve=.2, optimization_max_discharge_w=5000,
                     optimization_max_charge_w=4000, optimization_max_grid_import_w=6000), **kw)


def test_export_target_accounts_for_home_and_solar():
    assert module.battery_watts('discharge', 2000, snapshot(), config()) == 2800
    assert module.battery_watts('discharge', 10000, snapshot(), config()) == 5000
    assert module.battery_watts('discharge', 1000, snapshot(solar_w=5000), config()) == 0


def test_charging_respects_rate_and_site_import_headroom():
    assert module.battery_watts('charge', 2000, snapshot(), config()) == -2000
    assert module.battery_watts('charge', 10000, snapshot(load_w=5500), config()) == -700


def site_snapshot(import_kw=14.9, export_kw=-10.1, **kw):
    return snapshot(raw={'config': {'site_info': {'max_site_meter_power_ac': import_kw,
                                                 'min_site_meter_power_ac': export_kw}}}, **kw)


def test_gateway_site_limits_use_kw_and_refresh_in_both_directions():
    cfg = config(optimization_max_discharge_w=21000, optimization_max_charge_w=21000,
                 optimization_max_grid_import_w=None)
    assert module.site_grid_limits_w(site_snapshot()) == (14900, 10100)
    assert module.battery_watts('discharge', 20000, site_snapshot(), cfg) == 10900
    assert module.battery_watts('discharge', 20000, site_snapshot(export_kw=-12), cfg) == 12800
    assert module.battery_watts('discharge', 20000, site_snapshot(export_kw=-2), cfg) == 2800
    assert module.battery_watts('charge', 20000, site_snapshot(import_kw=4), cfg) == -3200
    assert module.battery_watts('charge', 20000, site_snapshot(import_kw=2), cfg) == -1200
    assert module.battery_watts('discharge', 20000, site_snapshot(export_kw=-1e9), cfg) == 20800
    assert module.battery_watts('charge', 20000, site_snapshot(import_kw=1e9), cfg) == -20000
    with pytest.raises(ValueError, match='blocks force discharge'):
        module.battery_watts('discharge', 1000, site_snapshot(export_kw=0), cfg)
    with pytest.raises(ValueError, match='headroom'):
        module.battery_watts('charge', 1000, site_snapshot(import_kw=0), cfg)


@pytest.mark.parametrize('raw', [ {'config': None}, {'config': {'site_info': None}},
    {'config': {'site_info': {'min_site_meter_power_ac': 10}}},
    {'config': {'site_info': {'max_site_meter_power_ac': float('nan')}}} ])
def test_gateway_site_limit_errors_fail_closed(raw):
    with pytest.raises(ValueError):
        module.battery_watts('discharge', 1000, snapshot(raw=raw), config())


def test_manual_slider_bounds_prefer_site_limits_then_learned_battery_limit():
    cfg = config(optimization_max_charge_w=21000, optimization_max_discharge_w=21000,
                 optimization_max_grid_import_w=None)
    assert module.manual_power_limits_kw(site_snapshot(), cfg, 10000) == {
        'charge_input_max_kw': 14.1, 'grid_export_max_kw': 10.1}
    assert module.manual_power_limits_kw(site_snapshot(import_kw=4, export_kw=-2), cfg, 10000) == {
        'charge_input_max_kw': 3.2, 'grid_export_max_kw': 2}
    assert module.manual_power_limits_kw(site_snapshot(import_kw=16, export_kw=-12), cfg, 10000) == {
        'charge_input_max_kw': 15.2, 'grid_export_max_kw': 12}
    assert module.manual_power_limits_kw(site_snapshot(import_kw=1e9, export_kw=-1e9), cfg, 10000) == {
        'charge_input_max_kw': 21, 'grid_export_max_kw': 9.2}
    assert module.manual_power_limits_kw(site_snapshot(), {**cfg, 'optimization_max_discharge_w': 0})['grid_export_max_kw'] == 0


def test_force_power_number_does_not_keep_an_old_selection_above_new_limits():
    import ast
    import math
    import time
    source = (ROOT / 'custom_components/power_sync/number.py').read_text()
    tree = ast.parse(source)
    wanted = {'_fresh_powerwall_local_snapshot', 'ForcePowerNumber'}
    nodes = [n for n in tree.body if getattr(n, 'name', '') in wanted]
    class StripImports(ast.NodeTransformer):
        def visit_ImportFrom(self, node): return ast.copy_location(ast.Pass(), node)
    scope = dict(NumberEntity=object, NumberMode=SimpleNamespace(SLIDER='slider'),
                 UnitOfPower=SimpleNamespace(KILO_WATT='kW'), FORCE_POWER_STEP_KW=.05,
                 FORCE_POWER_FALLBACK_MAX_KW=50, CONF_POWERWALL_LOCAL_PAIRED='powerwall_local_paired',
                 DOMAIN='power_sync', TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS=90,
                 manual_power_limits_kw=module.manual_power_limits_kw, math=math, time=time)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[StripImports().visit(n) for n in nodes], type_ignores=[])),
                 str(ROOT / 'custom_components/power_sync/number.py'), 'exec', flags=__import__('__future__').annotations.compiler_flag), scope)
    coord = SimpleNamespace(data=site_snapshot(), last_success_monotonic=time.monotonic())
    client = SimpleNamespace(dispatch_discharge_limit_w=10000)
    hass = SimpleNamespace(data={'power_sync': {'test': {'powerwall_local': {'coordinator': coord, 'client': client}}}})
    entry = SimpleNamespace(entry_id='test', data={'powerwall_local_paired': True, **config(optimization_max_charge_w=21000,
                            optimization_max_discharge_w=21000, optimization_max_grid_import_w=None)}, options={})
    number = scope['ForcePowerNumber'](hass, entry)
    number._attr_native_value = 20
    assert number.native_max_value == 14.1
    assert number.native_value == 14.1
    coord.data = site_snapshot(import_kw=4, export_kw=-2)
    assert number.native_max_value == 3.2
    assert number.native_value == 3.2
    coord.data = site_snapshot(import_kw=16, export_kw=-12)
    assert number.native_max_value == 15.2
    assert number.extra_state_attributes['grid_export_max_kw'] == 12
    coord.last_success_monotonic -= 100
    assert number.native_max_value == 0
    assert number.native_value == 20
    # Execute the actual switch routing with that stale entity value, then
    # the real local-force boundary. An explicit target cannot become Max.
    switch_tree = ast.parse((ROOT / 'custom_components/power_sync/switch.py').read_text())
    helpers = [n for n in switch_tree.body if getattr(n, 'name', '') in ('_state_float', '_selected_force_power_w')]
    switch = next(n for n in switch_tree.body if getattr(n, 'name', '') == 'ForceDischargeSwitch')
    turn_on = next(n for n in switch.body if getattr(n, 'name', '') == 'async_turn_on')
    registry = SimpleNamespace(async_get_entity_id=lambda *args: 'number.home_force_power')
    hass.states = SimpleNamespace(get=lambda key: SimpleNamespace(state=str(number.native_value)))
    import logging
    switch_scope = dict(er=SimpleNamespace(async_get=lambda h: registry), DOMAIN='power_sync', _LOGGER=logging.getLogger(__name__),
                        _selected_duration=lambda *args: 15, _coerce_duration=lambda value, fallback: value,
                        CONF_FORCE_DISCHARGE_DURATION='force_discharge_duration')
    exec(compile(ast.Module(body=[*helpers, turn_on], type_ignores=[]), '<actual-force-switch>', 'exec',
                 flags=__import__('__future__').annotations.compiler_flag), switch_scope)
    async def run():
        ns, controller, client, saved = load_routing()
        client.get_snapshot.side_effect = RuntimeError('offline')
        async def service(domain, action, data, **kwargs):
            assert data['power_w'] == 20000
            await ns['_try_local_powerwall_force']('discharge', call(**data), 15, data['power_w'])
        hass.services = SimpleNamespace(async_call=AsyncMock(side_effect=service))
        with pytest.raises(RuntimeError, match='cannot enforce the selected kW'):
            await switch_scope['async_turn_on'](SimpleNamespace(hass=hass, _entry=entry, _duration_minutes=15))
        assert hass.services.async_call.await_count == 1
        client.dispatch_battery_power.assert_not_called()
        assert not controller.pending
    asyncio.run(run())


def test_mobile_local_status_reports_fresh_direction_limits_and_fails_closed():
    import ast
    import time
    path = ROOT / 'custom_components/power_sync/powerwall_local/views.py'
    fn = next(n for n in ast.parse(path.read_text()).body if getattr(n, 'name', '') == '_local_access_status')
    scope = dict(CONF_POWERWALL_LOCAL_PAIRED='powerwall_local_paired', time=time,
                 TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS=90, manual_power_limits_kw=module.manual_power_limits_kw,
                 _normalized_gateway_host=lambda entry: '192.168.1.10', local_battery_dispatch_configured=lambda data: True)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(path), 'exec', flags=__import__('__future__').annotations.compiler_flag), scope)
    entry = SimpleNamespace(data={'powerwall_local_paired': True, **config()}, options={})
    coord = SimpleNamespace(data=site_snapshot(import_kw=4, export_kw=-10.1), reachable=True,
                            last_success_monotonic=time.monotonic(), client=SimpleNamespace(dispatch_discharge_limit_w=10000))
    status = scope['_local_access_status'](entry, coord)
    assert status['charge_input_max_kw'] == 3.2
    assert status['grid_export_max_kw'] == 10.1
    coord.data = snapshot(raw={'config': None})
    assert scope['_local_access_status'](entry, coord)['grid_export_max_kw'] == 0
    coord.data = site_snapshot()
    coord.last_success_monotonic -= 100
    assert scope['_local_access_status'](entry, coord)['charge_input_max_kw'] == 0
    scope['local_battery_dispatch_configured'] = lambda data: False
    assert 'charge_input_max_kw' not in scope['_local_access_status'](entry, coord)


def test_live_renewal_cannot_erase_known_gateway_limits():
    async def run():
        c, client, _ = controller()
        client.get_snapshot.return_value = site_snapshot()
        await c.start('discharge', 1000, 120)
        client.get_snapshot.return_value = snapshot(raw={'config': {'site_info': {}}})
        with pytest.raises(ValueError, match='unavailable'):
            await c._write()
        assert client.dispatch_battery_power.await_count == 1
        client.get_snapshot.return_value = site_snapshot(export_kw=-1e9)
        await c._write()
        await c.stop()
    asyncio.run(run())


@pytest.mark.parametrize('mode,snap,cfg', [
    ('discharge', snapshot(soc=20), config()),
    ('discharge', snapshot(soc=60, backup_reserve_percent=65), config()),
    ('charge', snapshot(soc=95), config(optimization_grid_charge_soc_cap=.9)),
    ('charge', snapshot(), config(optimization_allow_grid_charge=False)),
    ('charge', snapshot(load_w=7000), config()),
    ('charge', snapshot(grid_status='Unknown'), config()),
    ('discharge', snapshot(grid_status='SystemIslandedActive'), config()),
    ('discharge', snapshot(soc=None), config()),
    ('discharge', snapshot(load_w=float('nan')), config()),
])
def test_ineligible_dispatch_fails_closed(mode, snap, cfg):
    with pytest.raises(ValueError):
        module.battery_watts(mode, 1000, snap, cfg)


def controller():
    client = SimpleNamespace(get_snapshot=AsyncMock(return_value=snapshot()),
                             dispatch_battery_power=AsyncMock(return_value=True),
                             resume_battery_operation=AsyncMock(return_value=True))
    events = []
    async def persist(): events.append('intent')
    async def stopped(): events.append('stopped')
    c = module.LocalBatteryDispatch(client, config, lambda mode: True, persist, stopped)
    return c, client, events


def test_persists_before_command_and_releases_after_stop():
    async def run():
        c, client, events = controller()
        async def write(watts, seconds):
            assert events == ['intent']
            assert watts == -500
            assert 1 <= seconds <= module.LEASE_SECONDS
            return True
        client.dispatch_battery_power.side_effect = write
        result = await c.start('charge', 500, 120)
        assert result['command_status'] == 'accepted'
        assert c.pending
        await c.stop()
        assert events == ['intent', 'stopped']
        assert not c.pending
        client.resume_battery_operation.assert_awaited_once()
    asyncio.run(run())


def test_uncertain_command_resumes_before_reporting_failure():
    async def run():
        c, client, events = controller()
        client.dispatch_battery_power.side_effect = TimeoutError()
        with pytest.raises(module.DispatchCommandFailed): await c.start('charge', 500, 120)
        assert events == ['intent', 'stopped']
        client.resume_battery_operation.assert_awaited_once()
        assert not c.pending
    asyncio.run(run())


def test_failed_resume_retains_intent_for_restart_and_retry():
    async def run():
        c, client, events = controller()
        await c.start('charge', 500, 120)
        client.resume_battery_operation.return_value = False
        with pytest.raises(ValueError, match='cleanup remains pending'): await c.stop()
        assert c.pending
        assert events == ['intent']
        client.resume_battery_operation.return_value = True
        await c.stop()
        assert not c.pending
        assert events == ['intent', 'stopped']
    asyncio.run(run())


def test_renewal_rechecks_monitoring_and_stops_without_new_power():
    async def run():
        c, client, events = controller()
        await c.start('charge', 500, 120)
        c.allowed = lambda mode: False
        with pytest.raises(ValueError, match='ownership'): await c._write()
        assert client.dispatch_battery_power.await_count == 1
        await c.stop()
    asyncio.run(run())


def test_guard_rechecked_before_every_export_write():
    async def run():
        c, client, _ = controller()
        async def guard(write, controlled_export=0): return await write(100)
        await c.start('discharge', 2000, 120, guarded_write=guard)
        assert client.dispatch_battery_power.await_args.args[0] == 900
        c.guarded_write = AsyncMock(return_value=False)
        with pytest.raises(ValueError, match='network export'): await c._write()
        assert client.dispatch_battery_power.await_count == 1
        await c.stop()
    asyncio.run(run())


def test_direction_switch_cancels_old_renewal_and_expiry():
    async def run():
        c, client, _ = controller()
        await c.start('charge', 500, 120)
        old = c._task
        await c.start('discharge', 500, 120)
        assert old.cancelled()
        assert client.dispatch_battery_power.await_args.args[0] == 1300
        await c.stop()
    asyncio.run(run())


def load_routing(monitored=False, paired=True, **options):
    """Execute the real setup-entry closures with an isolated HA boundary."""
    import ast
    import logging
    from datetime import datetime, timedelta, timezone
    source = (ROOT / 'custom_components/power_sync/__init__.py').read_text()
    tree = ast.parse(source)
    setup = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'async_setup_entry')
    names = {'_clear_local_powerwall_dispatch', '_stop_local_powerwall_dispatch', '_try_local_powerwall_force',
             'handle_force_charge', 'handle_force_discharge', 'persist_force_mode_state', 'restore_force_mode_from_persistence',
             '_local_powerwall_dispatch_config', '_local_powerwall_dispatch_acknowledged', '_local_powerwall_dispatch_failed', '_local_powerwall_generation_current'}
    nodes = [n for n in setup.body if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name in names]
    class StripImports(ast.NodeTransformer):
        def visit_ImportFrom(self, node): return ast.copy_location(ast.Pass(), node)
    nodes = [StripImports().visit(n) for n in nodes]
    state = lambda: dict(active=False, saved_tariff=None, saved_operation_mode=None, saved_backup_reserve=None,
                         saved_grid_charging_enabled=None, saved_export_rule=None, expires_at=None,
                         hardware_expires_at=None, power_w=0)
    charge, discharge = state(), state()
    runtime = {}
    hass = SimpleNamespace(data={'power_sync': {'entry': runtime}})
    data = dict(battery_system='tesla', powerwall_local_paired=paired, powerwall_local_ip='192.168.1.10',
                **config(), **options)
    entry = SimpleNamespace(entry_id='entry', data=data, options={})
    saved = {}
    async def save(value): saved.clear(); saved.update(value)
    store = SimpleNamespace(async_load=AsyncMock(side_effect=lambda: dict(saved)), async_save=AsyncMock(side_effect=save))
    now = SimpleNamespace(utcnow=lambda: datetime.now(timezone.utc))
    hs = importlib.util.spec_from_file_location('local_host_test', ROOT/'custom_components/power_sync/powerwall_host.py')
    hm = importlib.util.module_from_spec(hs); hs.loader.exec_module(hm)
    ns = dict(local_battery_dispatch_configured=hm.local_battery_dispatch_configured, asyncio=asyncio, Any=object, ServiceCall=object, HomeAssistantError=RuntimeError,
              DOMAIN='power_sync', hass=hass, entry=entry, _local_powerwall_runtime=runtime, DispatchCommandFailed=module.DispatchCommandFailed, validate_dispatch_config=module.validate_dispatch_config, _LOGGER=logging.getLogger(__name__),
              force_charge_state=charge, force_discharge_state=discharge,
              hold_soc_state={'active':False}, self_consumption_state={'active':False},
              _local_powerwall_route_lock=asyncio.Lock(), _command_generation=[0],
              _control_call_source=lambda call: call.data.get('source', 'user'),
              _monitoring_mode_should_block_control=lambda call: monitored and call.data.get('source') not in ('user','manual'),
              _demand_grid_charging_protection_active=lambda: False,
              _cancel_all_force_timers=lambda reason: None, async_dispatcher_send=lambda *args: None,
              active_battery_system='tesla', BATTERY_SYSTEM_TESLA='tesla', BATTERY_SYSTEM_SOLAREDGE='solaredge',
              CONF_POWERWALL_LOCAL_IP='powerwall_local_ip', CONF_POWERWALL_LOCAL_PAIRED='powerwall_local_paired',
              normalize_powerwall_gateway_host=lambda host: host, dt_util=now, timedelta=timedelta, datetime=datetime,
              _resolve_force_command_power_w=lambda mode, value: value, _coerce_force_power_w=int,
              DEFAULT_DISCHARGE_DURATION=30, DISCHARGE_DURATIONS=[15,30,60],
              store=store, _select_restorable_tesla_tariff=lambda value: value,
              tesla_grid_charging_preferences={}, persisted_force_state={}, persisted_pending_restore={})
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(ROOT/'custom_components/power_sync/__init__.py'), 'exec'), ns)
    c, client, _ = controller()
    c.config=ns['_local_powerwall_dispatch_config']
    c.acknowledged=ns['_local_powerwall_dispatch_acknowledged']
    c.failed=ns['_local_powerwall_dispatch_failed']
    c.persist=ns['persist_force_mode_state']
    c.stopped=ns['_clear_local_powerwall_dispatch']
    runtime['powerwall_battery_dispatch']=c
    ns['_get_local_powerwall_dispatch']=AsyncMock(return_value=c)
    return ns, c, client, saved


def call(**data):
    return SimpleNamespace(data=data, context=SimpleNamespace(user_id=None,parent_id=None))


def test_real_force_handlers_route_locally_and_restore_persisted_state():
    async def run():
        ns,c,client,saved=load_routing()
        result=await ns['handle_force_charge'](call(source='user',power_w=500,duration=15))
        assert result['control_path']=='powerwall_local_dispatch'
        assert saved['force_mode_state']['powerwall_local_dispatch']
        assert saved['force_mode_state']['mode']=='charge'
        result=await ns['handle_force_discharge'](call(source='user',power_w=1000,duration=15))
        assert result['battery_power_w']==1800
        assert saved['force_mode_state']['mode']=='discharge'
        assert not ns['force_charge_state']['active']
        assert await ns['_stop_local_powerwall_dispatch']()
        assert saved['force_mode_state'] is None
        assert not ns['force_discharge_state']['active']
    asyncio.run(run())


def test_real_optimizer_monitoring_gate_sends_no_battery_command():
    async def run():
        ns,c,client,saved=load_routing(monitored=True)
        await ns['handle_force_charge'](call(source='optimizer',power_w=500,duration=1))
        with pytest.raises(RuntimeError,match='monitoring mode'):
            await ns['handle_force_discharge'](call(source='optimizer',power_w=500,duration=1))
        client.dispatch_battery_power.assert_not_called()
        assert not saved
    asyncio.run(run())


def test_optimizer_dispatch_is_distinct_from_manual_commitment():
    async def run():
        ns,c,client,saved=load_routing()
        await ns['handle_force_discharge'](call(source='optimizer',power_w=1000,duration=1))
        assert ns['force_discharge_state']['source']=='optimizer'
        assert ns['force_discharge_state']['scope']=='optimizer'
        assert not ns['force_discharge_state']['commitment_eligible']
        assert ns['force_discharge_state']['command_status']=='accepted'
        await c.stop()
    asyncio.run(run())


def test_pending_local_intent_survives_optimizer_clear_before_restore():
    async def run():
        ns,c,client,saved=load_routing()
        await ns['handle_force_charge'](call(source='optimizer',power_w=500,duration=1))
        ns['force_charge_state'].update(active=False,expires_at=None)
        await ns['persist_force_mode_state']()
        assert saved['force_mode_state']['powerwall_local_dispatch']
        await c.stop()
    asyncio.run(run())


def test_restart_resumes_without_replaying_dispatch_or_tariff():
    async def run():
        ns,c,client,saved=load_routing()
        ns['persisted_force_state']={'mode':'charge','source':'optimizer','powerwall_local_dispatch':True}
        ns['hass'].data['power_sync']['entry']['powerwall_local_recovery_pending']=True
        await ns['restore_force_mode_from_persistence']()
        client.dispatch_battery_power.assert_not_called()
        client.resume_battery_operation.assert_awaited_once()
        assert saved['force_mode_state'] is None
        assert not ns['hass'].data['power_sync']['entry']['powerwall_local_recovery_pending']
    asyncio.run(run())


def test_active_legacy_force_is_not_adopted_into_local_control():
    async def run():
        ns,c,client,saved=load_routing()
        ns['force_charge_state'].update(active=True,saved_tariff={'baseline':True})
        assert await ns['_try_local_powerwall_force']('charge',call(source='user'),15,500) is None
        client.dispatch_battery_power.assert_not_called()
    asyncio.run(run())


def test_demand_protection_blocks_every_local_charge_command():
    async def run():
        ns,c,client,saved=load_routing()
        ns['_demand_grid_charging_protection_active']=lambda: True
        with pytest.raises(RuntimeError,match='safety policy'):
            await ns['handle_force_charge'](call(source='user',power_w=500,duration=15))
        client.dispatch_battery_power.assert_not_called()
        assert saved['force_mode_state'] is None
    asyncio.run(run())


def test_saved_percentage_reserve_cap_and_hardware_floor():
    assert module.battery_watts('discharge', 500, snapshot(soc=50), config(optimization_backup_reserve=20)) == 1300
    for changes in ({'hardware_backup_reserve':.8},{'hardware_backup_reserve':80}):
        with pytest.raises(ValueError,match='reserve floor'):
            module.battery_watts('discharge',500,snapshot(soc=50),config(**changes))
    with pytest.raises(ValueError,match='charge ceiling'):
        module.battery_watts('charge',500,snapshot(soc=50),config(optimization_grid_charge_soc_cap=40))
    assert module.battery_watts('charge',500,snapshot(),config(optimization_max_grid_import_w=0)) == -500
    assert module.battery_watts('discharge',5000,snapshot(),config(optimization_max_grid_export_w=1000)) == 1800
    with pytest.raises(ValueError):
        module.battery_watts('discharge',500,snapshot(),config(optimization_max_grid_export_w=0))


def test_data_reserve_precedence_matches_existing_optimizer():
    ns,c,_,_=load_routing()
    ns['entry'].data['optimization_backup_reserve']=.8
    ns['entry'].options['optimization_backup_reserve']=.2
    with pytest.raises(ValueError,match='reserve floor'):
        module.battery_watts('discharge',500,snapshot(soc=50),c.config())


def test_reload_during_client_acquisition_never_sends_old_command():
    async def run():
        ns,c,client,_=load_routing()
        entered, release=asyncio.Event(),asyncio.Event()
        async def delayed():
            entered.set();await release.wait();return c
        ns['_get_local_powerwall_dispatch']=delayed
        request=asyncio.create_task(ns['_try_local_powerwall_force']('charge',call(source='user'),15,500))
        await entered.wait()
        ns['_local_powerwall_runtime']['powerwall_local_dispatch_stopping']=True
        ns['hass'].data['power_sync']['entry']={'powerwall_local_dispatch_stopping':False}
        release.set()
        with pytest.raises(RuntimeError,match='generation'): await request
        client.dispatch_battery_power.assert_not_called()
    asyncio.run(run())


def test_old_setup_callbacks_cannot_clear_replacement_state():
    async def run():
        ns,c,client,saved=load_routing()
        ns['hass'].data['power_sync']['entry']={'marker':'new'}
        await ns['_clear_local_powerwall_dispatch']()
        await ns['_local_powerwall_dispatch_failed']()
        assert not saved
        with pytest.raises(ValueError,match='generation'):
            await ns['_local_powerwall_dispatch_acknowledged'](500,60)
    asyncio.run(run())


def test_local_command_fallback_requires_resume_and_fresh_authorization():
    async def run():
        ns,c,client,saved=load_routing()
        client.dispatch_battery_power.return_value=False
        result=await ns['_try_local_powerwall_force']('charge',call(source='user'),15,500)
        assert result=={'fallback':True,'power_w':500}
        client.resume_battery_operation.assert_awaited_once()
        assert not c.pending
        assert saved['force_mode_state'] is None
        ns,c,client,saved=load_routing()
        client.dispatch_battery_power.return_value=False
        client.resume_battery_operation.return_value=False
        with pytest.raises(RuntimeError,match='cleanup remains pending'):
            await ns['_try_local_powerwall_force']('charge',call(source='user'),15,500)
        assert c.pending
        assert saved['force_mode_state']['command_status']=='restore_pending'
        # No renewal task exists after a failed first request.
        assert c._task is None
        ns,c,client,saved=load_routing()
        client.dispatch_battery_power.return_value=False
        async def resume():
            ns['_demand_grid_charging_protection_active']=lambda: True
            return True
        client.resume_battery_operation.side_effect=resume
        with pytest.raises(RuntimeError,match='authorization changed'):
            await ns['_try_local_powerwall_force']('charge',call(source='user'),15,500)
    asyncio.run(run())


def test_precommand_local_read_failure_cannot_erase_unknown_site_limits():
    async def run():
        ns,c,client,saved=load_routing()
        client.get_snapshot.side_effect=TimeoutError()
        with pytest.raises(ValueError, match='fallback blocked by gateway site limits'):
            await ns['_try_local_powerwall_force']('charge',call(source='user'),15,500)
        client.dispatch_battery_power.assert_not_called()
        client.resume_battery_operation.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['charge', 'discharge'])
def test_known_site_cap_and_lost_lan_cannot_enter_max_tariff_fallback(mode):
    async def run():
        ns, c, client, saved = load_routing()
        client.get_snapshot.return_value = site_snapshot(import_kw=4, export_kw=-1)
        await ns['_try_local_powerwall_force'](mode, call(source='user', power_w=500), 15, 500)
        await ns['_stop_local_powerwall_dispatch']()
        client.get_snapshot.side_effect = TimeoutError('LAN lost')
        with pytest.raises(ValueError, match='fallback blocked by gateway site limits'):
            await ns['_try_local_powerwall_force'](mode, call(source='user'), 15, 500)
        client.get_snapshot.side_effect = None
        # Even after a new controller/reload, an actual configured cap denies
        # fallback. Only fresh explicit-unset evidence can permit Max tariff.
        with pytest.raises(ValueError, match='fallback blocked by gateway site limits'):
            await c.check_tariff_fallback(mode)
        fresh, fresh_client, _ = controller()
        fresh_client.get_snapshot.return_value = snapshot(raw={'config': {'site_info': {}}})
        with pytest.raises(ValueError, match='unavailable limit evidence'):
            await fresh.check_tariff_fallback(mode)
        fresh_client.get_snapshot.return_value = snapshot()
        await fresh.check_tariff_fallback(mode)
    asyncio.run(run())


@pytest.mark.parametrize('path', ['optimizer', 'manual'])
@pytest.mark.parametrize('change', ['stopping', 'generation', 'license'])
def test_delayed_site_limit_read_rechecks_fallback_authorization(path, change):
    async def run():
        ns, c, client, saved = load_routing()
        if path == 'optimizer':
            ns['_local_powerwall_runtime']['powerwall_dispatch_fallback_pending'] = True
        client.dispatch_battery_power.return_value = False
        count = 0
        async def read():
            nonlocal count
            count += 1
            if count == (1 if path == 'optimizer' else 2):
                if change == 'stopping':
                    ns['_local_powerwall_runtime']['powerwall_local_dispatch_stopping'] = True
                elif change == 'generation':
                    ns['_command_generation'][0] += 1
                else:
                    ns['hass'].data['power_sync_license_manager'] = object()
                    ns['service_allowed'] = lambda *args: False
            return snapshot()
        client.get_snapshot.side_effect = read
        with pytest.raises(RuntimeError, match='changed during limit verification'):
            await ns['_try_local_powerwall_force']('charge', call(source=path if path == 'optimizer' else 'user'), 15, 500)
        assert client.dispatch_battery_power.await_count == (0 if path == 'optimizer' else 1)
        assert not c.pending
    asyncio.run(run())


def test_real_network_guard_fault_releases_without_recursive_deadlock(monkeypatch):
    import sys
    from dataclasses import replace
    spec = importlib.util.spec_from_file_location('pw_real_network_guard', ROOT/'custom_components/power_sync/network_envelope.py')
    network=importlib.util.module_from_spec(spec);sys.modules[spec.name]=network;spec.loader.exec_module(network)
    monkeypatch.setattr(module,'RENEW_SECONDS',.01)
    async def run():
        c,client,events=controller()
        manager=SimpleNamespace(snapshot=network.NetworkExportEnvelope(mode='off'),
                                pcc_export_w=lambda:(None,None))
        async def fault(reason):
            manager.snapshot=replace(manager.snapshot,fault=reason,active_export_permitted=False)
        manager.async_set_fault=fault
        guard=network.ExportGuard(manager,stop_export=c.stop)
        async def guarded(write, controlled_export):
            return await guard.async_guard_write(500,write,current_controlled_export_w=controlled_export,stop_on_fault=False)
        await asyncio.wait_for(c.start('discharge',500,120,guarded_write=guarded),1)
        # Make the next real guard check encounter stale PCC while local owns
        # a lease. Its release must finish, rather than recursively lock stop.
        manager.snapshot=network.NetworkExportEnvelope(mode='active',active_export_permitted=True,effective_limit_w=5000)
        for _ in range(50):
            if not c.pending:break
            await asyncio.sleep(.01)
        assert not c.pending
        assert manager.snapshot.fault=='PCC telemetry is stale or unavailable'
        client.resume_battery_operation.assert_awaited_once()
        assert client.dispatch_battery_power.await_count==1
        await c.stop()
    asyncio.run(run())


def test_local_lease_expiry_resumes_automatically(monkeypatch):
    monkeypatch.setattr(module,'RENEW_SECONDS',.01)
    async def run():
        c,client,_=controller()
        await c.start('charge',500,120)
        c._deadline=module.time.monotonic()-.1
        for _ in range(30):
            if not c.pending:break
            await asyncio.sleep(.01)
        assert not c.pending
        client.resume_battery_operation.assert_awaited_once()
        assert client.dispatch_battery_power.await_count==1
    asyncio.run(run())


def test_local_target_capability_honors_lp_and_static_limits_without_changing_cloud():
    import ast
    tree=ast.parse((ROOT/'custom_components/power_sync/optimization/coordinator.py').read_text())
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='OptimizationCoordinator')
    names={'_supports_local_powerwall_target_power','_supports_target_export_power','_supports_target_charge_power','_export_command_power_w'}
    nodes=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in names]
    class StripImports(ast.NodeTransformer):
        def visit_ImportFrom(self,node): return ast.copy_location(ast.Pass(),node)
    cls=ast.ClassDef(name='Probe',bases=[],keywords=[],body=[StripImports().visit(n) for n in nodes],decorator_list=[])
    hs=importlib.util.spec_from_file_location('target_host_test',ROOT/'custom_components/power_sync/powerwall_host.py')
    hm=importlib.util.module_from_spec(hs);hs.loader.exec_module(hm)
    ns={'Any':object,'DOMAIN':'power_sync','local_battery_dispatch_configured':hm.local_battery_dispatch_configured,
        'TARGET_EXPORT_POWER_BATTERY_SYSTEMS':set(),'TARGET_CHARGE_POWER_BATTERY_SYSTEMS':set()}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[])), 'capability_probe','exec'),ns)
    c=ns['Probe']();c.battery_system='tesla';c.entry_id='entry';c.hass=SimpleNamespace(data={'power_sync':{'entry':{}}})
    c._config=SimpleNamespace(max_discharge_w=5000,max_grid_export_w=1500)
    c._entry=SimpleNamespace(data={'powerwall_local_paired':True,'powerwall_local_ip':'192.168.1.10'})
    assert c._supports_target_export_power() and c._supports_target_charge_power()
    assert c._export_command_power_w(SimpleNamespace(power_w=1000))==1000
    assert c._export_command_power_w(SimpleNamespace(power_w=5000))==1500
    assert c._export_command_power_w(SimpleNamespace(power_w=0))==0
    c._entry.data['powerwall_local_paired']=False
    assert not c._supports_target_export_power()
    assert c._export_command_power_w(SimpleNamespace(power_w=1000))==5000
    c._entry.data.update(powerwall_local_paired=True,powerwall_local_ip='http://127.0.0.2/')
    assert not c._supports_target_charge_power()
    c._entry.data['powerwall_local_ip']='gateway.local'
    c.hass.data['power_sync']['entry']['force_charge_state']={'active':True}
    assert not c._supports_target_charge_power()


def test_ack_records_actual_hardware_lease_and_failed_cleanup_status():
    async def run():
        ns,c,client,saved=load_routing()
        await ns['_try_local_powerwall_force']('charge',call(source='user'),15,500)
        state=ns['force_charge_state']
        seconds=(state['hardware_expires_at']-ns['dt_util'].utcnow()).total_seconds()
        assert 0 < seconds <= module.LEASE_SECONDS
        assert state['expires_at']>state['hardware_expires_at']
        client.resume_battery_operation.return_value=False
        with pytest.raises(ValueError): await c.stop()
        assert state['command_status']=='restore_pending'
        assert state['expires_at']==state['hardware_expires_at']
        assert saved['force_mode_state']['command_status']=='restore_pending'
    asyncio.run(run())


def test_first_optimizer_fallback_defers_for_targetless_reserve_safe_replan():
    async def run():
        ns,c,client,saved=load_routing()
        client.dispatch_battery_power.return_value=False
        with pytest.raises(RuntimeError,match='targetless safety checks'):
            await ns['_try_local_powerwall_force']('discharge',call(source='optimizer'),15,500)
        assert ns['_local_powerwall_runtime']['powerwall_dispatch_fallback_pending']
        assert not c.pending
        assert saved['force_mode_state'] is None
        client.resume_battery_operation.assert_awaited_once()
        # Only the next targetless-safeguarded request may enter the legacy
        # handler. Do not repeatedly retry local with targetless scheduling.
        assert await ns['_try_local_powerwall_force']('discharge',call(source='optimizer'),1,500) is None
        assert client.dispatch_battery_power.await_count==1
    asyncio.run(run())


@pytest.mark.parametrize('mode,changes', [
    ('charge',{'optimization_allow_grid_charge':False}),
    ('charge',{'optimization_max_charge_w':0}),
    ('charge',{'optimization_grid_charge_soc_cap':0}),
    ('discharge',{'optimization_max_discharge_w':0}),
    ('discharge',{'optimization_max_grid_export_w':0}),
])
def test_known_policy_denial_never_falls_back_on_missing_telemetry(mode,changes):
    async def run():
        ns,c,client,saved=load_routing()
        ns['entry'].data.update(changes)
        client.get_snapshot.side_effect=TimeoutError()
        with pytest.raises(RuntimeError):
            await ns['_try_local_powerwall_force'](mode,call(source='user'),15,500)
        client.get_snapshot.assert_not_called()
        client.dispatch_battery_power.assert_not_called()
        client.resume_battery_operation.assert_not_called()
    asyncio.run(run())


def test_manual_explicit_kw_failure_never_falls_back_to_uncontrolled_tariff():
    async def run():
        ns,c,client,saved=load_routing()
        client.dispatch_battery_power.return_value=False
        with pytest.raises(RuntimeError,match="cannot enforce the selected kW"):
            await ns['_try_local_powerwall_force']('discharge',call(source='user',power_w=500),5,500)
        client.resume_battery_operation.assert_awaited_once()
        assert not c.pending
        assert saved['force_mode_state'] is None
    asyncio.run(run())


def test_reported_lower_gateway_cap_retries_once_after_resume_with_fresh_policy():
    async def run():
        c,client,events=controller()
        order=[]
        async def dispatch(watts,seconds):
            order.append(('power',watts,seconds))
            if len([v for v in order if v[0]=='power'])==1:
                client.dispatch_discharge_limit_w=1000
                return False
            assert order[-2][0]=='resume'
            return True
        async def resume(): order.append(('resume',));return True
        client.dispatch_battery_power.side_effect=dispatch
        client.resume_battery_operation.side_effect=resume
        result=await c.start('discharge',2000,20)
        assert result['battery_power_w']==1000
        assert result['gateway_discharge_limit_w']==1000
        assert client.get_snapshot.await_count==2
        assert events==['intent','intent']  # Same durable owner throughout retry.
        assert order[0][1]==2800 and order[2][1]==1000
        assert order[2][2] <= order[0][2]
        await c.stop()
    asyncio.run(run())


def test_reported_gateway_cap_does_not_bypass_policy_changed_during_resume():
    async def run():
        c,client,events=controller()
        async def dispatch(watts,seconds):client.dispatch_discharge_limit_w=1000;return False
        async def resume():c.allowed=lambda mode:False;return True
        client.dispatch_battery_power.side_effect=dispatch
        client.resume_battery_operation.side_effect=resume
        with pytest.raises(ValueError,match='ownership or safety'):
            await c.start('discharge',2000,20)
        assert client.dispatch_battery_power.await_count==1
        assert not c.pending and events[-1]=='stopped'
    asyncio.run(run())


def test_force_power_selection_resolves_renamed_entity_for_current_entry():
    import ast
    source=(ROOT/'custom_components/power_sync/switch.py').read_text()
    nodes=[node for node in ast.parse(source).body if isinstance(node,ast.FunctionDef) and node.name in ('_state_float','_selected_force_power_w')]
    registry=SimpleNamespace(async_get_entity_id=lambda domain,platform,uid: 'number.home_force_power' if uid=='one_force_power_kw' else None)
    hass=SimpleNamespace(states=SimpleNamespace(get=lambda entity: SimpleNamespace(state={'number.home_force_power':'0.5','number.power_sync_force_power_kw':'8'}.get(entity,'unknown'))))
    ns={'er':SimpleNamespace(async_get=lambda h:registry),'DOMAIN':'power_sync','HomeAssistant':object,'ConfigEntry':object}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),'<force-number-routing>','exec'),ns)
    assert ns['_selected_force_power_w'](hass,SimpleNamespace(entry_id='one'))==500
