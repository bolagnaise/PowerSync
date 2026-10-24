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
        'customer_preferred_export_rule':'pv_only'}}, tariff={'name':'real tariff','code':'REAL'},
        identity='paired', policy=True, grid='SystemGridConnected', soc=50,
        events=[], at=1000., upload_ok=True, local_ok=True,
        before_validate=None,before_send=None)
    original = deepcopy(state.config), deepcopy(state.tariff)
    settings = {'optimization_max_charge_w':14700, 'optimization_max_grid_import_w':15000,
                'optimization_grid_charge_soc_cap':1, 'optimization_allow_grid_charge':True}
    class Store:
        data=None
        async def async_load(self): return deepcopy(self.data)
        async def async_save(self,data): self.data=deepcopy(data)
    class Transport:
        async def read_config(self,din): return deepcopy(state.config)
        async def write_config(self,din,updates,*,before_write,validate_config):
            await asyncio.sleep(0)
            forcing=updates.get('default_real_mode')=='autonomous' and updates.get('site_info.backup_reserve_percent')==100
            if forcing and state.before_validate: state.before_validate()
            if not validate_config(deepcopy(state.config)): return False
            if forcing and state.before_send: state.before_send()
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
            return SimpleNamespace(raw={'config':deepcopy(state.config)},soc=state.soc,grid_status=state.grid)
        async def resume_battery_operation(self,*,before_write):
            before_write(); state.events.append(('resume',None)); return True
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
        entry_id='battery',site='site',monotonic=lambda:state.at)
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
