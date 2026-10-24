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
