"""Execute the real HA adapter with controlled lifecycle, registry and storage."""
from __future__ import annotations

import ast
import asyncio
import copy
from contextlib import suppress
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import web
import pytest

from test_support_contract import ROOT, NAME, p, c, s, h, frame


class MemoryStore:
    def __init__(self, *args, **kwargs): self.value = {}
    async def async_load(self): return copy.deepcopy(self.value)
    async def async_save(self, value): self.value = copy.deepcopy(value)
    async def async_remove(self): self.value = {}


def adapter():
    source = ast.parse((ROOT / 'ha.py').read_text())
    nodes = [node for node in source.body if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef))]
    class View: pass
    ns = {'__package__': NAME, '__file__':str(ROOT/'ha.py'), 'asyncio':asyncio,'suppress':suppress,'datetime':datetime,'timezone':timezone,
        'json':json,'math':math,'time':time,'Path':Path,'web':web,'HomeAssistantView':View,'Store':MemoryStore,
        'snapshot':c.snapshot,'select':c.select,'action_event':c.action_event,'Recorder':h.Recorder,'Session':s.Session,
        'BROKER':p.BROKER,'CATEGORIES':p.CATEGORIES,'MAX_RESULT':p.MAX_RESULT,'ROLES':p.ROLES,'SupportError':p.SupportError,
        'digest':p.digest,'encode':p.encode,'reference':p.reference,'number':p.number,'DATA':'power_sync_support','CONTRACT_DIGEST':p.CONTRACT_DIGEST}
    from collections import deque
    ns['deque'] = deque
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(ROOT/'ha.py'),'exec'),ns)
    return ns


@pytest.fixture
def runtime(monkeypatch):
    registry = SimpleNamespace(entities={})
    helper = ModuleType('homeassistant.helpers'); er=ModuleType('homeassistant.helpers.entity_registry');er.async_get=lambda hass:registry;helper.entity_registry=er
    monkeypatch.setitem(sys.modules,'homeassistant.helpers',helper)
    monkeypatch.setitem(sys.modules,'homeassistant.helpers.entity_registry',er)
    notifications=ModuleType('homeassistant.components.persistent_notification');notifications.async_create=lambda *a,**k:None;notifications.async_dismiss=lambda *a,**k:None
    components=ModuleType('homeassistant.components');components.persistent_notification=notifications
    monkeypatch.setitem(sys.modules,'homeassistant.components',components)
    monkeypatch.setitem(sys.modules,'homeassistant.components.persistent_notification',notifications)
    namespace=adapter()
    force={'active':True,'type':'charge','expires_at':datetime(2000,1,1,tzinfo=timezone.utc),'power_w':250}
    class Optimizer:
        def __init__(self):
            self._optimizer_force_state=force
            self._config=SimpleNamespace(allow_grid_charge=False)
            self._current_schedule=SimpleNamespace(actions=[SimpleNamespace(timestamp=datetime.now(timezone.utc),action='charge',power_w=500,reason='CANARY_SECRET')])
            self._enabled=True;self._last_update_time=datetime.now(timezone.utc)
        def get_api_data(self): raise AssertionError('impure formatter must not execute')
        def _get_active_force_state(self): self._optimizer_force_state.clear();raise AssertionError('impure getter')
        async def async_request_refresh(self): raise AssertionError('device refresh forbidden')
    states={}
    unsubscribed=[]
    def listen(name, callback): return lambda:unsubscribed.append(name)
    hass=SimpleNamespace(data={'power_sync':{'entry-one':{'optimization_coordinator':Optimizer()}}},states=SimpleNamespace(get=states.get),
        services=SimpleNamespace(async_call=AsyncMock(side_effect=AssertionError('service call forbidden'))),bus=SimpleNamespace(async_listen=listen),
        async_create_task=asyncio.create_task,http=SimpleNamespace(register_view=lambda view:None),
        async_add_executor_job=AsyncMock(side_effect=lambda func,*args:func(*args)))
    entry=SimpleNamespace(entry_id='entry-one',data={'monitoring_mode':True,'api_key':'CANARY_SECRET'},options={})
    rt=namespace['SupportRuntime'](hass,entry);rt.version='2.12.1657'
    hass.data['power_sync_support']={'entry-one':rt}
    registry.entities['sensor.example_battery']=SimpleNamespace(config_entry_id='entry-one',disabled_by=None,unique_id='entry-one_battery_power',entity_id='sensor.example_battery')
    registry.entities['sensor.other']=SimpleNamespace(config_entry_id='entry-two',disabled_by=None,unique_id='other_grid_power',entity_id='sensor.other')
    states['sensor.example_battery']=SimpleNamespace(state='300',attributes={'unit_of_measurement':'W','token':'CANARY_SECRET'},last_reported=datetime.now(timezone.utc),last_updated=datetime.now(timezone.utc))
    return rt,registry,force,namespace,unsubscribed


def test_real_adapter_does_not_mutate_expired_force_state_or_poll_devices(runtime):
    rt,_,force,_,_=runtime; before=copy.deepcopy(force)
    result=rt.capture()
    assert force==before
    assert result['entities'][0]['value']==300
    assert len(result['entities'])==1,'other entry excluded'
    assert result['optimizer'][0]['state']['monitoring_mode'] is True
    assert result['control_trace'][0]['state']['active'] is True
    assert result['control_trace'][0]['state']['expires_at']==before['expires_at'].timestamp()
    assert 'CANARY_SECRET' not in json.dumps(result)
    rt.hass.services.async_call.assert_not_called()
    c.validate_evidence(result)


@pytest.mark.asyncio
async def test_mapping_change_closes_permission_and_history_obeys_category_scope(runtime):
    rt,registry,_,_,_=runtime;rt.capture()
    rt.session.begin('owner',p.reference(rt.entry.entry_id+rt.mapping_digest),['history','entities'],time.time()-600,3600)
    rt.recorder.append(rt.frame)
    result=await rt.collect({'category':'history','start':time.time()-600,'end':time.time()})
    assert 'optimizer' not in result['records'][0] and 'system' not in result['records'][0]
    registry.entities['sensor.new']=SimpleNamespace(config_entry_id='entry-one',disabled_by=None,unique_id='entry-one_grid_power',entity_id='sensor.new')
    rt.capture();assert rt.session.key is None and rt.session.reason=='mapping_changed'


@pytest.mark.asyncio
async def test_persistent_recording_does_not_persist_session_or_ha_tokens(runtime):
    rt,_,_,_,_=runtime
    rt.recording=rt.persistent=True;rt.capture();rt.recorder.append(rt.frame)
    rt.session.begin('owner','entry_reference_00001',['entities'],time.time(),3600);rt.session.token='HA_TRANSPORT_CANARY'
    await rt.save();raw=json.dumps(rt.store.value)
    assert 'HA_TRANSPORT_CANARY' not in raw and 'private_key' not in raw and 'public_key' not in raw
    restored=rt.recorder.dump();assert restored and c.restore_frame(restored[0]) is not None


@pytest.mark.asyncio
async def test_offline_revoke_closes_locally_first_and_retains_delete_only_retry(runtime):
    rt,_,_,_,_=runtime
    rt.session.begin('owner','entry_reference_00001',['entities'],time.time(),3600);rt.session.token='HA_TOKEN'
    rt.revocations=[{'session_id':rt.session.id,'revoke_token':'DELETE_ONLY','expires_at':time.time()+3600}]
    async def offline(*args,**kwargs):
        assert rt.session.key is None and rt.session.token is None
        raise OSError('offline')
    rt.request=offline
    await rt.revoke()
    assert rt.session.state=='closed'
    assert rt.error=='stopped_locally_central_confirmation_pending'
    assert rt.store.value['revocations'][0]['revoke_token']=='DELETE_ONLY'
    assert 'HA_TOKEN' not in json.dumps(rt.store.value)


@pytest.mark.asyncio
async def test_restart_restores_recording_but_never_support_access_and_unsubscribes(runtime):
    rt,_,_,_,unsubscribed=runtime
    rt.capture()
    rt.store.value={'recording':True,'persistent':True,'mapping_digest':rt.mapping_digest,'records':[rt.frame,{'token':'CANARY_SECRET'}]}
    await rt.setup()
    assert rt.session.state=='closed' and rt.session.key is None
    assert rt.recording and rt.persistent
    assert rt.recorder.dump(), 'restore really ran'
    assert 'CANARY_SECRET' not in json.dumps(rt.recorder.dump())
    await rt.stop()
    assert rt.task.done()
    assert sorted(unsubscribed)==['power_sync_ev_command','power_sync_optimizer_action']


def test_existing_event_observer_filters_entry_and_never_copies_identifiers(runtime):
    rt,_,_,_,_=runtime;rt.recording=True
    data={'entry_id':'entry-other','timestamp':datetime.now(timezone.utc).isoformat(),'command':'stop','success':True,'vehicle_id':'CANARY_VIN'}
    rt.observe(SimpleNamespace(event_type='power_sync_ev_command',data=data));assert not rt.recorder.dump()
    data['entry_id']='entry-one';rt.observe(SimpleNamespace(event_type='power_sync_ev_command',data=data))
    raw=json.dumps(rt.recorder.dump());assert 'CANARY_VIN' not in raw and 'command_result' in raw


@pytest.mark.asyncio
async def test_local_views_are_admin_only_and_not_a_prompt_endpoint(runtime):
    rt,_,_,ns,_=runtime;view=ns['SupportView'](rt.hass)
    class Request(dict):
        def __init__(self,user,body=None): super().__init__(hass_user=user);self.content=SimpleNamespace(read=AsyncMock(side_effect=[json.dumps(body).encode(),b'']))
    assert (await view.get(Request(SimpleNamespace(is_admin=False)))).status==403
    assert (await view.post(Request(SimpleNamespace(is_admin=False),{}))).status==403
    assert (await view.post(Request(SimpleNamespace(is_admin=True,id='owner'),{'entry_id':'entry-one','action':'ask','prompt':'question'}))).status==409
    assert (await view.post(Request(SimpleNamespace(is_admin=True,id='owner'),{'entry_id':'entry-two','action':'bundle'}))).status==409


def test_solve_observation_owns_actual_input_values_and_is_bounded(runtime):
    rt,_,_,_,_=runtime;rt.capture();rt.recording=True
    action=SimpleNamespace(timestamp=datetime.now(timezone.utc),action='charge',power_w=500)
    schedule=SimpleNamespace(actions=[action]);config=SimpleNamespace(backup_reserve=.2,api_key='CANARY')
    result=SimpleNamespace(solver_used='highs',feasible=True,solve_time_s=.1)
    prices=[.25];solar=[2.0];load=[1.0]
    rt.publish_plan(schedule,prices,prices,solar,load,config,result)
    prices[0]=99;action.power_w=999;config.backup_reserve=.9
    rt.drain_plans()
    projection=rt.frame['optimizer'][0]
    assert projection['input_provenance']=='solve_commit'
    assert projection['schedule'][0]['import_price']==.25
    assert projection['schedule'][0]['power_w']==500
    assert 'CANARY' not in json.dumps(rt.recorder.dump())
    for _ in range(10): rt.publish_plan(schedule,prices,prices,solar,load,config,result)
    assert len(rt.pending_plans)==4 and rt.recorder.dropped==6


def test_source_solve_hook_failure_is_expendable_and_disabled_has_no_work():
    from conftest import load_source_node
    source=ROOT.parent/'optimization/coordinator.py'
    function=load_source_node(source,'_run_optimization')
    assignments=[node for node in ast.walk(function) if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='support_observer' for t in node.targets)]
    assert len(assignments)==1
    guard=next(node for node in ast.walk(function) if isinstance(node,ast.If) and isinstance(node.test,ast.Compare) and isinstance(node.test.left,ast.Name) and node.test.left.id=='support_observer')
    program=compile(ast.fix_missing_locations(ast.Module(body=[assignments[0],guard],type_ignores=[])),str(source),'exec')
    for observer in (None,lambda *args:(_ for _ in ()).throw(RuntimeError('recorder failed'))):
        namespace={'self':SimpleNamespace(_support_plan_observer=observer,_config=SimpleNamespace()),'result':SimpleNamespace(schedule=[]),
            'import_prices':[],'export_prices':[],'solar_forecast':[],'load_forecast':[]}
        exec(program,namespace)


@pytest.mark.asyncio
async def test_archive_lost_response_and_restart_preserve_all_delete_only_credentials(runtime):
    rt,_,_,_,_=runtime;rt.capture()
    rt.session.begin('owner','entry_reference_00001',['history','entities'],time.time()-600,3600)
    rt.session.state='active';rt.session.token='READ_TOKEN_CANARY'
    sid=rt.session.id
    rt.archives={'older-session':{'session_id':'older-session','revoke_token':'OLD_DELETE','expires_at':time.time()+600,'shared':True},
        sid:{'session_id':sid,'revoke_token':'NEW_DELETE','expires_at':time.time()+90000,'pending':False,'shared':False}}
    async def lost(path,body=None,**kwargs):
        assert rt.store.value['archives'][sid]['revoke_token']=='NEW_DELETE','persisted before upload'
        assert rt.store.value['archives']['older-session']['revoke_token']=='OLD_DELETE'
        raise OSError('response lost after commit')
    rt.request=lost
    with pytest.raises(OSError): await rt.share_archive('owner',600)
    assert rt.archives[sid]['shared'] is True
    assert 'READ_TOKEN_CANARY' not in json.dumps(rt.store.value)
    await rt.revoke_archive()
    assert all(x['pending'] for x in rt.archives.values())
    assert rt.error=='archive_revocation_pending'
    rt.request=AsyncMock(return_value={'revoked':True,'receipt':[{'event':'archive_revoked_locally'}]})
    await rt.drain_archives()
    assert not rt.archives
    assert rt.store.value['receipt'][0]['event']=='archive_revoked_locally'


@pytest.mark.asyncio
async def test_recorder_import_needs_both_history_and_entities_approval(runtime):
    rt,*_=runtime;rt.capture()
    rt.session.begin('owner','entry_reference_00001',['history'],time.time()-600,3600,include_recorder=True)
    rt.recorder_import=AsyncMock(side_effect=AssertionError('unapproved read'))
    with pytest.raises(p.SupportError,match='category_denied'):
        await rt.collect({'category':'history','source':'ha_recorder','start':time.time()-600,'end':time.time()})
    rt.recorder_import.assert_not_called()


@pytest.mark.asyncio
async def test_confirmation_requires_displayed_descriptor_digest(runtime):
    rt,*_=runtime;rt.descriptor={'actor':'investigator'};rt.policy={'policy_digest':'abc'}
    rt.request=AsyncMock(side_effect=AssertionError('must not send activation'))
    with pytest.raises(p.SupportError,match='invitation_changed'):
        await rt.confirm('owner','older-screen')
    rt.request.assert_not_called()


def test_registry_exact_identity_excludes_ev_suffix_collision(runtime):
    rt,registry,*_=runtime
    registry.entities['sensor.ev_battery']=SimpleNamespace(config_entry_id='entry-one',disabled_by=None,unique_id='entry-one_ev_battery_level',entity_id='sensor.ev_battery')
    assert 'battery_level' not in rt.resolve_manifest()


@pytest.mark.asyncio
async def test_recorder_import_is_bounded_and_excludes_changed_identity(runtime, monkeypatch):
    rt,registry,*_=runtime;rt.capture()
    start=time.time()-600;end=time.time()
    row=registry.entities['sensor.example_battery'];row.created_at=datetime.fromtimestamp(start-100,timezone.utc);row.modified_at=row.created_at
    recorder=ModuleType('homeassistant.components.recorder');history=ModuleType('homeassistant.components.recorder.history')
    calls=[]
    def read(hass,lo,hi,**kwargs):
        calls.append(kwargs)
        return {kwargs['entity_id']:[SimpleNamespace(state='12.5',attributes={'unit_of_measurement':'W','secret':'CANARY'},last_updated=datetime.fromtimestamp(start+1,timezone.utc))]*1500}
    history.state_changes_during_period=read;recorder.get_instance=lambda hass:hass
    monkeypatch.setitem(sys.modules,'homeassistant.components.recorder',recorder)
    monkeypatch.setitem(sys.modules,'homeassistant.components.recorder.history',history)
    result=await rt.recorder_import({'start':start,'end':end})
    assert len(result['records'])<=1000
    assert calls[0]['limit']==1000 and calls[0]['include_start_time_state'] is False
    assert 'CANARY' not in json.dumps(result)
    calls.clear();row.modified_at=datetime.now(timezone.utc)
    result=await rt.recorder_import({'start':start,'end':end})
    assert result['availability']=='unsupported' and calls==[]


@pytest.mark.asyncio
async def test_simultaneous_starts_on_two_entries_only_register_one(runtime):
    rt,_,_,ns,_=runtime
    other=ns['SupportRuntime'](rt.hass,SimpleNamespace(entry_id='entry-two',data={},options={}))
    other.version=rt.version;rt.hass.data['power_sync_support']['entry-two']=other
    waiting=0;both=asyncio.Event();register_release=asyncio.Event();registrations=[]
    async def request(path,body=None,**kwargs):
        nonlocal waiting
        if path.endswith('/policy'):
            waiting+=1
            if waiting==2: both.set()
            await both.wait()
            return {'disclosure':'synthetic','policy_digest':'policy'}
        registrations.append(body)
        await register_release.wait()
        raise OSError('synthetic transport stop')
    rt.request=other.request=request
    args={'categories':['entities'],'duration':3600,'history_seconds':0,'invitation_token':'synthetic'}
    tasks=[asyncio.create_task(value.start('owner',args)) for value in (rt,other)]
    await both.wait();await asyncio.sleep(0)
    assert len(registrations)==1
    register_release.set()
    results=await asyncio.gather(*tasks,return_exceptions=True)
    assert any(isinstance(x,p.SupportError) and str(x)=='another_session_active' for x in results)
    for value in (rt,other):value.session.close()


@pytest.mark.asyncio
async def test_disabled_default_diagnostics_do_not_rewrite_storage(runtime):
    rt,*_=runtime;rt.store.async_save=AsyncMock()
    await rt.drain_archives()
    rt.store.async_save.assert_not_called()


@pytest.mark.asyncio
async def test_setup_failure_cleanup_does_not_overwrite_unrestored_history(runtime):
    rt,*_=runtime;rt.recording=rt.persistent=True;rt.store.value={'records':['original']}
    await rt.stop()
    assert rt.store.value['records']==['original']


@pytest.mark.asyncio
async def test_never_shared_archive_slots_do_not_survive_session_close(runtime):
    rt,*_=runtime
    rt.archives={str(i):{'session_id':str(i),'revoke_token':'DELETE','expires_at':time.time()+90000,'shared':False,'pending':False} for i in range(8)}
    rt.request=AsyncMock(side_effect=AssertionError('no remote archive ever existed'))
    await rt.drain_archives()
    assert rt.archives=={}
