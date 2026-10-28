"""Tesla preference defaults and per-direction pre-plan actuator routing."""
import importlib
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1] / "custom_components/power_sync"
PREFIX = "tesla_method_probe"
for name, path in [(PREFIX, ROOT), (PREFIX + ".powerwall_local", ROOT / "powerwall_local")]:
    package = ModuleType(name); package.__path__ = [str(path)]; sys.modules[name] = package
policy = importlib.import_module(PREFIX + ".powerwall_local.control_method")


def config(**extra):
    return {"tesla_control_method": "auto", "powerwall_local_paired": True,
        "powerwall_local_ip": "192.168.1.2", "optimization_max_charge_w": 15000,
        "optimization_max_discharge_w": 15000, "optimization_max_grid_export_w": 5000,
        "optimization_max_grid_import_w": 15000, **extra}


def snapshot(load=2000, solar=0, battery=0):
    return SimpleNamespace(load_w=load, solar_w=solar, battery_w=battery,
        grid_w=load-solar-battery, grid_status="SystemGridConnected",
        raw={"config": {"site_info": {"min_site_meter_power_ac": -5, "max_site_meter_power_ac": 15}}})


def test_missing_or_legacy_invalid_value_defaults_to_tariff():
    for raw in ({}, {"tesla_control_method": "old"}, {"tesla_control_method": None}):
        assert policy.control_method(raw) == "tariff"
    for raw in (None, "unknown", True, [], {}):
        with pytest.raises(ValueError): policy.validate_method(raw)


@pytest.mark.parametrize("method", ["tariff", "local"])
def test_explicit_method_does_not_depend_on_inverter_nameplate(method):
    assert policy.select_route(config(tesla_control_method=method), "discharge") == method


def test_three_powerwall_site_can_use_local_export_and_native_charge():
    state=snapshot()
    assert policy.select_route(config(), "discharge", state, local_available=True) == "local"
    assert policy.select_route(config(), "charge", state, local_available=True) == "tariff"


@pytest.mark.parametrize("load,solar,expected", [(4999,0,"local"),(5000,0,"local"),(5001,0,"tariff"),
    (8000,0,"tariff"),(2000,12000,"tariff")])
def test_auto_checks_full_inverter_envelope(load,solar,expected):
    assert policy.select_route(config(),"discharge",snapshot(load,solar),local_available=True)==expected


def test_auto_honors_a_lower_directional_gateway_limit():
    assert policy.select_route(config(), "discharge", snapshot(), local_available=True, local_limit_w=6000)=="tariff"


def test_auto_missing_stale_or_unbalanced_evidence_uses_tariff():
    assert policy.select_route(config(), "discharge", snapshot(), local_available=False)=="tariff"
    state=snapshot();state.grid_w=99999
    assert policy.select_route(config(), "discharge", state, local_available=True)=="tariff"
    runtime={"powerwall_local": {"coordinator": SimpleNamespace(data=snapshot(),last_success_monotonic=time.monotonic()-91)}}
    assert policy.runtime_route(runtime,config(),"discharge")=="tariff"


def test_auto_keeps_a_native_episode_stable_until_it_finishes():
    runtime={"force_discharge_state": {"active": True,"native_tariff_force": True},
        "powerwall_local": {"coordinator": SimpleNamespace(data=snapshot(),last_success_monotonic=time.monotonic())}}
    assert policy.runtime_route(runtime,config(),"discharge")=="tariff"
    runtime["force_discharge_state"]["active"]=False
    assert policy.runtime_route(runtime,config(),"discharge")=="local"


@pytest.mark.asyncio
async def test_preference_change_stops_local_action_before_persistence_and_does_not_replay():
    from test_powerwall_battery_dispatch import load_routing
    from unittest.mock import Mock
    ns,local,client,_=load_routing()
    entry=ns['entry'];events=[]
    ns['hass'].config_entries=SimpleNamespace(async_get_entry=lambda key:entry,
        async_update_entry=lambda current,**values:(events.append('persist'),setattr(current,'options',values['options'])))
    ns['async_dispatcher_send']=Mock()
    await local.start('discharge',2000,120)
    assert local.pending
    await ns['_set_tesla_control_method']('tariff')
    assert entry.options['tesla_control_method']=='tariff'
    assert not local.pending
    client.resume_battery_operation.assert_awaited()
    assert client.dispatch_battery_power.await_count==1
    assert events==['persist']


@pytest.mark.asyncio
async def test_failed_cleanup_keeps_previous_preference_and_retains_owner():
    from test_powerwall_battery_dispatch import load_routing
    from unittest.mock import Mock
    ns,local,client,_=load_routing()
    entry=ns['entry'];save=Mock()
    ns['hass'].config_entries=SimpleNamespace(async_get_entry=lambda key:entry,async_update_entry=save)
    await local.start('discharge',2000,120)
    client.resume_battery_operation.return_value=False
    with pytest.raises(Exception,match='resume unconfirmed'):
        await ns['_set_tesla_control_method']('tariff')
    save.assert_not_called()
    assert policy.control_method({**entry.data,**entry.options})=='auto'
    assert local.pending
    assert not ns['_local_powerwall_runtime'].get('tesla_control_method_changing')
    client.resume_battery_operation.return_value=True
    await local.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('confirmed', [True, False])
async def test_tariff_change_recovers_unknown_local_permission_through_confirmed_cloud(confirmed):
    from test_powerwall_battery_dispatch import load_routing, install_grid_permission, call
    from unittest.mock import AsyncMock, Mock
    ns, local, client, saved = load_routing(optimization_allow_grid_charge=True)
    snap, events = install_grid_permission(client)
    entry = ns['entry']
    entry.data.update(tesla_control_method='local', tesla_energy_site_id='site', powerwall_local_din='gateway')
    update = Mock(side_effect=lambda current, **kw: setattr(current, 'options', kw['options']))
    ns['hass'].config_entries = SimpleNamespace(async_get_entry=lambda _: entry, async_update_entry=update)
    ns['_get_tesla_site_configs'] = lambda *_: [('site', 'token', 'fleet')]
    async def cloud(*args, **kw):
        assert local._resume_confirmed and local.pending and local.grid_charge_restore
        assert kw['gateway_din'] == 'gateway' and kw['is_current']()
        assert not update.called
        events.append(('cloud_restore', None))
        return confirmed
    ns['async_restore_tesla_grid_charging_confirmed'] = AsyncMock(side_effect=cloud)
    ns['async_get_clientsession'] = lambda _: object()
    client._transport.read_config.side_effect = lambda _: {'site_info': {}}
    try:
        with pytest.raises(RuntimeError):
            await ns['handle_force_charge'](call(source='optimizer', power_w=1000, duration=1))
        client.dispatch_battery_power.assert_not_awaited()
        if confirmed:
            await ns['_set_tesla_control_method']('tariff')
            assert entry.options['tesla_control_method'] == 'tariff'
            assert not local.pending and saved['force_mode_state'] is None
            assert [e[0] for e in events] == ['grid', 'resume', 'cloud_restore']
        else:
            with pytest.raises(RuntimeError, match='cleanup remains pending'):
                await ns['_set_tesla_control_method']('tariff')
            update.assert_not_called()
            assert local.pending and saved['force_mode_state']['local_grid_charge_restore']
        ns['async_restore_tesla_grid_charging_confirmed'].assert_awaited_once()
        client.dispatch_battery_power.assert_not_awaited()
    finally:
        client._transport.read_config.side_effect = lambda _: snap.raw['config']
        await local.stop()


@pytest.mark.asyncio
async def test_tariff_change_keeps_successful_local_restore_independent_of_cloud():
    from test_powerwall_battery_dispatch import load_routing, install_grid_permission, call
    from unittest.mock import AsyncMock, Mock
    ns, local, client, saved = load_routing(optimization_allow_grid_charge=True)
    snap, events = install_grid_permission(client)
    entry = ns['entry']
    entry.data['tesla_control_method'] = 'local'
    ns['hass'].config_entries = SimpleNamespace(async_get_entry=lambda _: entry,
        async_update_entry=lambda current, **kw: setattr(current, 'options', kw['options']))
    cloud = ns['_restore_local_grid_charging_via_cloud'] = AsyncMock(side_effect=AssertionError('cloud not required'))
    await ns['handle_force_charge'](call(source='optimizer', power_w=1000, duration=1))
    await ns['_set_tesla_control_method']('tariff')
    assert entry.options['tesla_control_method'] == 'tariff' and not local.pending
    assert snap.grid_charging_enabled is False and saved['force_mode_state'] is None
    assert [e[0] for e in events] == ['grid', 'watts', 'resume', 'grid']
    cloud.assert_not_awaited()


@pytest.mark.asyncio
async def test_tariff_change_recovers_existing_v1519_journal_after_restart(monkeypatch):
    from test_powerwall_battery_dispatch import load_routing, install_grid_permission
    from test_tesla_grid_control import tesla_grid_control, _Session, _Response, _Clock, _recovery_info
    from functools import partial
    ns, local, client, saved = load_routing(optimization_allow_grid_charge=True)
    snap, _ = install_grid_permission(client, disabled=False)
    entry = ns['entry']
    entry.data.update(tesla_control_method='local', tesla_energy_site_id='site', powerwall_local_din='gateway')
    ns['hass'].config_entries = SimpleNamespace(async_get_entry=lambda _: entry,
        async_update_entry=lambda current, **kw: setattr(current, 'options', kw['options']))
    ns['_get_tesla_site_configs'] = lambda *_: [('site', 'token', 'fleet')]
    clock = _Clock()
    original = tesla_grid_control.async_set_tesla_grid_charging_confirmed
    monkeypatch.setattr(tesla_grid_control, 'async_set_tesla_grid_charging_confirmed',
                        partial(original, sleep=clock.sleep, monotonic=clock.monotonic))
    session = _Session(posts=[_Response(200, {'response': {'result': True}})],
                       gets=[_recovery_info(None), _recovery_info(True)])
    ns['async_get_clientsession'] = lambda _: session
    ns['async_restore_tesla_grid_charging_confirmed'] = tesla_grid_control.async_restore_tesla_grid_charging_confirmed
    ns['persisted_force_state'] = {'mode': 'charge', 'powerwall_local_dispatch': True,
        'local_grid_charge_restore': {'owner': 'v1519-owner', 'din': 'gateway',
                                     'original': True, 'phase': 'restoring', 'acceptance_until': 0}}
    client._transport.read_config.side_effect = lambda _: None
    with pytest.raises(RuntimeError, match='restoration unconfirmed'):
        await ns['restore_force_mode_from_persistence']()
    assert not session.get_calls and not session.post_calls
    assert local.pending and saved['force_mode_state']['local_grid_charge_restore']['owner'] == 'v1519-owner'
    await ns['_set_tesla_control_method']('tariff')
    assert entry.options['tesla_control_method'] == 'tariff' and not local.pending
    assert saved['force_mode_state'] is None
    client.dispatch_battery_power.assert_not_awaited()
    assert len(session.post_calls) == 1
    assert session.post_calls[0]['json'] == {'disallow_charge_from_grid_with_solar_installed': True}


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['site', 'din', 'setup', 'command'])
async def test_cloud_recovery_does_not_clear_owner_after_identity_changes(change):
    from test_powerwall_battery_dispatch import load_routing, install_grid_permission, call
    from unittest.mock import Mock
    ns, local, client, saved = load_routing(optimization_allow_grid_charge=True)
    snap, _ = install_grid_permission(client)
    entry = ns['entry']
    entry.data.update(tesla_control_method='local', tesla_energy_site_id='site', powerwall_local_din='gateway')
    update = Mock()
    ns['hass'].config_entries = SimpleNamespace(async_get_entry=lambda _: entry, async_update_entry=update)
    ns['_get_tesla_site_configs'] = lambda *_: [('site', 'token', 'fleet')]
    ns['async_get_clientsession'] = lambda _: object()
    async def cloud(*args, **kw):
        assert kw['is_current']()
        if change == 'site': entry.data['tesla_energy_site_id'] = 'another-site'
        elif change == 'din': entry.data['powerwall_local_din'] = 'another-gateway'
        elif change == 'setup': ns['hass'].data['power_sync']['entry'] = {}
        else: ns['_command_generation'][0] += 1
        assert not kw['is_current']()
        return True
    ns['async_restore_tesla_grid_charging_confirmed'] = cloud
    client._transport.read_config.side_effect = lambda _: None
    try:
        with pytest.raises(RuntimeError):
            await ns['handle_force_charge'](call(source='optimizer', power_w=1000, duration=1))
        with pytest.raises(RuntimeError, match='restoration unconfirmed'):
            await ns['_set_tesla_control_method']('tariff')
        update.assert_not_called()
        assert local.pending and local.grid_charge_restore
        client.dispatch_battery_power.assert_not_awaited()
    finally:
        ns['hass'].data['power_sync']['entry'] = ns['_local_powerwall_runtime']
        client._transport.read_config.side_effect = lambda _: snap.raw['config']
        await local.stop()


@pytest.mark.asyncio
async def test_local_preference_never_enables_a_tariff_fallback():
    from test_powerwall_battery_dispatch import load_routing,call
    ns,local,client,_=load_routing(tesla_control_method='local')
    client.dispatch_battery_power.return_value=False
    with pytest.raises(RuntimeError,match='tariff fallback is disabled'):
        await ns['_try_local_powerwall_force']('discharge',call(source='user',power_w=500),5,500)
    assert not local.pending
    assert not ns['_local_powerwall_runtime'].get('powerwall_dispatch_fallback_pending')


def view_fixture(entries,body=None,change=None):
    import ast
    from unittest.mock import AsyncMock
    class View:
        def json(self,payload,status_code=200):return status_code,payload
    source=ROOT/'powerwall_local/views.py'
    node=next(n for n in ast.parse(source.read_text()).body if isinstance(n,ast.ClassDef) and n.name=='PowerwallControlMethodView')
    class StripImports(ast.NodeTransformer):
        def visit_ImportFrom(self,node):return ast.copy_location(ast.Pass(),node)
    namespace=dict(HomeAssistantView=View,HomeAssistantError=RuntimeError,DOMAIN='power_sync',METHODS=policy.METHODS,
        control_method=policy.control_method,runtime_route=policy.runtime_route,validate_method=policy.validate_method,
        set_control_method=change or AsyncMock(),local_battery_dispatch_configured=lambda config:bool(config.get('powerwall_local_paired')))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[StripImports().visit(node)],type_ignores=[])),str(source),'exec'),namespace)
    hass=SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda domain:entries),data={})
    return namespace[node.name](hass),SimpleNamespace(query={},json=AsyncMock(return_value=body)),namespace['set_control_method']


@pytest.mark.asyncio
async def test_api_is_tesla_only_and_never_picks_the_wrong_entry():
    a=SimpleNamespace(entry_id='a',data={'battery_system':'tesla'},options={})
    b=SimpleNamespace(entry_id='b',data={'battery_system':'tesla'},options={})
    other=SimpleNamespace(entry_id='other',data={'battery_system':'goodwe'},options={})
    view,request,change=view_fixture([a,b,other])
    assert (await view.get(request))[0]==404
    request.query={'entry_id':'other'}
    assert (await view.get(request))[0]==404
    request.query={'entry_id':'b'}
    status,body=await view.get(request)
    assert status==200 and body['method']=='tariff'
    change.assert_not_awaited()


@pytest.mark.asyncio
async def test_api_only_returns_saved_value_after_successful_cleanup():
    from unittest.mock import AsyncMock
    entry=SimpleNamespace(entry_id='a',data={'battery_system':'tesla','powerwall_local_paired':True},options={'tesla_control_method':'local'})
    change=AsyncMock(side_effect=RuntimeError('cleanup pending'))
    view,request,_=view_fixture([entry],{'method':'tariff'},change)
    status,body=await view.post(request)
    assert status==409 and body['success'] is False
    assert entry.options['tesla_control_method']=='local'
    for value in (None,[],{'method':'unknown'},{'method':True}):
        request.json=AsyncMock(return_value=value)
        assert (await view.post(request))[0]==400
    assert change.await_count==1



def test_unpaired_tariff_plans_keep_the_existing_tariff_service_contract():
    import ast
    source=ROOT/'optimization/coordinator.py'
    cls=next(node for node in ast.parse(source.read_text()).body if isinstance(node,ast.ClassDef) and node.name=='OptimizationCoordinator')
    method=next(node for node in cls.body if isinstance(node,ast.FunctionDef) and node.name=='_powerwall_service_power')
    class StripImports(ast.NodeTransformer):
        def visit_ImportFrom(self,node):return ast.copy_location(ast.Pass(),node)
    from importlib import import_module
    host=import_module(PREFIX+'.powerwall_host')
    namespace={'local_battery_dispatch_configured':host.local_battery_dispatch_configured}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[StripImports().visit(method)],type_ignores=[])),str(source),'exec'),namespace)
    obj=SimpleNamespace(battery_system='tesla',_entry=SimpleNamespace(data={},options={}),_powerwall_plan_uses_tariff=lambda direction:True)
    assert namespace['_powerwall_service_power'](obj,'discharge',15000)==15000
    obj._entry.data.update(powerwall_local_paired=True,powerwall_local_ip='gateway.local')
    assert namespace['_powerwall_service_power'](obj,'discharge',15000)==0


@pytest.mark.asyncio
@pytest.mark.parametrize('direction', ['charge', 'discharge'])
@pytest.mark.parametrize('source', ['user', 'optimizer'])
@pytest.mark.parametrize('preference', ['tariff', None])
async def test_tariff_control_reaches_cloud_without_reading_unreachable_gateway(direction, source, preference):
    from test_powerwall_battery_dispatch import load_routing, call
    from unittest.mock import AsyncMock
    ns, local, client, _ = load_routing(tesla_control_method='tariff')
    if preference is None:
        ns['entry'].data.pop('tesla_control_method')
    client.get_snapshot.side_effect = OSError('Powerwall LAN unreachable')
    native = SimpleNamespace(pending=False, adapter=SimpleNamespace(
        guard=AsyncMock(side_effect=OSError('Powerwall LAN unreachable'))))
    ns.update(_get_tesla_site_configs=lambda *_: [('site', 'token', 'provider')],
              _get_native_max_charge=AsyncMock(return_value=native), _native_max_context={})
    request = call(source=source, power_w=0)
    # None passes admission on to the existing confirmed cloud tariff path.
    if direction == 'charge':
        assert await ns['_try_native_max_charge'](request, 15, 9999999999) is None
    assert await ns['_try_local_powerwall_force'](direction, request, 15, 0) is None
    ns['_get_native_max_charge'].assert_not_awaited()
    client.get_snapshot.assert_not_awaited()
    client.dispatch_battery_power.assert_not_awaited()
    assert not local.pending


@pytest.mark.asyncio
async def test_tariff_preference_keeps_an_explicit_watt_target_local():
    from test_powerwall_battery_dispatch import load_routing, call
    ns, local, client, _ = load_routing(tesla_control_method='tariff')
    result = await ns['_try_local_powerwall_force']('charge', call(power_w=500), 15, 500)
    assert result['control_path'] == 'powerwall_local_dispatch'
    client.dispatch_battery_power.assert_awaited_once()
    await local.stop()


@pytest.mark.asyncio
async def test_tariff_control_cannot_abandon_uncertain_local_cleanup():
    from test_powerwall_battery_dispatch import load_routing, call
    ns, local, client, _ = load_routing(tesla_control_method='tariff')
    await local.start('charge', 500, 120)
    client.resume_battery_operation.return_value = False
    with pytest.raises(RuntimeError, match='cleanup remains pending'):
        await ns['_try_local_powerwall_force']('charge', call(power_w=0), 15, 0)
    assert local.pending
    client.resume_battery_operation.return_value = True
    await local.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('reservation', ['armed', 'unobserved', 'managed', 'native_max'])
async def test_cloud_tariff_keeps_shared_ev_and_native_ownership_guards(reservation):
    from test_powerwall_battery_dispatch import load_routing, call
    from unittest.mock import AsyncMock
    ns, local, client, _ = load_routing(tesla_control_method='tariff')
    ns['site_identity'] = lambda entry: 'site'
    ns['hass'].data['power_sync_native_rate_budget'] = SimpleNamespace(
        load=AsyncMock(),
        native_max_claim=lambda site: {'token': 'owner'} if reservation == 'native_max' else None,
        site_armed=lambda site: reservation == 'armed',
        unobserved_w=lambda site: 1000 if reservation == 'unobserved' else 0,
        managed={'car': {'site': 'site'}} if reservation == 'managed' else {},
    )
    with pytest.raises(RuntimeError, match='headroom|owns this site'):
        await ns['_try_local_powerwall_force']('charge', call(power_w=0), 15, 0)
    client.get_snapshot.assert_not_awaited()
    client.dispatch_battery_power.assert_not_awaited()
    assert not local.pending


@pytest.mark.asyncio
@pytest.mark.parametrize('direction', ['charge', 'discharge'])
async def test_tariff_waits_for_durable_native_ownership_before_falling_through(direction):
    from test_powerwall_battery_dispatch import load_routing, call
    from unittest.mock import AsyncMock
    ns, _, client, _ = load_routing(tesla_control_method='tariff')
    budget = SimpleNamespace(loaded=False)
    async def load():
        budget.loaded = True
    budget.load = AsyncMock(side_effect=load)
    budget.native_max_claim = lambda site: {'token': 'persisted-owner'} if budget.loaded else None
    ns['site_identity'] = lambda entry: 'site'
    ns['hass'].data['power_sync_native_rate_budget'] = budget
    with pytest.raises(RuntimeError, match='owns this site'):
        await ns['_try_local_powerwall_force'](direction, call(power_w=0), 15, 0)
    budget.load.assert_awaited_once()
    client.dispatch_battery_power.assert_not_awaited()


@pytest.mark.asyncio
async def test_tariff_cleanup_cannot_continue_from_a_replaced_setup():
    from test_powerwall_battery_dispatch import load_routing, call
    from unittest.mock import AsyncMock
    ns, _, client, _ = load_routing(tesla_control_method='tariff')
    async def replace():
        ns['hass'].data['power_sync']['entry'] = {}
    ns['_stop_local_powerwall_dispatch'] = AsyncMock(side_effect=replace)
    with pytest.raises(RuntimeError, match='authorization changed'):
        await ns['_try_local_powerwall_force']('charge', call(power_w=0), 15, 0)
    client.dispatch_battery_power.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('setting', ['operation_mode', 'grid_charging'])
@pytest.mark.parametrize('accepted', [True, False])
async def test_tariff_prerequisite_settings_use_confirmed_cloud_writes(setting, accepted):
    import ast
    import logging
    from unittest.mock import AsyncMock
    from test_tesla_grid_control import tesla_grid_control
    path = ROOT / '__init__.py'
    setup = next(n for n in ast.parse(path.read_text()).body if getattr(n, 'name', '') == 'async_setup_entry')
    node = next(n for n in setup.body if getattr(n, 'name', '') == '_tesla_force_apply_' + setting)
    class StripImports(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            return ast.copy_location(ast.Pass(), node)
    local = AsyncMock(side_effect=AssertionError('Tariff must not attempt local V1R'))
    cloud_mode = AsyncMock(return_value=accepted)
    cloud_grid = AsyncMock(return_value=tesla_grid_control.TeslaGridWriteOutcome(
        tesla_grid_control.TeslaGridWriteStatus.APPLIED if accepted
        else tesla_grid_control.TeslaGridWriteStatus.REJECTED))
    ns = dict(entry=SimpleNamespace(data={'tesla_control_method': 'local'}, options={'tesla_control_method': 'tariff'}),
        async_get_clientsession=lambda hass: object(), hass=object(),
        dispatch_powerwall_write=local, _tesla_force_set_operation_mode=cloud_mode,
        async_set_tesla_grid_charging_confirmed=cloud_grid,
        TeslaGridWriteStatus=tesla_grid_control.TeslaGridWriteStatus,
        get_tesla_api_base_url=lambda provider, base: 'https://tesla.test',
        CONF_FLEET_API_BASE_URL='base', _LOGGER=logging.getLogger(__name__))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[StripImports().visit(node)], type_ignores=[])),
                 str(path), 'exec', flags=__import__('__future__').annotations.compiler_flag), ns)
    result = await ns[node.name]([('site', 'token', 'provider')],
        'autonomous' if setting == 'operation_mode' else True, reason='force charge')
    assert result['confirmed_sites'] == (['site'] if accepted else [])
    assert result['failed_sites'] == ([] if accepted else ['site'])
    local.assert_not_awaited()
    (cloud_mode if setting == 'operation_mode' else cloud_grid).assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('local_active', [False, True])
@pytest.mark.parametrize('restored', [False, True])
async def test_preference_change_finishes_pending_cloud_restore_after_local_stop(local_active, restored):
    """Ticket 409: local resume does not complete an older cloud restore."""
    from test_powerwall_battery_dispatch import load_routing
    from unittest.mock import AsyncMock, Mock
    ns, local, client, _ = load_routing(tesla_control_method='local')
    entry = ns['entry']
    runtime = ns['_local_powerwall_runtime']
    marker = {'reason': 'grid charging restore failed', '_skip_backup_reserve_restore': True}
    runtime['pending_tesla_restore'] = marker
    runtime['optimizer_force_restart_restore_pending'] = True
    save = Mock(side_effect=lambda current, **values: setattr(current, 'options', values['options']))
    ns['hass'].config_entries = SimpleNamespace(async_get_entry=lambda key: entry, async_update_entry=save)
    ns['ServiceCall'] = lambda domain, service, data: SimpleNamespace(data=data)
    events = []
    async def restore(call):
        assert not local.pending
        assert call.data['_force_restore'] is True
        assert call.data['_confirm_restore'] is True
        assert call.data['_skip_backup_reserve_restore'] is True
        events.append('restore')
        if restored:
            runtime['pending_tesla_restore'] = None
            runtime['optimizer_force_restart_restore_pending'] = False
        # A normal service return alone does not prove completion.
    ns['handle_restore_normal'] = AsyncMock(side_effect=restore)
    if local_active:
        await local.start('discharge', 2000, 120)
    if restored:
        await ns['_set_tesla_control_method']('tariff')
        assert entry.options['tesla_control_method'] == 'tariff'
        save.assert_called_once()
        assert runtime['pending_tesla_restore'] is None
    else:
        with pytest.raises(RuntimeError, match='cleanup remains pending'):
            await ns['_set_tesla_control_method']('tariff')
        save.assert_not_called()
        assert policy.control_method({**entry.data, **entry.options}) == 'local'
        assert runtime['pending_tesla_restore'] == marker
    assert events == ['restore']
    assert not runtime.get('tesla_control_method_changing')
    if local_active:
        client.dispatch_battery_power.assert_awaited_once()
    else:
        client.dispatch_battery_power.assert_not_awaited()
