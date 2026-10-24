"""Native Max preserves permissions and restores only its own exact settings."""
import asyncio
from copy import deepcopy
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1] / 'custom_components/power_sync'


@pytest.fixture
def setup(monkeypatch):
    package = 'native_max_adapter_probe'
    for name, path in [(package, ROOT), (package+'.powerwall_local', ROOT/'powerwall_local')]:
        p = ModuleType(name); p.__path__ = [str(path)]; monkeypatch.setitem(sys.modules,name,p)
    m = importlib.import_module(package+'.powerwall_local.native_max_adapter')
    budget_module = importlib.import_module(package+'.native_rate_budget')
    session_module = importlib.import_module(package+'.powerwall_local.native_max_charge')
    state = SimpleNamespace(config={'default_real_mode':'self_consumption', 'site_info':{
        'backup_reserve_percent':5, 'max_site_meter_power_ac':14.9,
        'min_site_meter_power_ac':-14.9,
        'customer_preferred_export_rule':'pv_only'}}, tariff={'name':'real tariff','code':'REAL'},
        identity='paired', policy=True, grid='SystemGridConnected', soc=50,
        events=[], at=1000., upload_ok=True, local_ok=True,
        before_validate=None,before_send=None, battery_w=0, load_w=1000, solar_w=0,
        kick_ok=True, kick_error=None, resume_ok=True, after_kick=None)
    original = deepcopy(state.config), deepcopy(state.tariff)
    settings = {'optimization_max_charge_w':14700, 'optimization_max_grid_import_w':15000,
                'optimization_grid_charge_soc_cap':1, 'optimization_allow_grid_charge':True}
    class Store:
        data=None
        async def async_load(self): return deepcopy(self.data)
        async def async_save(self,data): self.data=deepcopy(data)
    class Transport:
        async def read_config(self,din): return deepcopy(state.config)
        async def write_config(self,din,updates,*,before_write,validate_config,before_submit):
            await asyncio.sleep(0)
            forcing=updates.get('default_real_mode')=='autonomous' and updates.get('site_info.backup_reserve_percent')==100
            if forcing and state.before_validate: state.before_validate()
            if not validate_config(deepcopy(state.config)): return False
            if forcing and state.before_send: state.before_send()
            await before_submit(state.at-2)
            before_write()
            state.events.append(('config',deepcopy(updates)))
            if not state.local_ok: return False
            for key,value in updates.items():
                if key.startswith('site_info.'): state.config['site_info'][key[10:]]=value
                else: state.config[key]=value
            return True
    class Client:
        _transport=Transport(); _din='gateway'; local_access_enabled=True
        async def get_snapshot(self):
            return SimpleNamespace(raw={'config':deepcopy(state.config)},soc=state.soc,grid_status=state.grid,
                battery_w=state.battery_w, load_w=state.load_w, solar_w=state.solar_w,
                grid_w=state.load_w-state.solar_w-state.battery_w,
                backup_reserve_percent=state.config['site_info']['backup_reserve_percent'])
        async def resume_battery_operation(self,*,before_write,before_submit):
            await before_submit(state.at-2)
            before_write(); state.events.append(('resume',None)); return state.resume_ok
        async def dispatch_battery_power(self,watts,seconds,*,before_write,before_submit):
            await before_submit(state.at-2)
            before_write(); state.events.append(('kick',(watts,seconds)))
            if state.after_kick: state.after_kick()
            if state.kick_error: raise state.kick_error
            return state.kick_ok
    async def read_tariff(): return deepcopy(state.tariff)
    async def upload(tariff,before_write):
        fence=await before_write(); fence(); state.events.append(('tariff',deepcopy(tariff)))
        if state.upload_ok: state.tariff=deepcopy(tariff)
        return state.upload_ok
    budget=budget_module.SharedRateBudget(Store(),clock=lambda:state.at)
    adapter=m.NativeMaxChargeAdapter(client=Client(),identity='paired',
        current_identity=lambda:state.identity,policy=lambda:state.policy,config=lambda:settings,
        read_tariff=read_tariff,upload=upload,restorable=lambda t:t.get('code')=='REAL',
        forcing_tariff={'name':'Force Charge','code':'UNIQUE_FORCE'},budget=budget,
        entry_id='battery',site='site',monotonic=lambda:state.at,settle_interval=0,clock=lambda:state.at)
    store=Store()
    session=session_module.NativeMaxChargeSession(adapter,store,
        clock=lambda:state.at,monotonic=lambda:state.at)
    return SimpleNamespace(m=m,state=state,original=original,settings=settings,budget=budget,
                           adapter=adapter,session=session,store=store)


@pytest.mark.asyncio
async def test_manual_max_uses_native_settings_and_restores_exact_original(setup):
    s=setup
    assert (await s.session.start(120))['active']
    assert s.state.config['default_real_mode']=='autonomous'
    assert s.state.config['site_info']['backup_reserve_percent']==100
    assert s.state.events[0][0]=='resume'
    assert s.state.config['site_info']['customer_preferred_export_rule']=='pv_only'
    assert 'disallow_charge_from_grid_with_solar_installed' not in s.state.config['site_info']
    assert s.settings['optimization_max_charge_w']==14700
    assert await s.session.stop()
    assert (s.state.config,s.state.tariff)==s.original
    assert not s.budget.native_max_claim('site')


@pytest.mark.asyncio
async def test_signed_solar_standby_draw_is_balanced_without_rejecting_capture_or_settlement(setup):
    s=setup;s.state.solar_w=-3;s.state.battery_w=1670;s.state.load_w=1668
    assert (await s.session.start(120))['active']
    assert s.store.data['baseline']['settle_charge_w']==0
    assert await s.session.stop()
    assert s.store.data is None and not s.budget.native_max_claim('site')


@pytest.mark.asyncio
@pytest.mark.parametrize('denial', ['permission','site_cap','zero_charge','soc','grid','policy','native_ev','pending_managed'])
async def test_current_policy_denial_prevents_every_forcing_write(setup,denial):
    s=setup
    if denial=='permission': s.state.config['site_info']['disallow_charge_from_grid_with_solar_installed']=True
    if denial=='site_cap': s.settings['optimization_max_grid_import_w']=14000
    if denial=='zero_charge': s.settings['optimization_max_charge_w']=0
    if denial=='soc': s.state.soc=100
    if denial=='grid': s.state.grid='SystemIslandedActive'
    if denial=='policy': s.state.policy=False
    if denial=='native_ev': s.budget.armed_sites.add('site')
    if denial=='pending_managed':
        await s.budget.admit_managed('VIN',{'site':'site','identity':'provider:car','reserved_w':0},{},lambda:True)
    with pytest.raises(Exception,match='policy denied'):
        await s.session.start(120)
    assert not s.state.events and not s.session.pending


@pytest.mark.asyncio
async def test_foreign_tariff_is_preserved_while_owned_mode_and_reserve_restore(setup):
    s=setup;await s.session.start(120)
    s.state.tariff={'code':'FOREIGN','name':'new external tariff'}
    assert await s.session.stop()
    assert s.state.tariff['code']=='FOREIGN'
    assert s.state.config==s.original[0]
    assert not s.budget.native_max_claim('site')


@pytest.mark.asyncio
async def test_restore_remains_allowed_after_monitoring_or_licence_permission_loss(setup):
    s=setup;await s.session.start(120);s.state.policy=False
    assert await s.session.stop()
    assert (s.state.config,s.state.tariff)==s.original


@pytest.mark.asyncio
async def test_pairing_changed_during_restore_read_cannot_write(setup):
    s=setup;await s.session.start(120)
    original=s.adapter.read_tariff
    changed_at=[]
    async def replaced():
        result=await original();changed_at.append(len(s.state.events));s.state.identity='different';return result
    s.adapter.read_tariff=replaced
    count=len(s.state.events)
    with pytest.raises(s.m.NativeMaxChargeError):await s.session.stop()
    assert len(s.state.events)==changed_at[0] and s.session.pending
    await s.session._cancel_watch()
    s.state.identity='paired';s.adapter.read_tariff=original
    assert await s.session.stop()


@pytest.mark.asyncio
async def test_external_reserve_change_during_apply_read_is_not_overwritten(setup):
    s=setup
    upload=s.adapter.upload
    async def interleave(*args):
        result=await upload(*args)
        s.state.config['site_info']['backup_reserve_percent']=30
        return result
    s.adapter.upload=interleave
    with pytest.raises(s.m.NativeMaxChargeError): await s.session.start(120)
    assert s.state.config['site_info']['backup_reserve_percent']==30
    assert not any(event[0]=='config' for event in s.state.events)
    assert not s.session.pending and not s.budget.native_max_claim('site')
    assert s.state.tariff==s.original[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('moment,change', [(moment,change)
    for moment in ('before_validate','before_send')
    for change in ('native_cap','software_cap','charge_disabled','permission','soc_cap')
    if (moment,change)!=('before_send','native_cap')])
async def test_config_submission_revalidates_actual_native_and_current_software_policy(setup,moment,change):
    s=setup
    def changed():
        if change=='native_cap':s.state.config['site_info']['max_site_meter_power_ac']=20
        if change=='software_cap':s.settings['optimization_max_grid_import_w']=10000
        if change=='charge_disabled':s.settings['optimization_max_charge_w']=0
        if change=='permission':s.settings['optimization_allow_grid_charge']=False
        if change=='soc_cap':s.settings['optimization_grid_charge_soc_cap']=.4
    # Native config changes after the actual RMW read are rejected by its
    # optimistic hash; this fixture models the fresh snapshot at validation.
    setattr(s.state,moment,changed)
    with pytest.raises(s.m.NativeMaxChargeError):await s.session.start(120)
    assert not any(kind=='config' and update.get('site_info.backup_reserve_percent')==100
                   for kind,update in s.state.events)
    assert not s.session.pending and s.state.tariff==s.original[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('field,external', [('reserve',30),('mode','backup')])
async def test_foreign_config_field_does_not_strand_owned_tariff_or_other_field(setup,field,external):
    s=setup;await s.session.start(120)
    if field=='reserve':s.state.config['site_info']['backup_reserve_percent']=external
    else:s.state.config['default_real_mode']=external
    assert await s.session.stop()
    assert s.state.tariff==s.original[1]
    assert s.state.config['site_info']['backup_reserve_percent']==(external if field=='reserve' else 5)
    assert s.state.config['default_real_mode']==(external if field=='mode' else 'self_consumption')


@pytest.mark.asyncio
async def test_tariff_outage_does_not_prevent_removing_owned_reserve_and_mode(setup):
    s=setup;await s.session.start(120)
    read=s.adapter.read_tariff
    async def offline():raise OSError('offline')
    s.adapter.read_tariff=offline
    with pytest.raises(s.m.NativeMaxChargeError):await s.session.stop()
    assert s.state.config==s.original[0]
    assert s.session.pending and s.budget.native_max_claim('site')
    await s.session._cancel_watch();s.adapter.read_tariff=read
    assert await s.session.stop()
    assert s.state.tariff==s.original[1]


@pytest.mark.asyncio
async def test_autonomous_baseline_disarm_survives_restart_and_finishes_after_tariff_outage(setup):
    s=setup;s.state.config['default_real_mode']='autonomous'
    await s.session.start(120);s.state.upload_ok=False
    with pytest.raises(s.m.NativeMaxChargeError):await s.session.stop()
    assert s.state.config['default_real_mode']=='self_consumption'
    assert s.state.config['site_info']['backup_reserve_percent']==5
    assert s.store.data['cleanup']['mode_disarmed']
    await s.session._cancel_watch();s.state.upload_ok=True
    # The failed Fleet attempt was ambiguous. Its late baseline readback,
    # rather than a retry or elapsed delay, resolves that submitted write.
    assert s.store.data['tariff_intent']=='baseline'
    s.state.tariff=deepcopy(s.original[1])
    recovered=type(s.session)(s.adapter,s.store,clock=lambda:s.state.at,monotonic=lambda:s.state.at)
    assert await recovered.recover()
    assert s.state.config['default_real_mode']=='autonomous' and s.state.tariff==s.original[1]


@pytest.mark.asyncio
async def test_cancellation_waits_for_submitted_tariff_before_cleanup_can_release_site(setup):
    s=setup;entered=asyncio.Event();finish=asyncio.Event()
    original=s.adapter.upload
    async def delayed(tariff,before_write):
        if tariff.get('code')!='UNIQUE_FORCE':return await original(tariff,before_write)
        fence=await before_write();fence();entered.set();await finish.wait()
        # A server-side write may complete after cancellation of its caller.
        s.state.tariff=deepcopy(tariff)
        return True
    s.adapter.upload=delayed
    start=asyncio.create_task(s.session.start(120));await entered.wait()
    stop=asyncio.create_task(s.session.stop());await asyncio.sleep(0);await asyncio.sleep(0)
    assert not stop.done() and s.session.pending and s.budget.native_max_claim('site')
    finish.set()
    with pytest.raises(s.m.NativeMaxChargeError):await start
    assert await stop
    assert s.state.tariff==s.original[1] and s.state.config==s.original[0]
    assert not s.budget.native_max_claim('site')


@pytest.mark.asyncio
async def test_http_timeout_does_not_expire_signed_config_before_late_application(setup):
    s=setup; submitted=asyncio.Event(); late=[]
    original=s.adapter.client._transport.write_config
    async def timeout(din,updates,*,before_write,validate_config,before_submit):
        if updates.get('site_info.backup_reserve_percent')!=100:
            return await original(din,updates,before_write=before_write,
                                  validate_config=validate_config,before_submit=before_submit)
        assert validate_config(deepcopy(s.state.config))
        await before_submit(s.state.at+.05)
        before_write();submitted.set()
        async def remotely_delayed():
            await asyncio.sleep(.02)
            assert s.store.data['local_acceptance_until']==1000.05
            assert s.budget.native_max_claim('site')
            s.state.config['default_real_mode']='autonomous'
            s.state.config['site_info']['backup_reserve_percent']=100
            s.state.at+=2
            late.append('applied after HTTP timeout')
        asyncio.create_task(remotely_delayed())
        raise TimeoutError('HTTP completed before envelope acceptance expired')
    s.adapter.client._transport.write_config=timeout
    start=asyncio.create_task(s.session.start(120));await submitted.wait();await asyncio.sleep(0)
    assert s.session.pending and s.budget.native_max_claim('site')
    with pytest.raises(s.m.NativeMaxChargeError,match='provider timed out'):await start
    assert late and (s.state.config,s.state.tariff)==s.original
    assert s.store.data is None and not s.budget.native_max_claim('site')


@pytest.mark.asyncio
async def test_resume_timeout_retains_barrier_until_old_release_command_cannot_cancel_new_dispatch(setup):
    s=setup;submitted=asyncio.Event();late=[]
    async def delayed_resume(*,before_write,before_submit):
        await before_submit(s.state.at+.05);before_write();submitted.set()
        async def accepted_late():
            await asyncio.sleep(.02)
            assert s.budget.native_max_claim('site') and s.store.data
            late.append('old resume accepted before release to new control')
            s.state.at+=2
        asyncio.create_task(accepted_late())
        raise TimeoutError('resume HTTP timeout')
    s.adapter.client.resume_battery_operation=delayed_resume
    start=asyncio.create_task(s.session.start(120));await submitted.wait();await asyncio.sleep(0)
    assert s.session.pending and not any(k=='tariff' for k,_ in s.state.events)
    with pytest.raises(s.m.NativeMaxChargeError,match='provider timed out'):await start
    assert late and s.store.data is None and not s.budget.native_max_claim('site')
    assert (s.state.config,s.state.tariff)==s.original


@pytest.mark.asyncio
async def test_ambiguous_fleet_submission_is_quarantined_until_unique_late_tariff_arrives(setup):
    s=setup;original=s.adapter.upload
    async def timeout(tariff,before_write):
        if tariff.get('code')!='UNIQUE_FORCE':return await original(tariff,before_write)
        fence=await before_write();fence()
        raise TimeoutError('Fleet request may be queued remotely')
    s.adapter.upload=timeout
    with pytest.raises(s.m.NativeMaxChargeError,match='provider timed out'):await s.session.start(120)
    await s.session._cancel_watch()
    assert s.store.data['tariff_intent']=='forcing'
    assert s.state.tariff==s.original[1] and s.budget.native_max_claim('site')
    s.state.at+=600
    with pytest.raises(s.m.NativeMaxChargeError):await s.session.stop()
    await s.session._cancel_watch()
    # Neither elapsed time nor an unchanged baseline proves the queued write
    # cannot arrive. This exact unique tariff proves its one attempt arrived.
    s.state.tariff=deepcopy(s.adapter.forcing_tariff)
    assert await s.session.stop()
    assert s.state.tariff==s.original[1] and s.store.data is None


@pytest.mark.asyncio
async def test_settings_restored_with_high_battery_draw_keeps_ev_barrier_across_restart(setup):
    s=setup;await s.session.start(120)
    s.state.battery_w=-13800
    with pytest.raises(s.m.NativeMaxChargeError,match='physical settling'):
        await s.session.stop()
    assert (s.state.config,s.state.tariff)==s.original
    assert s.store.data['phase']=='settling' and s.budget.native_max_claim('site')
    await s.session._cancel_watch();events=deepcopy(s.state.events)
    recovered=type(s.session)(s.adapter,s.store,clock=lambda:s.state.at,monotonic=lambda:s.state.at)
    with pytest.raises(s.m.NativeMaxChargeError,match='physical settling'):
        await recovered.recover()
    await recovered._cancel_watch()
    assert s.budget.native_max_claim('site') and s.state.events==events
    s.state.battery_w=0
    assert await recovered.recover()
    assert not s.budget.native_max_claim('site') and s.state.events==events


@pytest.mark.asyncio
async def test_one_safe_sample_followed_by_renewed_draw_cannot_release_ev_capacity(setup):
    s=setup;await s.session.start(120)
    original=s.adapter.client.get_snapshot;reads=[]
    async def samples():
        s.state.battery_w=0 if not reads else -10000
        reads.append(s.state.battery_w)
        return await original()
    s.adapter.client.get_snapshot=samples
    with pytest.raises(s.m.NativeMaxChargeError,match='physical settling'):
        await s.session.stop()
    await s.session._cancel_watch()
    assert reads==[0,-10000] and s.store.data['phase']=='settling'
    assert s.budget.native_max_claim('site')
    s.adapter.client.get_snapshot=original;s.state.battery_w=0
    assert await s.session.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('direction,watts',[('charge',-10000),('discharge',10000)])
async def test_large_site_one_shot_kick_releases_before_native_tariff_and_restores(setup,direction,watts):
    s=setup
    s.adapter.direction=direction;s.adapter.startup_kick=True
    s.settings.update(optimization_max_discharge_w=20000,optimization_max_grid_export_w=15000,
                      optimization_backup_reserve=.2)
    assert (await s.session.start(120))['active']
    events=[e[0] for e in s.state.events]
    assert events[:3]==['kick','resume','tariff']
    assert [e[1] for e in s.state.events if e[0]=='kick']==[(watts,30)]
    assert s.store.data['expires_at']==1120
    assert not s.store.data.get('kick_pending')
    assert s.store.data['baseline']['direction']==direction
    if direction=='discharge':
        assert s.state.config['site_info']['backup_reserve_percent']==20
        assert s.state.config['site_info']['customer_preferred_export_rule']=='battery_ok'
    assert await s.session.stop()
    assert (s.state.config,s.state.tariff)==s.original
    assert not s.session.pending


@pytest.mark.asyncio
@pytest.mark.parametrize('busy', ['battery','solar','home'])
async def test_busy_large_inverter_skips_kick_and_uses_tariff(setup,busy):
    s=setup;s.adapter.startup_kick=True
    if busy=='battery':s.state.battery_w=12000
    if busy=='solar':s.state.solar_w=12000
    if busy=='home':s.state.load_w=12000
    await s.session.start(120)
    assert not any(e[0]=='kick' for e in s.state.events)
    assert any(e[0]=='tariff' for e in s.state.events)
    assert await s.session.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('response',['rejected','timeout'])
async def test_uncertain_kick_is_released_before_tariff_without_a_second_pulse(setup,response):
    s=setup;s.adapter.startup_kick=True
    if response=='rejected':s.state.kick_ok=False
    else:s.state.kick_error=TimeoutError('uncertain submission')
    await s.session.start(120)
    assert [e[0] for e in s.state.events][:3]==['kick','resume','tariff']
    assert sum(e[0]=='kick' for e in s.state.events)==1
    assert await s.session.stop()


@pytest.mark.asyncio
async def test_revocation_after_kick_only_resumes_and_never_uploads_forcing_tariff(setup):
    s=setup;s.adapter.startup_kick=True
    s.state.after_kick=lambda:setattr(s.state,'policy',False)
    with pytest.raises(s.m.NativeMaxChargeError):await s.session.start(120)
    assert [e[0] for e in s.state.events]==['kick','resume']
    assert (s.state.config,s.state.tariff)==s.original
    assert not s.session.pending


@pytest.mark.asyncio
async def test_failed_kick_release_keeps_owner_until_cleanup_succeeds(setup):
    s=setup;s.adapter.startup_kick=True;s.state.resume_ok=False
    with pytest.raises(s.m.NativeMaxChargeError):await s.session.start(120)
    assert s.store.data['kick_pending'] and s.session.pending
    assert not any(e[0]=='tariff' for e in s.state.events)
    s.state.resume_ok=True
    assert await s.session.stop()
    assert not s.session.pending


@pytest.mark.asyncio
async def test_discharge_recovery_retains_owner_until_export_settles(setup):
    s=setup;s.adapter.direction='discharge'
    s.settings.update(optimization_max_discharge_w=20000,optimization_max_grid_export_w=15000)
    await s.session.start(120)
    s.state.battery_w=5000
    with pytest.raises(s.m.NativeMaxChargeError,match='settling'):await s.session.stop()
    assert s.store.data['phase']=='settling'
    assert (s.state.config,s.state.tariff)==s.original
    count=len(s.state.events);s.state.battery_w=900
    assert await s.session.stop()
    assert len(s.state.events)==count


@pytest.mark.asyncio
async def test_restart_releases_uncertain_startup_kick_without_replaying_it(setup):
    s=setup;s.adapter.startup_kick=True;s.state.resume_ok=False
    with pytest.raises(s.m.NativeMaxChargeError):await s.session.start(120)
    await s.session._cancel_watch()
    assert s.store.data['kick_pending']
    s.store.data['local_acceptance_until']=s.state.at+1
    s.state.resume_ok=True
    recovered=type(s.session)(s.adapter,s.store,clock=lambda:s.state.at,monotonic=lambda:s.state.at)
    count=len(s.state.events)
    task=asyncio.create_task(recovered.recover());await asyncio.sleep(.02)
    assert not task.done() and len(s.state.events)==count
    s.state.at+=2
    assert await task
    assert sum(e[0]=='kick' for e in s.state.events)==1
    assert not any(e[0]=='tariff' for e in s.state.events)
    assert s.store.data is None and not s.budget.native_max_claim('site')


@pytest.mark.asyncio
@pytest.mark.parametrize('direction',['charge','discharge'])
async def test_optimizer_native_force_accepts_planning_rate_below_native_rating(setup,direction):
    s=setup;s.adapter.direction=direction;s.adapter.planning_request=True
    s.settings.update(powerwall_native_capacity_w=20000,optimization_max_discharge_w=15000)
    assert (await s.session.start(120))['active']
    assert any(event[0]=='tariff' for event in s.state.events)
    assert await s.session.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('optimizer', [False, True])
async def test_reported_large_site_tightens_export_before_tariff_and_restores(setup, optimizer):
    s=setup;s.adapter.direction='discharge';s.adapter.planning_request=optimizer
    s.settings.update(powerwall_native_capacity_w=21040, optimization_max_discharge_w=21000,
                      optimization_max_grid_export_w=10000, optimization_backup_reserve=.66)
    s.state.soc=72;s.state.config['site_info']['min_site_meter_power_ac']=-10.1
    original=deepcopy(s.state.config)
    upload=s.adapter.upload
    async def checked_upload(tariff,before_write):
        if tariff.get('code')=='UNIQUE_FORCE':
            assert s.state.config['site_info']['min_site_meter_power_ac']==-10
        return await upload(tariff,before_write)
    s.adapter.upload=checked_upload
    assert (await s.session.start(480))['active']
    assert s.store.data['baseline']['export_limit']==-10.1
    assert s.state.config['site_info']['backup_reserve_percent']==66
    assert s.settings['optimization_max_grid_export_w']==10000
    assert s.state.config['site_info']['min_site_meter_power_ac']==-10
    assert await s.session.stop()
    assert s.state.config==original


@pytest.mark.asyncio
@pytest.mark.parametrize('original_limit', [-10.1, -1e9])
async def test_export_bound_unconfirmed_never_uploads_forcing_tariff(setup, original_limit):
    s=setup;s.adapter.direction='discharge'
    s.settings.update(optimization_max_discharge_w=21000,optimization_max_grid_export_w=10000)
    s.state.config['site_info']['min_site_meter_power_ac']=original_limit
    write=s.adapter.client._transport.write_config
    async def ignored_limit(din,updates,**kwargs):
        result=await write(din,updates,**kwargs)
        if 'site_info.min_site_meter_power_ac' in updates:
            s.state.config['site_info']['min_site_meter_power_ac']=original_limit
        return result
    s.adapter.client._transport.write_config=ignored_limit
    with pytest.raises(s.m.NativeMaxChargeError):await s.session.start(120)
    assert not any(e[0]=='tariff' for e in s.state.events)
    assert not s.session.pending


@pytest.mark.asyncio
@pytest.mark.parametrize('original_limit', [-10.1, -1e9])
async def test_export_limit_retained_through_settling_and_restart(setup, original_limit):
    s=setup;s.adapter.direction='discharge'
    s.settings.update(optimization_max_discharge_w=21000,optimization_max_grid_export_w=10000)
    s.state.config['site_info']['min_site_meter_power_ac']=original_limit
    await s.session.start(120)
    s.state.battery_w=5000
    with pytest.raises(s.m.NativeMaxChargeError,match='settling'):await s.session.stop()
    await s.session._cancel_watch()
    assert s.store.data['phase']=='settling'
    assert s.state.config['site_info']['min_site_meter_power_ac']==-10
    s.state.battery_w=900
    recovered_adapter=type(s.adapter)(**{
        key:getattr(s.adapter,key) for key in (
            'client','identity','current_identity','policy','config','read_tariff','upload',
            'restorable','forcing_tariff','entry_id','site','monotonic','settle_interval','clock')},
        budget=type(s.budget)(s.budget.store,clock=lambda:s.state.at))
    recovered=type(s.session)(recovered_adapter,s.store,clock=lambda:s.state.at,monotonic=lambda:s.state.at)
    assert await recovered.recover()
    assert s.state.config['site_info']['min_site_meter_power_ac']==original_limit


@pytest.mark.asyncio
async def test_export_limit_changed_externally_is_preserved_on_restore(setup):
    s=setup;s.adapter.direction='discharge'
    s.settings.update(optimization_max_discharge_w=21000,optimization_max_grid_export_w=10000)
    s.state.config['site_info']['min_site_meter_power_ac']=-10.1
    await s.session.start(120)
    s.state.config['site_info']['min_site_meter_power_ac']=-8
    assert await s.session.stop()
    assert s.state.config['site_info']['min_site_meter_power_ac']==-8


@pytest.mark.asyncio
async def test_second_native_max_session_can_reacquire_after_export_limit_restore(setup):
    s=setup;s.adapter.direction='discharge'
    s.settings.update(optimization_max_discharge_w=21000,optimization_max_grid_export_w=10000)
    s.state.config['site_info']['min_site_meter_power_ac']=-10.1
    for _ in range(2):
        assert (await s.session.start(120))['active']
        assert s.state.config['site_info']['min_site_meter_power_ac']==-10
        assert await s.session.stop()
        assert s.state.config['site_info']['min_site_meter_power_ac']==-10.1


@pytest.mark.asyncio
@pytest.mark.parametrize('optimizer', [False, True])
async def test_unset_native_export_limit_is_tightened_before_large_site_tariff(setup, optimizer):
    s=setup;s.adapter.direction='discharge';s.adapter.planning_request=optimizer
    s.settings.update(powerwall_native_capacity_w=21040, optimization_max_discharge_w=21000,
                      optimization_max_grid_export_w=18800)
    s.state.config['site_info']['min_site_meter_power_ac']=-1e9
    original=deepcopy(s.state.config)
    upload=s.adapter.upload
    async def checked_upload(tariff,before_write):
        if tariff.get('code')=='UNIQUE_FORCE':
            assert s.state.config['site_info']['min_site_meter_power_ac']==-18.8
        return await upload(tariff,before_write)
    s.adapter.upload=checked_upload
    for _ in range(2):
        assert (await s.session.start(120))['active']
        assert s.store.data['baseline']['export_limit']==-1e9
        assert s.state.config['site_info']['min_site_meter_power_ac']==-18.8
        assert await s.session.stop()
        assert s.state.config==original
        assert not s.budget.native_max_claim('site')


@pytest.mark.asyncio
@pytest.mark.parametrize('native_limit', [None, 'missing', True, '-1000000000', float('nan'), float('inf'), 1, 0])
async def test_unknown_invalid_or_zero_native_export_limit_never_forces(setup, native_limit):
    s=setup;s.adapter.direction='discharge';s.adapter.planning_request=True
    s.settings.update(powerwall_native_capacity_w=21040, optimization_max_discharge_w=21000,
                      optimization_max_grid_export_w=18800)
    if native_limit=='missing':
        s.state.config['site_info'].pop('min_site_meter_power_ac')
    else:
        s.state.config['site_info']['min_site_meter_power_ac']=native_limit
    with pytest.raises(s.m.NativeMaxChargeError):await s.session.start(120)
    assert not s.state.events and not s.session.pending
    assert not s.budget.native_max_claim('site')


@pytest.mark.asyncio
async def test_native_export_cap_raised_during_session_revokes_forcing(setup):
    s=setup;s.adapter.direction='discharge'
    s.settings.update(optimization_max_discharge_w=21000,optimization_max_grid_export_w=10000)
    s.state.config['site_info']['min_site_meter_power_ac']=-10.1
    await s.session.start(120)
    s.state.config['site_info']['min_site_meter_power_ac']=-12
    assert not await s.adapter.guard()
    assert await s.session.stop()
    assert s.state.config['site_info']['min_site_meter_power_ac']==-12


@pytest.mark.asyncio
@pytest.mark.parametrize('original_limit', [-10.1, -1e9])
async def test_failed_native_limit_restore_retains_cleanup_for_retry(setup, original_limit):
    s=setup;s.adapter.direction='discharge'
    s.settings.update(optimization_max_discharge_w=21000,optimization_max_grid_export_w=10000)
    s.state.config['site_info']['min_site_meter_power_ac']=original_limit
    await s.session.start(120)
    write=s.adapter.client._transport.write_config
    async def fail_restore(din,updates,**kwargs):
        if updates.get('site_info.min_site_meter_power_ac')==original_limit:
            return False
        return await write(din,updates,**kwargs)
    s.adapter.client._transport.write_config=fail_restore
    with pytest.raises(s.m.NativeMaxChargeError,match='site limit restore'):
        await s.session.stop()
    await s.session._cancel_watch()
    assert s.store.data['phase']=='settling' and s.budget.native_max_claim('site')
    assert s.state.config['site_info']['min_site_meter_power_ac']==-10
    s.adapter.client._transport.write_config=write
    assert await s.session.stop()
    assert s.state.config['site_info']['min_site_meter_power_ac']==original_limit


@pytest.mark.asyncio
async def test_export_limit_edit_before_acquisition_write_blocks_forcing(setup):
    s=setup;s.adapter.direction='discharge'
    s.settings.update(optimization_max_discharge_w=21000,optimization_max_grid_export_w=10000)
    s.state.config['site_info']['min_site_meter_power_ac']=-10.1
    write=s.adapter.client._transport.write_config
    async def changed(din,updates,**kwargs):
        if 'site_info.min_site_meter_power_ac' in updates:
            s.state.config['site_info']['min_site_meter_power_ac']=-8
        return await write(din,updates,**kwargs)
    s.adapter.client._transport.write_config=changed
    with pytest.raises(s.m.NativeMaxChargeError):await s.session.start(120)
    assert not any(e[0]=='tariff' for e in s.state.events)
    assert s.state.config['site_info']['min_site_meter_power_ac']==-8
    assert not s.session.pending


@pytest.mark.asyncio
@pytest.mark.parametrize('optimizer', [False, True])
@pytest.mark.parametrize('original_limit', [-1e9, -2e9])
async def test_unset_export_limit_is_confirmed_before_native_force_and_restored_exactly(setup, optimizer, original_limit):
    s=setup;s.adapter.direction='discharge';s.adapter.planning_request=optimizer
    s.adapter.startup_kick=True
    s.settings.update(powerwall_native_capacity_w=20000, optimization_max_discharge_w=20000,
                      optimization_max_grid_export_w=18800)
    s.state.config['site_info']['min_site_meter_power_ac']=original_limit
    original=deepcopy(s.state.config)
    upload,read=s.adapter.upload,s.adapter.client._transport.read_config
    async def read_config(din):
        config=await read(din)
        s.state.events.append(('cap_readback',config['site_info']['min_site_meter_power_ac']))
        return config
    async def checked_upload(tariff,before_write):
        if tariff.get('code')=='UNIQUE_FORCE':
            assert s.store.data['baseline']['export_limit']==original_limit
            assert s.state.config['site_info']['min_site_meter_power_ac']==-18.8
            assert ('cap_readback',-18.8) in s.state.events
        return await upload(tariff,before_write)
    s.adapter.client._transport.read_config=read_config;s.adapter.upload=checked_upload
    assert await s.adapter.guard()
    assert not s.adapter._forcing_permitted(require_export_limit=True)
    for _ in range(2):
        assert (await s.session.start(120))['active']
        assert s.store.data['baseline']['export_limit']==original_limit
        assert s.state.config['site_info']['min_site_meter_power_ac']==-18.8
        assert await s.session.stop()
        assert s.state.config==original
        assert s.store.data is None and not s.budget.native_max_claim('site')


@pytest.mark.asyncio
@pytest.mark.parametrize('native_limit,software_limit,expected', [
    (-14.9,18800,-14.9), (-20,18800,-18.8), (-14.9,None,-14.9), (-1e9,None,-1e9)])
async def test_native_export_limit_is_never_widened_or_invented(setup,native_limit,software_limit,expected):
    s=setup;s.adapter.direction='discharge'
    s.settings.update(optimization_max_discharge_w=20000,optimization_max_grid_export_w=software_limit)
    s.state.config['site_info']['min_site_meter_power_ac']=native_limit
    assert (await s.session.start(120))['active']
    assert s.state.config['site_info']['min_site_meter_power_ac']==expected
    if native_limit==expected:
        assert not any(e[0]=='config' and 'site_info.min_site_meter_power_ac' in e[1] for e in s.state.events)
    assert await s.session.stop()
    assert s.state.config['site_info']['min_site_meter_power_ac']==native_limit


INVALID_EXPORT_LIMITS=['missing',None,False,'-1000000000',10,float('nan'),float('inf'),float('-inf')]


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid',INVALID_EXPORT_LIMITS)
async def test_invalid_native_export_evidence_denies_admission_before_commands(setup,invalid):
    s=setup;s.adapter.direction='discharge'
    s.settings.update(optimization_max_discharge_w=20000,optimization_max_grid_export_w=18800)
    info=s.state.config['site_info']
    if invalid=='missing':info.pop('min_site_meter_power_ac')
    else:info['min_site_meter_power_ac']=invalid
    assert not await s.adapter.guard()
    with pytest.raises(s.m.NativeMaxChargeError,match='policy denied'):
        await s.session.start(120)
    assert s.state.events==[] and s.store.data is None


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid',INVALID_EXPORT_LIMITS)
async def test_capture_revalidates_its_own_export_limit_before_journaling(setup,invalid):
    s=setup;s.adapter.direction='discharge'
    s.settings.update(optimization_max_discharge_w=20000,optimization_max_grid_export_w=18800)
    s.state.config['site_info']['min_site_meter_power_ac']=-18.8
    read=s.adapter.client.get_snapshot
    count=0
    async def capture_corruption():
        nonlocal count
        count+=1
        snapshot=await read()
        if count==2:  # start's admission succeeds; only capture returns corrupt evidence.
            info=snapshot.raw['config']['site_info']
            if invalid=='missing':info.pop('min_site_meter_power_ac')
            else:info['min_site_meter_power_ac']=invalid
        return snapshot
    s.adapter.client.get_snapshot=capture_corruption
    with pytest.raises(s.m.NativeMaxChargeError,match='export.*baseline'):
        await s.session.start(120)
    assert s.state.events==[] and s.store.data is None
    assert not s.budget.native_max_claim('site')


@pytest.mark.asyncio
@pytest.mark.parametrize('response',['rejected','timeout'])
async def test_unset_export_limit_failed_acquisition_never_submits_forcing(setup,response):
    s=setup;s.adapter.direction='discharge'
    s.settings.update(optimization_max_discharge_w=20000,optimization_max_grid_export_w=18800)
    s.state.config['site_info']['min_site_meter_power_ac']=-1e9
    write=s.adapter.client._transport.write_config
    async def fail_cap(din,updates,**kwargs):
        if updates.get('site_info.min_site_meter_power_ac')==-18.8:
            if response=='timeout':
                await write(din,updates,**kwargs)  # The timed-out write may have arrived.
                raise TimeoutError()
            return False
        return await write(din,updates,**kwargs)
    s.adapter.client._transport.write_config=fail_cap
    with pytest.raises((s.m.NativeMaxChargeError,TimeoutError)):
        await s.session.start(120)
    assert not any(e[0]=='tariff' and e[1].get('code')=='UNIQUE_FORCE' for e in s.state.events)
    assert s.state.config['site_info']['min_site_meter_power_ac']==-1e9
    assert not s.session.pending


@pytest.mark.asyncio
async def test_unset_export_limit_lost_before_tariff_submission_denies_forcing(setup):
    s=setup;s.adapter.direction='discharge'
    s.settings.update(optimization_max_discharge_w=20000,optimization_max_grid_export_w=18800)
    s.state.config['site_info']['min_site_meter_power_ac']=-1e9
    upload=s.adapter.upload
    async def remove_cap(tariff,before_write):
        if tariff.get('code')=='UNIQUE_FORCE':
            s.state.config['site_info']['min_site_meter_power_ac']=-1e9
        return await upload(tariff,before_write)
    s.adapter.upload=remove_cap
    with pytest.raises(s.m.NativeMaxChargeError):await s.session.start(120)
    assert not any(e[0]=='tariff' and e[1].get('code')=='UNIQUE_FORCE' for e in s.state.events)
    assert not s.session.pending


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid',INVALID_EXPORT_LIMITS)
async def test_invalid_export_restore_readback_retains_claim_until_retry(setup,invalid):
    s=setup;s.adapter.direction='discharge'
    s.settings.update(optimization_max_discharge_w=20000,optimization_max_grid_export_w=18800)
    s.state.config['site_info']['min_site_meter_power_ac']=-20
    await s.session.start(120)
    restore=s.adapter.restore_limits
    async def corrupted_readback(*args,**kwargs):
        read=s.adapter.client._transport.read_config
        async def read_config(din):
            config=await read(din)
            if invalid=='missing':config['site_info'].pop('min_site_meter_power_ac')
            else:config['site_info']['min_site_meter_power_ac']=invalid
            return config
        s.adapter.client._transport.read_config=read_config
        try:return await restore(*args,**kwargs)
        finally:s.adapter.client._transport.read_config=read
    s.adapter.restore_limits=corrupted_readback
    with pytest.raises(s.m.NativeMaxChargeError,match='site limit restore'):
        await s.session.stop()
    await s.session._cancel_watch()
    assert s.store.data['phase']=='settling' and s.budget.native_max_claim('site')
    assert s.state.config['site_info']['min_site_meter_power_ac']==-18.8
    s.adapter.restore_limits=restore
    assert await s.session.stop()
    assert s.state.config['site_info']['min_site_meter_power_ac']==-20
    assert s.store.data is None and not s.budget.native_max_claim('site')


@pytest.mark.asyncio
@pytest.mark.parametrize('field',['export_limit','force_export_limit'])
@pytest.mark.parametrize('invalid',[None,float('-inf'),'missing'])
async def test_invalid_saved_export_limit_never_becomes_a_restore_write(setup,field,invalid):
    s=setup;s.adapter.direction='discharge'
    s.settings.update(optimization_max_discharge_w=20000,optimization_max_grid_export_w=18800)
    s.state.config['site_info']['min_site_meter_power_ac']=-20
    await s.session.start(120)
    if invalid=='missing':s.session.record['baseline'].pop(field)
    else:s.session.record['baseline'][field]=invalid
    await s.session._save()
    with pytest.raises(s.m.NativeMaxChargeError,match='site limit restore'):
        await s.session.stop()
    await s.session._cancel_watch()
    assert s.store.data['phase']=='settling' and s.budget.native_max_claim('site')
    assert s.state.config['site_info']['min_site_meter_power_ac']==-18.8
    s.session.record['baseline'].update(export_limit=-20,force_export_limit=-18.8)
    assert await s.session.stop()
