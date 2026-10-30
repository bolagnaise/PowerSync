"""Local power dispatch: export semantics, lease failure and ownership recovery."""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
_hybrid_spec = importlib.util.spec_from_file_location('hybrid_policy_probe', ROOT / 'custom_components/power_sync/powerwall_local/hybrid_policy.py')
_hybrid = importlib.util.module_from_spec(_hybrid_spec)
_hybrid_spec.loader.exec_module(_hybrid)
prefers_native_tariff = _hybrid.prefers_native_tariff
routing_site_info = _hybrid.routing_site_info
native_capacity_w = _hybrid.native_capacity_w
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


def test_native_ev_reservation_reduces_grid_following_battery_without_rewriting_limits():
    cfg = config(native_ev_unobserved_w=2000)
    assert module.battery_watts('charge', 6000, snapshot(load_w=2000, solar_w=0), cfg,
                               target_basis='grid_import') == -2000
    # A manual battery target shares the same site connection and cannot
    # consume power already reserved for an outstanding EV command.
    assert module.battery_watts('charge', 3000, snapshot(load_w=2000, solar_w=0), cfg) == -2000
    cfg['native_ev_unobserved_w'] = 6000
    assert module.battery_watts('charge', 6000, snapshot(load_w=2000, solar_w=0), cfg,
                               target_basis='grid_import') == 0
    assert cfg['optimization_max_grid_import_w'] == 6000
    assert cfg['optimization_max_charge_w'] == 4000
    with pytest.raises(ValueError, match='No grid import headroom'):
        module.battery_watts('charge', 3000, snapshot(load_w=2000, solar_w=0), cfg)


@pytest.mark.asyncio
async def test_tariff_fallback_cannot_consume_reserved_ev_capacity():
    controller = module.LocalBatteryDispatch(None, lambda: config(native_ev_unobserved_w=1000),
                                            None, None, None)
    with pytest.raises(ValueError, match='reserved EV headroom'):
        await controller.check_tariff_fallback('charge')


def site_snapshot(import_kw=14.9, export_kw=-10.1, **kw):
    return snapshot(raw={'config': {'site_info': {'max_site_meter_power_ac': import_kw,
                                                 'min_site_meter_power_ac': export_kw}}}, **kw)


def test_gateway_site_limits_use_kw_and_refresh_in_both_directions():
    cfg = config(optimization_max_discharge_w=21000, optimization_max_charge_w=21000,
                 optimization_max_grid_import_w=None)
    assert module.site_grid_limits_w(site_snapshot()) == (14900, 10100)
    assert module.battery_watts('discharge', 20000, site_snapshot(), cfg) == 10000
    assert module.battery_watts('discharge', 20000, site_snapshot(export_kw=-12), cfg) == 10000
    assert module.battery_watts('discharge', 20000, site_snapshot(export_kw=-2), cfg) == 2800
    assert module.battery_watts('charge', 20000, site_snapshot(import_kw=4), cfg) == -3200
    assert module.battery_watts('charge', 20000, site_snapshot(import_kw=2), cfg) == -1200
    assert module.battery_watts('discharge', 20000, site_snapshot(export_kw=-1e9), cfg) == 10000
    assert module.battery_watts('charge', 20000, site_snapshot(import_kw=1e9), cfg) == -10000
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


def test_manual_slider_bounds_respect_site_and_available_battery_headroom():
    cfg = config(optimization_max_charge_w=21000, optimization_max_discharge_w=21000,
                 optimization_max_grid_import_w=None)
    assert module.manual_power_limits_kw(site_snapshot(), cfg, 10000) == {
        'charge_input_max_kw': 10, 'grid_export_max_kw': 9.2}
    assert module.manual_power_limits_kw(site_snapshot(import_kw=4, export_kw=-2), cfg, 10000) == {
        'charge_input_max_kw': 3.2, 'grid_export_max_kw': 2}
    assert module.manual_power_limits_kw(site_snapshot(import_kw=16, export_kw=-12), cfg, 10000) == {
        'charge_input_max_kw': 10, 'grid_export_max_kw': 9.2}
    assert module.manual_power_limits_kw(site_snapshot(import_kw=1e9, export_kw=-1e9), cfg, 10000) == {
        'charge_input_max_kw': 10, 'grid_export_max_kw': 9.2}
    assert module.manual_power_limits_kw(site_snapshot(), {**cfg, 'optimization_max_discharge_w': 0})['grid_export_max_kw'] == 0


@pytest.mark.parametrize('load,solar,site_limit,expected', [
    (8000, 0, -14.9, 2), (8000, 3000, -14.9, 5),
    (8000, 3000, -1, 1), (11000, 0, -14.9, 0),
])
def test_manual_export_slider_deducts_home_load_from_learned_battery_ceiling(load, solar, site_limit, expected):
    cfg = config(optimization_max_discharge_w=21000)
    snap = site_snapshot(export_kw=site_limit, load_w=load, solar_w=solar)
    assert module.manual_power_limits_kw(snap, cfg, 10000)['grid_export_max_kw'] == expected


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
    assert number.native_max_value == 10
    assert number.native_value == 10
    coord.data = site_snapshot(import_kw=4, export_kw=-2)
    assert number.native_max_value == 3.2
    assert number.native_value == 3.2
    coord.data = site_snapshot(import_kw=16, export_kw=-12)
    assert number.native_max_value == 10
    assert number.extra_state_attributes['grid_export_max_kw'] == 9.2
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
    assert status['grid_export_max_kw'] == 4.2
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


@pytest.mark.asyncio
async def test_pending_battery_charge_draw_survives_uncertain_ack_until_resume():
    c, client, events = controller()
    entered, release = asyncio.Event(), asyncio.Event()
    async def dispatch(*_):
        entered.set(); await release.wait(); return False
    client.dispatch_battery_power.side_effect = dispatch
    client.resume_battery_operation.return_value = False
    task = asyncio.create_task(c.start('charge',3000,20))
    await entered.wait()
    assert c.charge_generation is None  # Manual battery target, not grid following.
    assert c.prospective_charge_w == 3000 and c._last_watts == 0
    release.set()
    with pytest.raises(ValueError): await task
    assert c.prospective_charge_w == 3000 and c.pending
    client.resume_battery_operation.return_value = True
    await c.stop()
    assert c.prospective_charge_w == 0 and not c.pending


@pytest.mark.asyncio
async def test_borrow_refresh_preserves_exact_generation_owner_target_and_deadline():
    c, client, _ = controller()
    cfg = config()
    c.config = lambda: cfg
    await c.start('charge',6000,20,target_basis='grid_import')
    generation, deadline, allowed = c.charge_generation, c._deadline, c.allowed
    cfg['native_ev_unobserved_w'] = 2000
    result = await c.refresh_current_generation(generation)
    assert result['battery_power_w'] == -3200
    assert c._deadline == deadline and c._target == 6000 and c.allowed is allowed
    assert await c.refresh_current_generation(generation+1) is None
    await c.stop()


@pytest.mark.asyncio
async def test_tariff_fallback_rechecks_reservations_after_native_limit_read():
    c, client, _ = controller()
    cfg = config()
    c.config = lambda: cfg
    async def snapshot_with_new_ev_demand():
        cfg['native_ev_unobserved_w'] = 5000
        return snapshot()
    client.get_snapshot.side_effect = snapshot_with_new_ev_demand
    with pytest.raises(ValueError): await c.check_tariff_fallback('charge')


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
    names = {'_track_tesla_settings_control', '_clear_local_powerwall_dispatch', '_stop_local_powerwall_dispatch', '_try_local_powerwall_force',
             '_try_local_powerwall_force_locked',
             '_stop_local_powerwall_dispatch_locked', '_stop_native_max_charge_locked',
             '_set_tesla_control_method', '_restore_local_grid_charging_via_cloud', '_stop_native_max_charge', '_try_native_max_charge', '_try_native_powerwall_force', '_powerwall_prefers_native_tariff',
             '_cloud_tariff_control_requested', '_prepare_cloud_tariff_control',
             'handle_force_charge', 'handle_force_discharge', 'persist_force_mode_state', 'restore_force_mode_from_persistence',
             '_local_powerwall_dispatch_config', '_local_powerwall_dispatch_acknowledged', '_local_powerwall_dispatch_failed', '_local_powerwall_generation_current'}
    nodes = [n for n in setup.body if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name in names]
    class StripImports(ast.NodeTransformer):
        def visit_ImportFrom(self, node): return ast.copy_location(ast.Pass(), node)
    nodes = [StripImports().visit(n) for n in nodes]
    nodes += [n for n in tree.body if getattr(n, 'name', '') == '_update_powerwall_store']
    state = lambda: dict(active=False, saved_tariff=None, saved_operation_mode=None, saved_backup_reserve=None,
                         saved_grid_charging_enabled=None, saved_export_rule=None, expires_at=None,
                         hardware_expires_at=None, power_w=0)
    charge, discharge = state(), state()
    runtime = {}
    hass = SimpleNamespace(data={'power_sync': {'entry': runtime}})
    data = dict(tesla_control_method='auto', battery_system='tesla', powerwall_local_paired=paired, powerwall_local_ip='192.168.1.10', **config())
    data.update(options)
    entry = SimpleNamespace(entry_id='entry', data=data, options={})
    saved = {}
    async def save(value): saved.clear(); saved.update(value)
    store = SimpleNamespace(async_load=AsyncMock(side_effect=lambda: dict(saved)), async_save=AsyncMock(side_effect=save))
    now = SimpleNamespace(utcnow=lambda: datetime.now(timezone.utc))
    hs = importlib.util.spec_from_file_location('local_host_test', ROOT/'custom_components/power_sync/powerwall_host.py')
    hm = importlib.util.module_from_spec(hs); hs.loader.exec_module(hm)
    lock_spec = importlib.util.spec_from_file_location('battery_route_test', ROOT/'custom_components/power_sync/powerwall_local/route_lock.py')
    lock_module = importlib.util.module_from_spec(lock_spec); lock_spec.loader.exec_module(lock_module)
    duration_spec = importlib.util.spec_from_file_location('local_discharge_duration_test', ROOT/'custom_components/power_sync/force_discharge_duration.py')
    durations = importlib.util.module_from_spec(duration_spec); duration_spec.loader.exec_module(durations)
    upload_spec = importlib.util.spec_from_file_location('tariff_upload_probe', ROOT/'custom_components/power_sync/tesla_tariff_upload.py')
    uploads = importlib.util.module_from_spec(upload_spec); upload_spec.loader.exec_module(uploads)
    from test_powerwall_control_method import policy
    ns = dict(NativeMaxChargeError=type("NativeMaxChargeError", (Exception,), {}),runtime_route=policy.runtime_route, control_method=policy.control_method,
              reserve_tariff_upload=uploads.reserve_tariff_upload,
              TariffUploadReservationError=uploads.TariffUploadReservationError,
              get_tesla_api_base_url=lambda provider, base: base or provider, CONF_FLEET_API_BASE_URL='fleet_api_base_url',
              CONF_TESLA_ENERGY_SITE_ID='tesla_energy_site_id', CONF_POWERWALL_LOCAL_DIN='powerwall_local_din',
              validate_method=policy.validate_method, CONF_CONTROL_METHOD=policy.CONF_CONTROL_METHOD, prefers_native_tariff=prefers_native_tariff, routing_site_info=routing_site_info, native_capacity_w=native_capacity_w, math=__import__('math'), time=__import__('time'), uuid4=__import__('uuid').uuid4,
              _get_tesla_site_configs=lambda *_: [],
              update_native_discharge_alert=lambda *_: None,
              update_native_charge_alert=lambda *_: None,
              local_battery_dispatch_configured=hm.local_battery_dispatch_configured, asyncio=asyncio, Any=object, ServiceCall=object, HomeAssistantError=RuntimeError,
              selected_discharge_duration=durations.selected_discharge_duration, validate_native_discharge_duration=durations.validate_native_discharge_duration,
              DOMAIN='power_sync', hass=hass, entry=entry, _local_powerwall_runtime=runtime, DispatchCommandFailed=module.DispatchCommandFailed, GridChargingPermissionError=module.GridChargingPermissionError, validate_dispatch_config=module.validate_dispatch_config, _LOGGER=logging.getLogger(__name__),
              force_charge_state=charge, force_discharge_state=discharge,
              hold_soc_state={'active':False}, self_consumption_state={'active':False},
              _local_powerwall_route_lock=lock_module.BatteryRouteLock(), _command_generation=[0],
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
    runtime['powerwall_local']={'client':client,'coordinator':SimpleNamespace(data=client.get_snapshot.return_value,last_success_monotonic=__import__('time').monotonic())}
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


def test_active_legacy_force_is_restored_before_local_control():
    async def run():
        ns,c,client,saved=load_routing()
        ns['force_charge_state'].update(active=True,saved_tariff={'baseline':True})
        async def restore(_):
            client.dispatch_battery_power.assert_not_called()
            ns['force_charge_state'].update(active=False,saved_tariff=None)
        ns['handle_restore_normal']=AsyncMock(side_effect=restore)
        result=await ns['_try_local_powerwall_force']('charge',call(source='user'),15,500)
        assert result['battery_power_w']==-500
        ns['handle_restore_normal'].assert_awaited_once()
        await c.stop()
    asyncio.run(run())


def test_unconfirmed_legacy_restore_blocks_local_control():
    async def run():
        ns,c,client,saved=load_routing()
        ns['force_charge_state'].update(active=True,saved_tariff={'baseline':True})
        ns['handle_restore_normal']=AsyncMock()
        with pytest.raises(RuntimeError,match='cleanup remains pending'):
            await ns['_try_local_powerwall_force']('charge',call(source='user'),15,500)
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
    names={'_powerwall_route','_powerwall_plan_uses_tariff','_supports_local_powerwall_target_power','_supports_target_export_power','_supports_target_charge_power','_export_command_power_w','_native_powerwall_safety_power_w'}
    nodes=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in names]
    class StripImports(ast.NodeTransformer):
        def visit_ImportFrom(self,node): return ast.copy_location(ast.Pass(),node)
    cls=ast.ClassDef(name='Probe',bases=[],keywords=[],body=[StripImports().visit(n) for n in nodes],decorator_list=[])
    hs=importlib.util.spec_from_file_location('target_host_test',ROOT/'custom_components/power_sync/powerwall_host.py')
    hm=importlib.util.module_from_spec(hs);hs.loader.exec_module(hm)
    ns={'Any':object,'DOMAIN':'power_sync','local_battery_dispatch_configured':hm.local_battery_dispatch_configured,
        'TARGET_EXPORT_POWER_BATTERY_SYSTEMS':set(),'TARGET_CHARGE_POWER_BATTERY_SYSTEMS':set(),
        'prefers_native_tariff':prefers_native_tariff,'routing_site_info':routing_site_info, 'runtime_route':__import__('test_powerwall_control_method').policy.runtime_route}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[])), 'capability_probe','exec'),ns)
    c=ns['Probe']();c.battery_system='tesla';c.entry_id='entry';c.hass=SimpleNamespace(data={'power_sync':{'entry':{}}})
    c._config=SimpleNamespace(max_discharge_w=5000,max_grid_export_w=1500)
    c._entry=SimpleNamespace(data={'tesla_control_method':'local','powerwall_local_paired':True,'powerwall_local_ip':'192.168.1.10'})
    assert c._supports_target_export_power() and c._supports_target_charge_power()
    assert c._export_command_power_w(SimpleNamespace(power_w=1000))==1000
    assert c._export_command_power_w(SimpleNamespace(power_w=5000))==1500
    assert c._export_command_power_w(SimpleNamespace(power_w=0))==0
    c._entry.data['tesla_control_method']='tariff'
    c.energy_coordinator=SimpleNamespace(_site_info_cache={'nameplate_power':20000},_site_info_last_fetch=__import__('time').monotonic())
    assert not c._supports_target_export_power() and not c._supports_target_charge_power()
    assert c._export_command_power_w(SimpleNamespace(power_w=1000))==5000
    c._entry.data['tesla_control_method']='local'
    c.energy_coordinator._site_info_cache['nameplate_power']=10000
    assert c._supports_target_charge_power()
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


def test_reported_charge_ceiling_retries_below_limit_without_extending_deadline():
    async def run():
        c, client, events = controller()
        cfg = config(optimization_max_charge_w=20000, optimization_max_grid_import_w=25000)
        c.config = lambda: cfg
        order = []
        async def dispatch(watts, seconds):
            order.append(('power', watts, seconds))
            if len([x for x in order if x[0] == 'power']) == 1:
                client.dispatch_charge_limit_w = 8000
                return False
            return True
        async def resume(): order.append(('resume',)); return True
        client.dispatch_battery_power.side_effect = dispatch
        client.resume_battery_operation.side_effect = resume
        result = await c.start('charge', 12000, 20)
        assert result['battery_power_w'] == -8000
        assert result['requested_power_w'] == 12000
        assert result['gateway_charge_limit_w'] == 8000
        assert cfg['optimization_max_charge_w'] == 20000
        assert order[0][1] == -10000 and order[1] == ('resume',) and order[2][1] == -8000
        assert order[2][2] <= order[0][2]
        assert events == ['intent', 'intent'] and client.get_snapshot.await_count == 2
        await c.stop()
    asyncio.run(run())


def test_renewal_charge_ceiling_recovery_preserves_current_lease_owner():
    async def run():
        c, client, events = controller()
        c.config = lambda: config(optimization_max_charge_w=20000, optimization_max_grid_import_w=15000)
        client.get_snapshot.side_effect = [snapshot(load_w=6000, solar_w=0), snapshot(load_w=1000, solar_w=0), snapshot(load_w=2000, solar_w=0)]
        sent = []
        async def dispatch(watts, seconds):
            sent.append(watts)
            if len(sent) == 2:
                client.dispatch_charge_limit_w = 8000
                return False
            return True
        client.dispatch_battery_power.side_effect = dispatch
        await c.start('charge', 12000, 120)
        deadline = c._deadline
        await c._write_with_limit_retry()
        assert sent == [-9000, -10000, -8000]
        assert c.pending and c._deadline == deadline
        assert events == ['intent', 'intent']
        client.resume_battery_operation.assert_awaited_once()
        await c.stop()
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['resume', 'policy', 'second_rejection', 'deadline'])
def test_charge_cap_retry_cannot_escape_cleanup_policy_or_original_window(failure):
    async def run():
        c, client, events = controller()
        c.config = lambda: config(optimization_max_charge_w=20000, optimization_max_grid_import_w=25000)
        async def dispatch(watts, seconds):
            client.dispatch_charge_limit_w = 10000 if client.dispatch_battery_power.await_count == 1 else 9000
            return False
        async def resume():
            if failure == 'resume': return False
            if failure == 'policy': c.allowed = lambda mode: False
            if failure == 'deadline': c._deadline = 0
            return True
        client.dispatch_battery_power.side_effect = dispatch
        client.resume_battery_operation.side_effect = resume
        with pytest.raises((ValueError, module.DispatchCommandFailed)):
            await c.start('charge', 12000, 20)
        assert client.dispatch_battery_power.await_count == (2 if failure == 'second_rejection' else 1)
        assert c.pending is (failure == 'resume')
        assert c._task is None
    asyncio.run(run())


def test_manual_charge_cap_is_independent_of_export_cap_and_nameplate():
    cfg = config(optimization_max_charge_w=20000, optimization_max_discharge_w=20000, optimization_max_grid_import_w=None)
    limits = module.manual_power_limits_kw(site_snapshot(load_w=0, solar_w=0), cfg, 8000, 10000)
    assert limits == {'charge_input_max_kw': 10, 'grid_export_max_kw': 8}
    assert cfg['optimization_max_charge_w'] == 20000


def test_charge_cap_first_learned_on_renewal_updates_persisted_acknowledged_status():
    async def run():
        ns, c, client, saved = load_routing(optimization_max_charge_w=20000, optimization_max_grid_import_w=15000)
        client.get_snapshot.side_effect = [snapshot(load_w=6000, solar_w=0), snapshot(load_w=1000, solar_w=0), snapshot(load_w=2000, solar_w=0)]
        async def dispatch(watts, seconds):
            if client.dispatch_battery_power.await_count == 2:
                client.dispatch_charge_limit_w = 10000
                return False
            return True
        client.dispatch_battery_power.side_effect = dispatch
        await ns['_try_local_powerwall_force']('charge', call(source='user', power_w=12000), 15, 12000)
        assert ns['force_charge_state'].get('gateway_charge_limit_w') is None
        await c._write_with_limit_retry()
        assert ns['force_charge_state']['gateway_charge_limit_w'] == 10000
        assert ns['force_charge_state']['battery_power_w'] == -10000
        assert ns['force_charge_state']['command_status'] == 'accepted'
        assert saved['force_mode_state']['gateway_charge_limit_w'] == 10000
        await c.stop()
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['charge', 'discharge'])
def test_learned_native_cap_blocks_targetless_tariff_fallback(mode):
    async def run():
        c, client, _ = controller()
        setattr(client, f'dispatch_{mode}_limit_w', 10000)
        client.get_snapshot.return_value = snapshot()
        with pytest.raises(ValueError, match='cannot enforce the learned gateway dispatch limit'):
            await c.check_tariff_fallback(mode)
        client.dispatch_battery_power.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize('load,solar,expected', [(3500,0,-9000),(10860,0,-1640),(3500,2000,-10000),(13000,0,0)])
def test_grid_import_target_shares_actual_home_ev_and_solar(load,solar,expected):
    cfg=config(optimization_max_charge_w=20000, optimization_max_grid_import_w=14900)
    actual=module.battery_watts('charge',12500,snapshot(load_w=load,solar_w=solar),cfg,target_basis='grid_import')
    assert actual == expected
    if load-solar <= 14900:
        assert load-solar-actual <= 14900


def test_grid_import_target_renewal_uses_measured_load_and_preserves_deadline():
    async def run():
        c,client,_=controller()
        c.config=lambda: config(optimization_max_charge_w=20000,optimization_max_grid_import_w=14900)
        client.get_snapshot.return_value=snapshot(load_w=3500,solar_w=0)
        result=await c.start('charge',12500,60,target_basis='grid_import')
        assert result['battery_power_w']==-9000
        assert result['power_target_basis']=='grid_import'
        deadline=c._deadline
        await c._cancel_task()
        client.get_snapshot.return_value=snapshot(load_w=10860,solar_w=0)
        await c._write_with_limit_retry()
        assert client.dispatch_battery_power.call_args.args[0]==-1640
        assert c.grid_import_estimate_w==12500
        client.get_snapshot.return_value=snapshot(load_w=3500,solar_w=0)
        await c._write_with_limit_retry()
        assert client.dispatch_battery_power.call_args.args[0]==-9000
        assert c._deadline==deadline
        await c.stop()
    asyncio.run(run())


def test_grid_import_request_routes_without_changing_manual_charge_semantics():
    async def run():
        ns,c,client,saved=load_routing(optimization_max_charge_w=20000, optimization_spread_import_enabled=True)
        request=call(source='optimizer',grid_import_target_w=5000)
        result=await ns['_try_local_powerwall_force']('charge',request,15,2000)
        assert result['power_target_basis']=='grid_import'
        # Default fixture home 1000 less solar200 => 4200 battery input.
        assert result['battery_power_w']==-4200
        assert ns['force_charge_state']['grid_import_target_w']==5000
        await c.stop()
        result=await ns['_try_local_powerwall_force']('charge',call(source='user'),15,2000)
        assert result['battery_power_w']==-2000
        assert result['power_target_basis']=='battery'
        await c.stop()
    asyncio.run(run())


@pytest.mark.parametrize('target', [True,0,-1,float('nan'),'5000'])
def test_grid_import_target_rejects_invalid_or_non_optimizer_requests(target):
    async def run():
        ns,c,client,_=load_routing(optimization_spread_import_enabled=True)
        with pytest.raises(RuntimeError,match='Invalid optimiser grid-import target'):
            await ns['_try_local_powerwall_force']('charge',call(source='optimizer',grid_import_target_w=target),15,2000)
        client.dispatch_battery_power.assert_not_called()
    asyncio.run(run())


def test_failed_grid_target_does_not_enter_tariff_fallback():
    async def run():
        ns,c,client,_=load_routing(optimization_spread_import_enabled=True)
        client.dispatch_battery_power.return_value=False
        with pytest.raises(RuntimeError,match='tariff fallback cannot hold the target'):
            await ns['_try_local_powerwall_force']('charge',call(source='optimizer',grid_import_target_w=5000),15,2000)
        client.resume_battery_operation.assert_awaited()
        assert not c.pending
        assert not ns['_local_powerwall_runtime'].get('powerwall_dispatch_fallback_pending')
    asyncio.run(run())


def test_grid_target_stops_when_spread_import_is_disabled():
    async def run():
        ns,c,client,_=load_routing(optimization_spread_import_enabled=True)
        await ns['_try_local_powerwall_force']('charge',call(source='optimizer',grid_import_target_w=5000),15,2000)
        await c._cancel_task()
        ns['entry'].data['optimization_spread_import_enabled']=False
        before=client.dispatch_battery_power.await_count
        with pytest.raises(ValueError,match='ownership or safety'):
            await c._write_with_limit_retry()
        assert client.dispatch_battery_power.await_count==before
        await c.stop()
    asyncio.run(run())


def test_local_gateway_limits_constrain_plan_without_changing_config():
    import ast,math,time
    path=ROOT/'custom_components/power_sync/optimization/coordinator.py'
    cls=next(n for n in ast.parse(path.read_text()).body if isinstance(n,ast.ClassDef) and n.name=='OptimizationCoordinator')
    names={'_local_powerwall_plan_limits','_effective_charge_and_import_limits'}
    nodes=[n for n in cls.body if getattr(n,'name','') in names]
    class StripImports(ast.NodeTransformer):
        def visit_ImportFrom(self,node):return ast.copy_location(ast.Pass(),node)
    ns=dict(math=math,time=time,DOMAIN='power_sync',site_grid_limits_w=module.site_grid_limits_w,
        prefers_native_tariff=prefers_native_tariff,routing_site_info=routing_site_info)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[StripImports().visit(n) for n in nodes],type_ignores=[])),str(path),'exec',flags=__import__('__future__').annotations.compiler_flag),ns)
    client=SimpleNamespace(dispatch_charge_limit_w=10000,dispatch_discharge_limit_w=9000)
    coord=SimpleNamespace(data=site_snapshot(import_kw=14.9),last_success_monotonic=time.monotonic())
    subject=SimpleNamespace(battery_system='tesla',entry_id='entry',_config=SimpleNamespace(max_charge_w=20000,max_grid_import_w=15000),
        hass=SimpleNamespace(data={'power_sync':{'entry':{'powerwall_local':{'client':client,'coordinator':coord}}}}),
        _normalize_optional_power_w=lambda n:n)
    subject._powerwall_route=lambda direction:'local'
    subject._local_powerwall_plan_limits=lambda:ns['_local_powerwall_plan_limits'](subject)
    assert ns['_effective_charge_and_import_limits'](subject)==(10000,14900)
    assert subject._config.max_charge_w==20000
    assert subject._config.max_grid_import_w==15000
    subject._powerwall_route=lambda direction:'tariff'
    subject.energy_coordinator=SimpleNamespace(_site_info_cache={'nameplate_power':20000},_site_info_last_fetch=__import__('time').monotonic())
    assert ns['_effective_charge_and_import_limits'](subject)==(20000,14900)
    assert 'max_discharge_w' not in subject._local_powerwall_plan_limits()


def test_zero_forecast_battery_plan_still_fills_missing_ev_draw():
    async def run():
        ns,c,client,_=load_routing(optimization_spread_import_enabled=True,optimization_max_charge_w=20000)
        result=await ns['_try_local_powerwall_force']('charge',call(source='optimizer',grid_import_target_w=5000),15,0)
        assert result['battery_power_w']==-4200
        await c.stop()
    asyncio.run(run())


def test_grid_following_releases_instead_of_holding_zero_above_hard_import_cap():
    with pytest.raises(ValueError,match='Home load exceeds the grid import limit'):
        module.battery_watts('charge',12500,snapshot(load_w=17000,solar_w=0),
            config(optimization_max_charge_w=20000,optimization_max_grid_import_w=14900),target_basis='grid_import')


@pytest.mark.asyncio
async def test_automatic_manual_max_selects_native_path_without_local_watts():
    ns,c,client,_=load_routing(tesla_control_method='auto')
    session=SimpleNamespace(pending=False,adapter=SimpleNamespace(guard=AsyncMock(return_value=True)),
        start=AsyncMock(return_value={'expires_at':2000}))
    ns['entry'].options={'tesla_control_method':'auto'}
    ns['_powerwall_prefers_native_tariff'] = lambda direction='discharge': True
    ns.update(_native_max_context={'call':None}, _get_tesla_site_configs=lambda *_:[('site','token','provider')],
        _get_native_max_charge=AsyncMock(return_value=session),
        _create_charge_tariff=lambda duration:({'code':'temporary','name':'Force Charge'},None))
    result=await ns['handle_force_charge'](call(source='user',duration=15,power_w=0))
    assert result['native_max_charge'] and not result['physical_effect_confirmed']
    client.dispatch_battery_power.assert_not_called()
    args=session.start.await_args
    assert args.args==(900,) and args.kwargs['deadline']>0
    assert session.adapter.forcing_tariff['code'].startswith('CHARGE_NATIVE_MAX_')


@pytest.mark.asyncio
async def test_native_max_drains_inflight_price_upload_before_capturing_restore_tariff():
    ns, local, client, _ = load_routing(tesla_control_method='auto')
    ns['entry'].options = {'tesla_control_method': 'auto'}
    ns['_powerwall_prefers_native_tariff'] = lambda direction='discharge': True
    slot = {'busy': True, 'retry_at': 0.0}
    ns['hass'].data['power_sync']['_tesla_tou_uploads'] = {('provider', 'provider', 'site'): slot}
    observed = {'code': 'old-price-tariff'}
    captured = []
    async def start(seconds, *, deadline):
        captured.append(observed['code'])
        return {'expires_at': deadline}
    session = SimpleNamespace(pending=False,
        adapter=SimpleNamespace(guard=AsyncMock(return_value=True)), start=AsyncMock(side_effect=start))
    ns.update(_native_max_context={}, _get_tesla_site_configs=lambda *_: [('site', 'token', 'provider')],
        _get_native_max_charge=AsyncMock(return_value=session),
        get_tesla_api_base_url=lambda provider, base: base or provider,
        CONF_FLEET_API_BASE_URL='fleet_api_base_url',
        _create_charge_tariff=lambda duration: ({'code': 'force'}, None))
    deadline = ns['dt_util'].utcnow().timestamp() + 60
    request = asyncio.create_task(ns['_try_native_max_charge'](call(source='user', power_w=0), 1, deadline))
    try:
        await asyncio.sleep(.01)
        assert not session.start.await_count, 'Native Max captured before the in-flight price write settled'
        observed['code'] = 'new-price-tariff'
        slot['busy'] = False
        result = await request
        assert captured == ['new-price-tariff']
        assert result['success']
        assert session.start.await_args.kwargs['deadline'] == deadline
    finally:
        slot['busy'] = False
        if not request.done():
            request.cancel()
        await asyncio.gather(request, return_exceptions=True)
    assert 'reservation' not in slot
    client.dispatch_battery_power.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('power_request', [{'source':'optimizer','power_w':0},
    {'source':'user','power_w':500}, {'source':'user','power_w':True}])
async def test_precise_or_optimizer_request_never_selects_native_max(power_request):
    ns,c,client,_=load_routing()
    ns['_get_native_max_charge']=AsyncMock()
    assert await ns['_try_native_max_charge'](call(**power_request),15,2000) is None
    ns['_get_native_max_charge'].assert_not_called()


@pytest.mark.asyncio
async def test_native_max_finishes_existing_restore_before_capture_and_never_extends_deadline():
    ns,c,client,_=load_routing()
    ns['force_charge_state']['active']=True
    events=[]
    async def restore(_):events.append('restore');ns['force_charge_state']['active']=False
    async def get_session():events.append('capture-owner');return session
    session=SimpleNamespace(pending=False,adapter=SimpleNamespace(guard=AsyncMock(return_value=True)),
        start=AsyncMock(return_value={'expires_at':2000}))
    ns['entry'].options={'tesla_control_method':'auto'}
    ns['_powerwall_prefers_native_tariff'] = lambda direction='discharge': True
    ns.update(_native_max_context={'call':None}, _get_tesla_site_configs=lambda *_:[('site','token','provider')],
        _get_native_max_charge=get_session,handle_restore_normal=restore,
        _create_charge_tariff=lambda duration:({'code':'temporary'},None))
    await ns['_try_native_max_charge'](call(source='user',power_w=0),15,2000)
    assert events==['restore','capture-owner']
    assert session.start.await_args.kwargs['deadline']==2000


@pytest.mark.asyncio
async def test_native_max_cleanup_must_finish_before_numeric_local_handoff():
    ns,c,client,_=load_routing()
    runtime=ns['_local_powerwall_runtime']
    async def stop():native.pending=False;ns['force_charge_state'].update(active=False,native_max_charge=False)
    native=SimpleNamespace(pending=True,stop=AsyncMock(side_effect=stop))
    ns['entry'].options={'tesla_control_method':'auto'}
    ns['_powerwall_prefers_native_tariff'] = lambda direction='discharge': True
    runtime['powerwall_native_max_charge']=native
    ns['force_charge_state'].update(active=True,native_max_charge=True)
    result=await ns['_try_local_powerwall_force']('charge',call(source='user',power_w=500),15,500)
    assert result['control_path']=='powerwall_local_dispatch'
    native.stop.assert_awaited_once()
    assert client.dispatch_battery_power.await_args.args[0]==-500
    await c.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('native_first',[True,False])
async def test_queued_native_and_numeric_acquisitions_never_leave_competing_actuators(native_first):
    ns,c,client,_=load_routing();runtime=ns['_local_powerwall_runtime']
    events=[]
    async def start(*args,**kwargs):
        assert not c.pending
        events.append('native_start');native.pending=True
        ns['force_charge_state'].update(active=True,native_max_charge=True)
        return {'expires_at':2000}
    async def stop():
        events.append('native_stop');native.pending=False
        ns['force_charge_state'].update(active=False,native_max_charge=False)
    native=SimpleNamespace(pending=False,adapter=SimpleNamespace(guard=AsyncMock(return_value=True)),
        start=AsyncMock(side_effect=start),stop=AsyncMock(side_effect=stop))
    ns['entry'].options={'tesla_control_method':'auto'}
    ns['_powerwall_prefers_native_tariff'] = lambda direction='discharge': True
    runtime['powerwall_native_max_charge']=native
    ns['entry'].options={'tesla_control_method':'auto'}
    ns.update(_native_max_context={'call':None}, _get_tesla_site_configs=lambda *_:[('site','token','provider')],
        _get_native_max_charge=AsyncMock(return_value=native),
        _create_charge_tariff=lambda duration:({'code':'temporary'},None))
    async def max_charge():return await ns['_try_native_max_charge'](call(source='user',power_w=0),15,2000)
    async def numeric():return await ns['_try_local_powerwall_force']('charge',call(source='user',power_w=500),15,500)
    async with ns['_local_powerwall_route_lock']:
        first=asyncio.create_task(max_charge() if native_first else numeric())
        await asyncio.sleep(0)
        second=asyncio.create_task(numeric() if native_first else max_charge())
        await asyncio.sleep(0)
    await asyncio.wait_for(asyncio.gather(first,second),2)
    assert c.pending is native_first and native.pending is not native_first
    if native_first:assert events==['native_start','native_stop']
    else:
        assert events==['native_start']
        client.resume_battery_operation.assert_awaited()
    if native.pending:await native.stop()
    await c.stop()


@pytest.mark.asyncio
async def test_explicit_max_discharge_releases_local_and_selects_existing_tariff_lifecycle():
    ns,c,client,_=load_routing(optimization_max_grid_export_w=15000)
    session=_install_native_discharge_owner(ns,c,client)
    client.get_snapshot.return_value=site_snapshot(export_kw=-14.8, soc=70)
    client.dispatch_discharge_limit_w=10000
    await c.start('discharge',2000,120)
    result=await ns['_try_local_powerwall_force']('discharge',call(source='user',power_w=0),15,0)
    assert result['control_path']=='powerwall_native_tariff'
    await session.stop()
    assert not c.pending
    assert client.dispatch_battery_power.await_count==1
    client.resume_battery_operation.assert_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('owner',['user','automation'])
async def test_max_discharge_does_not_inherit_the_timed_gateway_power_ceiling(owner):
    ns,c,client,_=load_routing(optimization_max_grid_export_w=14800)
    session=_install_native_discharge_owner(ns,c,client)
    client.get_snapshot.return_value=site_snapshot(export_kw=-14.8)
    client.dispatch_discharge_limit_w=10000
    result=await ns['_try_local_powerwall_force']('discharge',call(source=owner,power_w=0),15,0)
    assert result['control_path']=='powerwall_native_tariff'
    await session.stop()
    client.dispatch_battery_power.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('owner',['user','automation','optimizer'])
async def test_positive_discharge_targets_keep_local_watts(owner):
    ns,c,client,_=load_routing()
    result=await ns['_try_local_powerwall_force']('discharge',call(source=owner,power_w=2000),15,2000)
    assert result['control_path']=='powerwall_local_dispatch'
    assert client.dispatch_battery_power.await_args.args[0]==2800
    await c.stop()


@pytest.mark.asyncio
async def test_max_discharge_preserves_software_export_limit_not_enforced_by_native_site():
    ns,c,client,_=load_routing(optimization_max_grid_export_w=2000)
    client.get_snapshot.return_value=site_snapshot(export_kw=-14.8)
    result=await ns['_try_local_powerwall_force']('discharge',call(source='user',power_w=0),15,0)
    assert result['control_path']=='powerwall_local_dispatch'
    assert client.dispatch_battery_power.await_args.args[0]<=2800
    await c.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('changes',[{'soc':10},{'grid_status':'SystemIslandedActive'}])
async def test_native_max_discharge_does_not_bypass_soc_or_connection(changes):
    ns,c,client,_=load_routing()
    session=_install_native_discharge_owner(ns,c,client)
    client.get_snapshot.return_value=site_snapshot(**changes)
    with pytest.raises(ValueError):
        await ns['_try_local_powerwall_force']('discharge',call(source='user',power_w=0),15,0)
    client.dispatch_battery_power.assert_not_called()


@pytest.mark.asyncio
async def test_native_max_discharge_missing_native_cap_evidence_keeps_local_control():
    c,client,_=controller()
    client.get_snapshot.return_value=snapshot(raw={'config':{'site_info':{}}})
    assert not await c.native_max_discharge_available()


@pytest.mark.asyncio
async def test_max_discharge_rechecks_setup_after_final_native_limit_read():
    ns,c,client,_=load_routing()
    session=_install_native_discharge_owner(ns,c,client)
    count=0
    async def observe():
        nonlocal count
        count+=1
        if count==2:ns['_local_powerwall_runtime']['powerwall_local_dispatch_stopping']=True
        return site_snapshot()
    client.get_snapshot.side_effect=observe
    with pytest.raises(RuntimeError,match='authority|policy'):
        await ns['_try_local_powerwall_force']('discharge',call(source='user',power_w=0),15,0)
    client.dispatch_battery_power.assert_not_called()


@pytest.mark.asyncio
async def test_max_discharge_cannot_cross_unconfirmed_resume_into_tariff_control():
    ns,c,client,_=load_routing()
    session=_install_native_discharge_owner(ns,c,client)
    await c.start('discharge',500,120)
    client.resume_battery_operation.side_effect=TimeoutError()
    with pytest.raises(RuntimeError,match='resume unconfirmed'):
        await ns['_try_local_powerwall_force']('discharge',call(source='user',power_w=0),15,0)
    assert c.pending
    client.resume_battery_operation.side_effect=None
    await c.stop()


@pytest.mark.asyncio
async def test_serialized_discharge_holds_route_lock_through_tariff_acquisition():
    import ast
    tree=ast.parse((ROOT/'custom_components/power_sync/__init__.py').read_text())
    function=next(n for n in ast.walk(tree) if isinstance(n,ast.AsyncFunctionDef) and n.name=='_serialized_force_discharge')
    ns,c,_,_=load_routing()
    entered,release=asyncio.Event(),asyncio.Event()
    async def force(_):entered.set();await release.wait();return {'success':True}
    ns['_unserialized_force_discharge']=force
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function],type_ignores=[])),'serialized-test','exec'),ns)
    operation=asyncio.create_task(ns['_serialized_force_discharge'](call()))
    await entered.wait()
    assert ns['_local_powerwall_route_lock'].locked()
    release.set()
    assert await operation=={'success':True}
    assert not ns['_local_powerwall_route_lock'].locked()


@pytest.mark.asyncio
@pytest.mark.parametrize('native,configured,saved,expected',[(True,.2,5,20),(True,.2,35,35),(False,.2,35,0)])
async def test_native_max_tariff_discharge_keeps_real_and_configured_backup_reserve(native,configured,saved,expected):
    import ast
    tree=ast.parse((ROOT/'custom_components/power_sync/__init__.py').read_text())
    handler=next(n for n in ast.walk(tree) if isinstance(n,ast.AsyncFunctionDef) and n.name=='handle_force_discharge')
    candidates=[]
    for parent in ast.walk(handler):
        body=getattr(parent,'body',None)
        if not isinstance(body,list):continue
        for i,node in enumerate(body):
            if (isinstance(node,ast.Assign) and isinstance(node.value,ast.Constant) and node.value.value==0
                    and any(isinstance(t,ast.Name) and t.id=='native_reserve' for t in node.targets)):
                candidates.append(body[i:i+3])
    assert len(candidates)==1
    async def pulse(sites,reserve,**kw):return reserve
    ns=dict(force_discharge_state={'native_max_discharge':native},
        _local_powerwall_dispatch_config=lambda:{'optimization_backup_reserve':configured},
        saved_states={'site':{'saved_backup_reserve':saved}},active_site_configs=[('site','token','provider')],
        _tesla_force_pulse_backup_reserve=pulse)
    fn=ast.AsyncFunctionDef(name='evaluate',args=ast.arguments(posonlyargs=[],args=[],kwonlyargs=[],kw_defaults=[],defaults=[]),
        body=candidates[0]+[ast.Return(value=ast.Name(id='reserve_result',ctx=ast.Load()))],decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),'native-reserve-test','exec'),ns)
    assert await ns['evaluate']()==expected


@pytest.mark.asyncio
@pytest.mark.parametrize('direction',['charge','discharge'])
@pytest.mark.parametrize('source',['user','automation','optimizer'])
async def test_large_site_tariff_route_never_starts_renewable_local_dispatch(direction,source):
    ns,c,client,_=load_routing(tesla_control_method='auto',optimization_max_charge_w=20000,optimization_max_discharge_w=20000)
    runtime=ns['_local_powerwall_runtime']
    requested_at=__import__('time').time()-30
    runtime['battery_route_requested_at']=requested_at
    runtime['tesla_coordinator']=SimpleNamespace(_site_info_cache={'nameplate_power':20000},
                                               _site_info_last_fetch=__import__('time').monotonic())
    native=SimpleNamespace(pending=False,record=None,adapter=SimpleNamespace(guard=AsyncMock(return_value=True)),
                           start=AsyncMock(return_value={'expires_at':2000}))
    ns['entry'].options={'tesla_control_method':'auto'}
    ns['_powerwall_prefers_native_tariff'] = lambda direction='discharge': True
    ns.update(_native_max_context={'call':None},_get_native_max_charge=AsyncMock(return_value=native),
        _get_tesla_site_configs=lambda *_:[('site','token','provider')],
        _create_charge_tariff=lambda minutes:({'code':'charge'},None),
        _create_discharge_tariff=lambda minutes:({'code':'discharge'},None))
    result=await ns['_try_local_powerwall_force'](direction,call(source=source,power_w=0),15,20000)
    assert result['control_path']=='powerwall_native_tariff'
    assert native.adapter.direction==direction and native.adapter.startup_kick
    native.start.assert_awaited_once()
    assert native.start.await_args.kwargs['deadline']==requested_at+900
    client.dispatch_battery_power.assert_not_called()
    assert not c.pending


@pytest.mark.asyncio
@pytest.mark.parametrize('direction',['charge','discharge'])
async def test_exact_optimizer_watts_do_not_become_tariff_on_a_large_site(direction):
    ns,c,client,_=load_routing()
    ns['_local_powerwall_runtime']['tesla_coordinator']=SimpleNamespace(
        _site_info_cache={'nameplate_power':20000},_site_info_last_fetch=__import__('time').monotonic())
    ns['_get_tesla_site_configs']=lambda *_:[('site','token','provider')]
    result=await ns['_try_local_powerwall_force'](direction,call(source='optimizer',power_w=2000),15,2000)
    assert result['control_path']=='powerwall_local_dispatch'
    assert abs(result['battery_power_w'])<=10000
    await c.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('phase',['prepared','applying','active'])
async def test_duplicate_optimizer_native_action_retains_owner_and_original_deadline(phase):
    ns,c,client,_=load_routing()
    runtime=ns['_local_powerwall_runtime']
    runtime['tesla_coordinator']=SimpleNamespace(_site_info_cache={'nameplate_power':20000},
        _site_info_last_fetch=__import__('time').monotonic())
    expiry=__import__('time').time()+120
    native=SimpleNamespace(pending=True,record={'phase':phase,'expires_at':expiry},
        adapter=SimpleNamespace(direction='charge',guard=AsyncMock(return_value=True)),
        start=AsyncMock(),stop=AsyncMock(),shorten=AsyncMock(return_value={'expires_at':expiry}))
    ns['entry'].options={'tesla_control_method':'auto'}
    ns['_powerwall_prefers_native_tariff'] = lambda direction='discharge': True
    runtime['powerwall_native_max_charge']=native
    ns['entry'].options={'tesla_control_method':'auto'}
    ns.update(_native_max_context={'call':call(source='optimizer'),'targetless':True},
              _get_tesla_site_configs=lambda *_:[('site','token','provider')])
    if phase=='active':
        result=await ns['_try_local_powerwall_force']('charge',call(source='optimizer',power_w=0),15,20000)
        assert result['expires_at']==expiry
    else:
        with pytest.raises(RuntimeError,match='in progress'):
            await ns['_try_local_powerwall_force']('charge',call(source='optimizer',power_w=0),15,20000)
    native.start.assert_not_called();native.stop.assert_not_called()
    client.dispatch_battery_power.assert_not_called()


@pytest.mark.asyncio
async def test_native_discharge_projection_cannot_create_a_second_restore_journal(caplog):
    import logging
    ns,c,client,saved=load_routing()
    caplog.set_level(logging.DEBUG, logger=__name__)
    ns['force_discharge_state'].update(active=True,native_tariff_force=True)
    await ns['persist_force_mode_state']()
    assert saved['force_mode_state'] is None
    assert 'separate durable recovery journal' in caplog.text
    assert 'Cleared persisted force mode state' not in caplog.text


def test_stale_or_missing_capacity_does_not_authorize_full_rate_takeover():
    now=__import__('time').monotonic()
    for info,at in [(None,now),({'nameplate_power':20000},now-21601),({'nameplate_power':20000},None)]:
        energy=SimpleNamespace(_site_info_cache=info,_site_info_last_fetch=at)
        assert not prefers_native_tariff(routing_site_info(energy))
    assert prefers_native_tariff(routing_site_info(SimpleNamespace(
        _site_info_cache={'nameplate_power':10001},_site_info_last_fetch=now)))
    assert not prefers_native_tariff({'nameplate_power':True})


@pytest.mark.asyncio
async def test_stop_revokes_native_application_before_waiting_for_site_route_lock():
    ns,c,client,_=load_routing()
    events=[]
    native=SimpleNamespace(pending=True,revoke=lambda:events.append('revoke'),stop=AsyncMock())
    ns['_local_powerwall_runtime']['powerwall_native_max_charge']=native
    async with ns['_local_powerwall_route_lock']:
        task=asyncio.create_task(ns['_stop_local_powerwall_dispatch']())
        await asyncio.sleep(0)
        assert events==['revoke'] and not task.done()
    assert await task
    native.stop.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('direction',['charge','discharge'])
@pytest.mark.parametrize('source',['user','automation','optimizer'])
async def test_large_site_max_denial_never_silently_submits_capped_local_command(direction,source):
    ns,c,client,_=load_routing()
    runtime=ns['_local_powerwall_runtime']
    runtime['tesla_coordinator']=SimpleNamespace(_site_info_cache={'nameplate_power':21040},
        _site_info_last_fetch=__import__('time').monotonic())
    native=SimpleNamespace(pending=False,adapter=SimpleNamespace(guard=AsyncMock(return_value=False)),start=AsyncMock())
    ns['entry'].options={'tesla_control_method':'auto'}
    ns['_powerwall_prefers_native_tariff'] = lambda direction='discharge': True
    ns.update(_native_max_context={},_get_native_max_charge=AsyncMock(return_value=native),
        _get_tesla_site_configs=lambda *_:[('site','token','provider')])
    with pytest.raises(RuntimeError,match='Max was not started'):
        await ns['_try_local_powerwall_force'](direction,call(source=source,power_w=0),15,21000)
    native.start.assert_not_called()
    client.dispatch_battery_power.assert_not_called()


@pytest.mark.asyncio
async def test_repeated_optimizer_request_persists_shorter_native_deadline():
    ns,c,client,_=load_routing()
    runtime=ns['_local_powerwall_runtime'];now=__import__('time').time()
    runtime.update(battery_route_requested_at=now,tesla_coordinator=SimpleNamespace(
        _site_info_cache={'nameplate_power':21040},_site_info_last_fetch=__import__('time').monotonic()))
    native=SimpleNamespace(pending=True,record={'phase':'active','expires_at':now+3600},
        adapter=SimpleNamespace(direction='discharge',guard=AsyncMock(return_value=True)),
        start=AsyncMock(),stop=AsyncMock(),shorten=AsyncMock(return_value={'expires_at':now+480}))
    ns['entry'].options={'tesla_control_method':'auto'}
    ns['_powerwall_prefers_native_tariff'] = lambda direction='discharge': True
    runtime['powerwall_native_max_charge']=native
    ns['entry'].options={'tesla_control_method':'auto'}
    ns.update(_native_max_context={'targetless':True},_get_tesla_site_configs=lambda *_:[('site','token','provider')])
    result=await ns['_try_local_powerwall_force']('discharge',call(source='optimizer',power_w=0),8,21000)
    native.shorten.assert_awaited_once_with(now+480)
    assert result['expires_at']==now+480
    native.start.assert_not_called();client.dispatch_battery_power.assert_not_called()


@pytest.mark.asyncio
async def test_large_site_max_without_tariff_provider_never_uses_local_fallback():
    ns,c,client,_=load_routing()
    ns['_local_powerwall_runtime']['tesla_coordinator']=SimpleNamespace(
        _site_info_cache={'nameplate_power':21040},_site_info_last_fetch=__import__('time').monotonic())
    ns['entry'].options={'tesla_control_method':'auto'}
    ns['_powerwall_prefers_native_tariff'] = lambda direction='discharge': True
    with pytest.raises(RuntimeError,match='provider is unavailable'):
        await ns['_try_local_powerwall_force']('discharge',call(source='user',power_w=0),15,21000)
    client.dispatch_battery_power.assert_not_called()



def _install_native_discharge_owner(ns,local,client):
    """Use the durable lifetime with the existing fake hardware boundary."""
    native_spec=importlib.util.spec_from_file_location('routing_native_lifetime',ROOT/'custom_components/power_sync/powerwall_local/native_max_charge.py')
    native_module=importlib.util.module_from_spec(native_spec);native_spec.loader.exec_module(native_module)
    class Store:
        data=None
        async def async_load(self):return self.data
        async def async_save(self,value):self.data=value
    async def resume(fence,**kwargs):fence();return await client.resume_battery_operation()
    async def apply(baseline,before_write,**kwargs):
        fence=await before_write();fence();return True
    async def restore(baseline,before_write,**kwargs):
        fence=await before_write();fence();return True
    adapter=SimpleNamespace(identity='paired',direction='discharge',
        authority=lambda:not ns['_local_powerwall_runtime'].get('powerwall_local_dispatch_stopping'),
        guard=local.native_max_discharge_available,capture=AsyncMock(return_value={'original':'tariff'}),
        claim=AsyncMock(return_value=True),release=AsyncMock(return_value=True),resume=resume,apply=apply,
        restore=restore,settled=AsyncMock(return_value=True))
    session=native_module.NativeMaxChargeSession(adapter,Store())
    ns['NativeMaxChargeError']=native_module.NativeMaxChargeError
    ns['entry'].options={'tesla_control_method':'auto'}
    ns['_powerwall_prefers_native_tariff'] = lambda direction='discharge': True
    ns['_get_tesla_site_configs']=lambda *_:[('site','token','provider')]
    ns['_get_native_max_charge']=AsyncMock(return_value=session)
    ns['_native_max_context']={}
    ns['_create_discharge_tariff']=lambda duration:({'code':'force'},None)
    ns['_local_powerwall_runtime']['powerwall_native_max_charge']=session
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize('duration', [30, 47, 'Custom'])
@pytest.mark.parametrize('power', [0, 1000])
@pytest.mark.parametrize('missing_config', [False, True])
async def test_force_discharge_recovers_empty_dcq_and_requires_current_limits(duration, power, missing_config):
    from test_powerwall_v1r_teslemetry_parity import (
        _bare_client, _dcq_snapshot_reply, transport_mod,
    )
    ns, local, client, _ = load_routing()
    native = _install_native_discharge_owner(ns, local, client)
    ns['entry'].options['force_discharge_custom_duration'] = 47
    native.adapter.apply = AsyncMock(wraps=native.adapter.apply)
    gateway = _bare_client()
    replies = iter([transport_mod.TEDAPIResponse(True, None)])
    fresh_reply = _dcq_snapshot_reply()
    config_read = site_snapshot().raw['config']
    configs = iter([config_read])
    async def read_query(*args):
        # Recovery is a read-only preflight, never a replay of discharge.
        if gateway._transport.post_v1r.await_count <= 2:
            client.dispatch_battery_power.assert_not_awaited()
            native.adapter.apply.assert_not_awaited()
        return next(replies, fresh_reply)
    async def read_config(*args):
        return next(configs, None if missing_config else config_read)
    gateway._transport = SimpleNamespace(post_v1r=AsyncMock(side_effect=read_query),
        read_config=AsyncMock(side_effect=read_config))
    client.get_snapshot.side_effect = gateway.get_snapshot
    try:
        if missing_config:
            with pytest.raises((ValueError, RuntimeError)):
                await ns['handle_force_discharge'](call(source='user', power_w=power, duration=duration))
            client.dispatch_battery_power.assert_not_awaited()
            native.adapter.apply.assert_not_awaited()
            assert not local.pending and not native.pending
            assert not ns['force_discharge_state']['active']
        else:
            result = await ns['handle_force_discharge'](call(source='user', power_w=power, duration=duration))
            assert result['control_path'] == ('powerwall_native_tariff' if power == 0 else 'powerwall_local_dispatch')
            assert ns['force_discharge_state']['duration'] == (47 if duration == 'Custom' else duration)
            assert gateway._transport.post_v1r.await_count >= 2
            if power:
                client.dispatch_battery_power.assert_awaited_once()
            else:
                native.adapter.apply.assert_awaited_once()
    finally:
        await native.stop()
        await local.stop()


def install_grid_permission(client, *, disabled=True):
    """Realistic native config CAS boundary for the owned grid setting."""
    from copy import deepcopy
    snap = client.get_snapshot.return_value
    snap.raw['config']['site_info']['disallow_charge_from_grid_with_solar_installed'] = disabled
    snap.grid_charging_enabled = not disabled
    events = []
    async def read(_): return deepcopy(snap.raw['config'])
    async def write(_, updates, *, before_write, validate_config, before_submit):
        if not validate_config(deepcopy(snap.raw['config'])): return False
        await before_submit(__import__('time').time() - 1)
        before_write()
        events.append(('grid', updates.copy()))
        for key, value in updates.items(): snap.raw['config']['site_info'][key.split('.')[-1]] = value
        snap.grid_charging_enabled = not snap.raw['config']['site_info']['disallow_charge_from_grid_with_solar_installed']
        return True
    client._din = 'gateway'
    client._transport = SimpleNamespace(read_config=AsyncMock(side_effect=read), write_config=AsyncMock(side_effect=write))
    async def snapshot_read():
        snap.grid_charging_enabled = not snap.raw['config']['site_info']['disallow_charge_from_grid_with_solar_installed']
        return snap
    client.get_snapshot.side_effect = snapshot_read
    async def dispatch(*_):
        assert snap.raw['config']['site_info']['disallow_charge_from_grid_with_solar_installed'] is False
        events.append(('watts', None)); return True
    client.dispatch_battery_power.side_effect = dispatch
    async def resume(): events.append(('resume', None)); return True
    client.resume_battery_operation.side_effect = resume
    return snap, events


def test_optimizer_local_charge_enables_grid_before_watts_and_restores_after_resume():
    async def run():
        ns, c, client, saved = load_routing(optimization_allow_grid_charge=True)
        snap, events = install_grid_permission(client)
        try:
            result = await ns['handle_force_charge'](call(source='optimizer',power_w=1000,duration=1))
            assert result['success']
            assert [event[0] for event in events] == ['grid', 'watts']
            assert saved['force_mode_state']['local_grid_charge_restore']
            await c.stop()
            assert [event[0] for event in events][-2:] == ['resume', 'grid']
            assert snap.grid_charging_enabled is False
            assert saved['force_mode_state'] is None
        finally:
            await c.stop()
    asyncio.run(run())


@pytest.mark.parametrize('source', ['user', 'optimizer'])
def test_local_charge_does_not_enable_grid_without_optimizer_permission(source):
    async def run():
        ns, c, client, saved = load_routing(optimization_allow_grid_charge=source != 'optimizer')
        snap, events = install_grid_permission(client)
        try:
            with pytest.raises(RuntimeError):
                await ns['handle_force_charge'](call(source=source,power_w=1000,duration=15))
            client._transport.write_config.assert_not_awaited()
            client.dispatch_battery_power.assert_not_awaited()
            assert snap.grid_charging_enabled is False
        finally: await c.stop()
    asyncio.run(run())


def test_repeated_local_charge_keeps_original_grid_baseline_and_restart_restores_it():
    async def run():
        ns, c, client, saved = load_routing(optimization_allow_grid_charge=True)
        snap, events = install_grid_permission(client)
        try:
            for _ in range(2):
                await ns['handle_force_charge'](call(source='optimizer',power_w=1000,duration=1))
            assert len([e for e in events if e[0] == 'grid']) == 1
            record = saved['force_mode_state'].copy()
            await c._cancel_task()  # Process ends; no more hardware writes.
            recovered, next_c, next_client, next_saved = load_routing(optimization_allow_grid_charge=True)
            next_snap, next_events = install_grid_permission(next_client, disabled=False)
            recovered['persisted_force_state'] = record
            try:
                await recovered['restore_force_mode_from_persistence']()
                assert next_snap.grid_charging_enabled is False
                assert [e[0] for e in next_events] == ['resume', 'grid']
                next_client.dispatch_battery_power.assert_not_awaited()
                assert next_saved['force_mode_state'] is None
            finally: await next_c.stop()
        finally: await c.stop()
    asyncio.run(run())


def test_resume_does_not_clear_unconfirmed_grid_restore():
    async def run():
        ns, c, client, saved = load_routing(optimization_allow_grid_charge=True)
        snap, events = install_grid_permission(client)
        await ns['handle_force_charge'](call(source='optimizer',power_w=1000,duration=1))
        write = client._transport.write_config.side_effect
        client._transport.write_config.side_effect = None
        client._transport.write_config.return_value = False
        with pytest.raises(ValueError, match='grid'):
            await c.stop()
        assert c.pending and saved['force_mode_state']['local_grid_charge_restore']
        client.resume_battery_operation.assert_awaited_once()
        client._transport.write_config.side_effect = write
        await c.stop()
        assert snap.grid_charging_enabled is False and saved['force_mode_state'] is None
    asyncio.run(run())


def test_external_grid_disable_is_not_overwritten_or_reenabled():
    async def run():
        ns, c, client, saved = load_routing(optimization_allow_grid_charge=True)
        snap, events = install_grid_permission(client)
        await ns['handle_force_charge'](call(source='optimizer',power_w=1000,duration=1))
        snap.raw['config']['site_info']['disallow_charge_from_grid_with_solar_installed'] = True
        with pytest.raises(ValueError): await c._write()
        await c.stop()
        assert len([e for e in events if e[0] == 'grid']) == 1
        assert snap.grid_charging_enabled is False
    asyncio.run(run())


@pytest.mark.parametrize('stage', ['unknown_readback', 'policy_before_submit', 'initial_save', 'applied_save'])
def test_grid_setup_failure_never_issues_watts_and_restoration_survives(stage):
    async def run():
        ns, c, client, saved = load_routing(optimization_allow_grid_charge=True)
        snap, events = install_grid_permission(client)
        original_write = client._transport.write_config.side_effect
        original_persist = c.persist
        async def write(*args, **kwargs):
            if stage == 'policy_before_submit': ns['entry'].options['optimization_allow_grid_charge'] = False
            result = await original_write(*args, **kwargs)
            if stage == 'unknown_readback': client._transport.read_config.return_value = {}
            return result
        if stage == 'unknown_readback':
            read = client._transport.read_config.side_effect
            async def readback(din):
                return {} if events and events[-1][0] == 'grid' else await read(din)
            client._transport.read_config.side_effect = readback
        client._transport.write_config.side_effect = write
        async def persist():
            record = c.grid_charge_restore
            if record and ((stage == 'initial_save' and record['phase'] == 'pending') or
                           (stage == 'applied_save' and record['phase'] == 'applied')):
                raise OSError('storage unavailable')
            await original_persist()
        c.persist = persist
        try:
            with pytest.raises((RuntimeError, ValueError, OSError)):
                await ns['handle_force_charge'](call(source='optimizer',power_w=1000,duration=1))
            client.dispatch_battery_power.assert_not_awaited()
            if stage == 'unknown_readback':
                assert c.pending and saved['force_mode_state']['local_grid_charge_restore']
        finally:
            c.persist = original_persist
            client._transport.read_config.side_effect = lambda _: __import__('copy').deepcopy(snap.raw['config'])
            await c.stop()
    asyncio.run(run())


def test_uncertain_enable_cannot_clear_journal_from_early_original_readback():
    async def run():
        import time
        ns, c, client, saved = load_routing(optimization_allow_grid_charge=True)
        snap, events = install_grid_permission(client)
        async def uncertain(_, updates, *, before_write, validate_config, before_submit):
            assert validate_config(snap.raw['config'])
            await before_submit(time.time() + 12)
            before_write()
            return False  # Request can arrive later; readback still original.
        client._transport.write_config.side_effect = uncertain
        try:
            with pytest.raises(RuntimeError):
                await ns['handle_force_charge'](call(source='optimizer',power_w=1000,duration=1))
            assert c.pending and saved['force_mode_state']['local_grid_charge_restore']
            assert c._cleanup_task is not None
            client.dispatch_battery_power.assert_not_awaited()
            assert snap.grid_charging_enabled is False
            # Signed lifetime has now expired; baseline is still observed.
            c._grid_charge_restore['acceptance_until'] = 0
            await c.stop()
            assert saved['force_mode_state'] is None
        finally:
            c._grid_charge_restore and c._grid_charge_restore.update(acceptance_until=0)
            await c.stop()
    asyncio.run(run())


def test_cancellation_drains_enable_before_resume_and_never_sends_watts():
    async def run():
        ns, c, client, saved = load_routing(optimization_allow_grid_charge=True)
        snap, events = install_grid_permission(client)
        entered, finish = asyncio.Event(), asyncio.Event()
        original = client._transport.write_config.side_effect
        async def delayed(*args, **kwargs):
            result = await original(*args, **kwargs)
            if snap.grid_charging_enabled:
                entered.set(); await finish.wait()
            return result
        client._transport.write_config.side_effect = delayed
        task = asyncio.create_task(ns['handle_force_charge'](call(source='optimizer',power_w=1000,duration=1)))
        await entered.wait(); task.cancel(); await asyncio.sleep(0)
        assert not task.done() and client.resume_battery_operation.await_count == 0
        finish.set()
        with pytest.raises(asyncio.CancelledError): await task
        client.dispatch_battery_power.assert_not_awaited()
        assert snap.grid_charging_enabled is False and saved['force_mode_state'] is None
        assert [e[0] for e in events] == ['grid','resume','grid']
        await c.stop()
    asyncio.run(run())


def test_already_enabled_local_grid_setting_has_no_restore_owner():
    async def run():
        ns, c, client, saved = load_routing(optimization_allow_grid_charge=True)
        snap, events = install_grid_permission(client, disabled=False)
        await ns['handle_force_charge'](call(source='optimizer',power_w=1000,duration=1))
        assert c.grid_charge_restore is None
        await c.stop()
        assert snap.grid_charging_enabled is True
        client._transport.write_config.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.asyncio
async def test_failed_grid_restore_retries_cleanup_without_restarting_watts(monkeypatch):
    ns, c, client, saved = load_routing(optimization_allow_grid_charge=True)
    snap, events = install_grid_permission(client)
    await ns['handle_force_charge'](call(source='optimizer',power_w=1000,duration=1))
    write = client._transport.write_config.side_effect
    client._transport.write_config.side_effect = None
    client._transport.write_config.return_value = False
    with pytest.raises(ValueError): await c.stop()
    assert c.pending and c._cleanup_task is not None
    client._transport.write_config.side_effect = write
    async def immediate(_): pass
    monkeypatch.setattr(module, 'asyncio', SimpleNamespace(**(vars(asyncio) | {'sleep':immediate})))
    await c._cleanup_task
    assert snap.grid_charging_enabled is False
    assert client.dispatch_battery_power.await_count == 1
    assert not c.pending and saved['force_mode_state'] is None


@pytest.mark.parametrize('bad', [{'din':'another-gateway'}, {'original':False}, {'acceptance_until':float('nan')}])
def test_invalid_grid_recovery_never_guesses_a_baseline(bad):
    async def run():
        ns, c, client, saved = load_routing()
        _, _events = install_grid_permission(client, disabled=False)
        record = {'owner':'old-owner','din':'gateway','original':True,'phase':'applied','acceptance_until':0} | bad
        ns['persisted_force_state'] = {'mode':'charge','powerwall_local_dispatch':True,
                                      'local_grid_charge_restore':record}
        with pytest.raises(ValueError): await ns['restore_force_mode_from_persistence']()
        client.dispatch_battery_power.assert_not_awaited()
        client._transport.write_config.assert_not_awaited()
    asyncio.run(run())


def test_manual_takeover_cannot_inherit_optimizer_grid_permission():
    async def run():
        ns, c, client, saved = load_routing(optimization_allow_grid_charge=True)
        snap, events = install_grid_permission(client)
        await ns['handle_force_charge'](call(source='optimizer',power_w=1000,duration=1))
        with pytest.raises(RuntimeError):
            await ns['handle_force_charge'](call(source='user',power_w=500,duration=15))
        assert client.dispatch_battery_power.await_count == 1
        assert snap.grid_charging_enabled is False
        assert saved['force_mode_state'] is None
        await c.stop()
    asyncio.run(run())


def test_replaced_setup_cannot_restore_grid_setting_from_old_controller():
    async def run():
        ns, c, client, saved = load_routing(optimization_allow_grid_charge=True)
        snap, events = install_grid_permission(client)
        await ns['handle_force_charge'](call(source='optimizer',power_w=1000,duration=1))
        c._cleanup_allowed = lambda: False
        with pytest.raises(ValueError): await c.stop()
        assert c.pending and c.grid_charge_restore
        assert len([e for e in events if e[0] == 'grid']) == 1
        c._cleanup_allowed = lambda: True
        await c.stop()
        assert snap.grid_charging_enabled is False
    asyncio.run(run())


@pytest.mark.parametrize('when', ['initial_read', 'native_write_read'])
def test_grid_enable_cannot_drop_a_known_native_import_limit(when):
    async def run():
        ns, c, client, saved = load_routing(optimization_allow_grid_charge=True)
        snap, events = install_grid_permission(client)
        c._site_limits = (4000, None)
        snap.raw['config']['site_info']['max_site_meter_power_ac'] = 4
        if when == 'initial_read':
            del snap.raw['config']['site_info']['max_site_meter_power_ac']
        else:
            original = client._transport.write_config.side_effect
            async def missing_config(*args, **kwargs):
                del snap.raw['config']['site_info']['max_site_meter_power_ac']
                return await original(*args, **kwargs)
            client._transport.write_config.side_effect = missing_config
        try:
            with pytest.raises(RuntimeError):
                await ns['handle_force_charge'](call(source='optimizer',power_w=1000,duration=1))
            assert not [e for e in events if e[0] == 'grid']
            client.dispatch_battery_power.assert_not_awaited()
        finally: await c.stop()
    asyncio.run(run())


@pytest.mark.asyncio
@pytest.mark.parametrize('blocker', ['signed_write', 'resume', 'gateway'])
async def test_tariff_cloud_recovery_cannot_bypass_local_release_requirements(blocker):
    ns, c, client, saved = load_routing(optimization_allow_grid_charge=True)
    snap, _ = install_grid_permission(client)
    resume = client.resume_battery_operation.side_effect
    client._transport.read_config.side_effect = lambda _: None
    with pytest.raises(RuntimeError):
        await ns['handle_force_charge'](call(source='optimizer', power_w=1000, duration=1))
    callback = AsyncMock(return_value=True)
    if blocker == 'signed_write': c._grid_charge_restore['acceptance_until'] = __import__('time').time() + 30
    elif blocker == 'resume':
        c._resume_confirmed = False
        client.resume_battery_operation.side_effect = None
        client.resume_battery_operation.return_value = False
    else: client._din = 'other-gateway'
    try:
        with pytest.raises(ValueError): await c.stop(restore_grid_charging=callback)
        callback.assert_not_awaited()
        assert c.pending and saved['force_mode_state']['local_grid_charge_restore']
        client.dispatch_battery_power.assert_not_awaited()
    finally:
        client._din = 'gateway'
        client.resume_battery_operation.side_effect = resume
        client.resume_battery_operation.return_value = True
        c._grid_charge_restore['acceptance_until'] = 0
        client._transport.read_config.side_effect = lambda _: snap.raw['config']
        await c.stop()


@pytest.mark.asyncio
async def test_cancelled_tariff_cloud_recovery_drains_without_dropping_journal():
    ns, c, client, saved = load_routing(optimization_allow_grid_charge=True)
    snap, _ = install_grid_permission(client)
    client._transport.read_config.side_effect = lambda _: None
    with pytest.raises(RuntimeError):
        await ns['handle_force_charge'](call(source='optimizer', power_w=1000, duration=1))
    entered, finish = asyncio.Event(), asyncio.Event()
    async def cloud(record, current):
        assert current()
        entered.set()
        await finish.wait()
        return True
    task = asyncio.create_task(c.stop(restore_grid_charging=cloud))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and c.pending and c.grid_charge_restore
    finish.set()
    with pytest.raises(asyncio.CancelledError): await task
    assert c.pending and saved['force_mode_state']['local_grid_charge_restore']
    client.dispatch_battery_power.assert_not_awaited()
    client._transport.read_config.side_effect = lambda _: snap.raw['config']
    await c.stop()


@pytest.mark.parametrize('permission,configured,reason', [
    (None, True, 'grid_charging_permission_unknown'),
    (False, False, 'configured_grid_charging_disabled'),
    (False, None, 'configured_grid_charging_disabled'),
])
@pytest.mark.parametrize('grid_target', [None, 3000])
@pytest.mark.parametrize('source', ['optimizer', 'user'])
def test_grid_permission_refusal_notifies_only_requested_optimizer_charge(permission, configured, reason, grid_target, source):
    from unittest.mock import Mock
    async def run():
        ns, c, client, saved = load_routing(optimization_allow_grid_charge=configured,
                                          optimization_spread_import_enabled=True)
        alert = ns['update_native_charge_alert'] = Mock()
        client.get_snapshot.return_value.grid_charging_enabled = permission
        request = dict(source=source, power_w=1000, duration=1)
        if grid_target is not None:
            request['grid_import_target_w'] = grid_target
        try:
            with pytest.raises(RuntimeError):
                await ns['handle_force_charge'](call(**request))
            if source == 'optimizer':
                alert.assert_called_once_with(ns['hass'], 'entry', reason)
            else:
                alert.assert_not_called()
            client.dispatch_battery_power.assert_not_awaited()
            assert not ns['_local_powerwall_runtime'].get('powerwall_dispatch_fallback_pending')
        finally:
            await c.stop()
    asyncio.run(run())


def test_external_grid_disable_on_optimizer_retry_notifies_and_never_fights_edit():
    from unittest.mock import Mock
    async def run():
        ns, c, client, saved = load_routing(optimization_allow_grid_charge=True)
        snap, events = install_grid_permission(client)
        alert = ns['update_native_charge_alert'] = Mock()
        try:
            await ns['handle_force_charge'](call(source='optimizer', power_w=1000, duration=1))
            alert.assert_called_once_with(ns['hass'], 'entry', None)
            alert.reset_mock()
            client.dispatch_battery_power.reset_mock()
            client._transport.write_config.reset_mock()
            snap.raw['config']['site_info']['disallow_charge_from_grid_with_solar_installed'] = True
            with pytest.raises(RuntimeError):
                await ns['handle_force_charge'](call(source='optimizer', power_w=1000, duration=1))
            alert.assert_called_once_with(ns['hass'], 'entry', 'grid_charging_not_permitted')
            client.dispatch_battery_power.assert_not_awaited()
            client._transport.write_config.assert_not_awaited()
        finally:
            await c.stop()
    asyncio.run(run())


def test_grid_permission_alert_clears_only_after_local_charge_acceptance():
    from unittest.mock import Mock
    async def run():
        ns, c, client, saved = load_routing(optimization_allow_grid_charge=True)
        alert = ns['update_native_charge_alert'] = Mock()
        client.get_snapshot.return_value.grid_charging_enabled = None
        try:
            with pytest.raises(RuntimeError):
                await ns['handle_force_charge'](call(source='optimizer', power_w=1000, duration=1))
            client.get_snapshot.return_value.grid_charging_enabled = True
            await ns['handle_force_discharge'](call(source='optimizer', power_w=1000, duration=1))
            alert.assert_called_once_with(ns['hass'], 'entry', 'grid_charging_permission_unknown')
            await ns['handle_force_charge'](call(source='optimizer', power_w=1000, duration=1))
            assert alert.call_args.args == (ns['hass'], 'entry', None)
            assert client.dispatch_battery_power.await_count == 2
        finally:
            await c.stop()
    asyncio.run(run())


def test_disabled_grid_charging_without_request_or_under_monitoring_is_quiet():
    from unittest.mock import Mock
    async def run():
        ns, c, client, saved = load_routing(monitored=True, optimization_allow_grid_charge=False)
        alert = ns['update_native_charge_alert'] = Mock()
        alert.assert_not_called()
        await ns['handle_force_charge'](call(source='optimizer', power_w=1000, duration=1))
        alert.assert_not_called()
        client.dispatch_battery_power.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['soc', 'transport'])
def test_other_local_charge_failures_do_not_claim_grid_permission_refusal(failure):
    from unittest.mock import Mock
    async def run():
        ns, c, client, saved = load_routing(optimization_allow_grid_charge=True)
        alert = ns['update_native_charge_alert'] = Mock()
        if failure == 'soc':
            client.get_snapshot.return_value.soc = 100
        else:
            client.dispatch_battery_power.return_value = False
        try:
            with pytest.raises(RuntimeError):
                await ns['handle_force_charge'](call(source='optimizer', power_w=1000, duration=1))
            alert.assert_not_called()
            assert bool(ns['_local_powerwall_runtime'].get('powerwall_dispatch_fallback_pending')) == (failure == 'transport')
        finally:
            await c.stop()
    asyncio.run(run())


@pytest.mark.parametrize('initially_disabled', [False, True])
def test_local_charge_handles_false_omission_through_grid_restore(initially_disabled):
    from copy import deepcopy
    from test_powerwall_grid_permission_encoding import normalization
    async def run():
        ns, controller, client, saved = load_routing(optimization_allow_grid_charge=True)
        snap, events = install_grid_permission(client, disabled=initially_disabled)
        field = 'disallow_charge_from_grid_with_solar_installed'
        snap.raw['config'].update(default_real_mode='self_consumption',battery_blocks=[{}],meters=[{}],strategy={})
        snap.raw['config']['site_info'].update(backup_reserve_percent=20,customer_preferred_export_rule='battery_ok')
        if not initially_disabled: snap.raw['config']['site_info'].pop(field)
        original = deepcopy(snap.raw['config'])
        client.native_grid_charging_reading = lambda config: normalization.native_grid_charging_reading(config)
        async def snapshot_read():
            reading = client.native_grid_charging_reading(snap.raw['config'])
            snap.grid_charging_enabled = not reading.disallowed if type(reading.disallowed) is bool else None
            return snap
        client.get_snapshot.side_effect = snapshot_read
        async def sparse_write(_, updates, *, before_write, validate_config, before_submit):
            if not validate_config(deepcopy(snap.raw['config'])): return False
            await before_submit(__import__('time').time()-1)
            before_write()
            events.append(('grid',updates.copy()))
            value = updates['site_info.'+field]
            if value: snap.raw['config']['site_info'][field] = True
            else: snap.raw['config']['site_info'].pop(field,None)
            return True
        client._transport.write_config.side_effect = sparse_write
        async def dispatch(*_):
            assert client.native_grid_charging_reading(snap.raw['config']).disallowed is False
            events.append(('watts',None)); return True
        client.dispatch_battery_power.side_effect = dispatch
        try:
            result = await ns['handle_force_charge'](call(source='optimizer' if initially_disabled else 'user',power_w=1000,duration=1))
            assert result['success']
            await controller.stop()
            assert snap.raw['config'] == original
            assert not controller.pending and saved['force_mode_state'] is None
            assert [kind for kind,_ in events] == (['grid','watts','resume','grid'] if initially_disabled else ['watts','resume'])
        finally:
            await controller.stop()
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['charge', 'discharge'])
def test_manual_local_request_publishes_pending_before_hardware_acknowledgement(mode):
    async def run():
        ns, c, client, saved = load_routing()
        signals = []
        ns['async_dispatcher_send'] = lambda *args: signals.append(args)
        entered, proceed = asyncio.Event(), asyncio.Event()
        original = client.dispatch_battery_power.side_effect
        async def blocked(*args, **kwargs):
            entered.set()
            await proceed.wait()
            if original:
                return await original(*args, **kwargs)
            return True
        client.dispatch_battery_power.side_effect = blocked
        task = asyncio.create_task(ns[f'handle_force_{mode}'](call(source='user', power_w=500, duration=15)))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            assert not task.done()
            assert ns[f'force_{mode}_state']['command_status'] == 'pending'
            assert any(args[1] == f'power_sync_force_{mode}_state'
                       and args[2].get('command_status') == 'pending' for args in signals)
            proceed.set()
            await task
            assert ns[f'force_{mode}_state']['command_status'] == 'accepted'
        finally:
            proceed.set()
            await asyncio.gather(task, return_exceptions=True)
            await c.stop()
    asyncio.run(run())
