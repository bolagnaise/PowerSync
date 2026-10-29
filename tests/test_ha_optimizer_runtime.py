"""Persistent licensing, offline deadlines and async command fences without hardware."""
import asyncio
import base64
import copy
import importlib.util
import json
from pathlib import Path
import sys
import types

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "_ha_runtime_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "custom_components/power_sync/licensing")]
sys.modules[PACKAGE] = package

def load(name):
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{name}", Path(package.__path__[0]) / f"{name}.py")
    result = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = result
    spec.loader.exec_module(result)
    return result

runtime = load("runtime")
guard = load("guard")
policy = load("policy")
fixture = json.loads((ROOT / "tests/fixtures/ha_optimizer_permission.json").read_text())
NOW = fixture["now"]
KEYS = {fixture["key_id"]: base64.urlsafe_b64decode(fixture["public_key"] + "=")}
PRIVATE = Ed25519PrivateKey.from_private_bytes(bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"))

def sign(claims, typ="PSHA1"):
    enc = lambda obj: base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode().rstrip("=")
    message = f"{enc({'alg':'EdDSA','typ':typ,'kid':fixture['key_id']})}.{enc(claims)}"
    return message + "." + base64.urlsafe_b64encode(PRIVATE.sign(message.encode())).decode().rstrip("=")

def rollout(**changes):
    return {"schema":1,"issuer":"powersync-ha-licensing","audience":"power_sync:ha_policy","policy_epoch":1,
        "rollout_mode":"enforced","activation_at":NOW-1,"legacy_access_until":NOW,
        "issued_at":NOW,"expires_at":NOW+7*86400,**changes}

class Harness:
    def __init__(self):
        self.wall = NOW
        self.mono = 0
        self.stored = {}
        self.offline = False
        self.denied = False
        self.revision = 1
        self.calls = []
        self.policy = rollout()
        self.client = self.new_client()

    def new_client(self):
        return runtime.LicenseRuntime(self.load, self.save, self.request, KEYS,
            wall_clock=lambda:self.wall, monotonic=lambda:self.mono)

    async def load(self): return copy.deepcopy(self.stored)
    async def save(self, value): self.stored = copy.deepcopy(value)
    async def request(self, path, body):
        self.calls.append((path, copy.deepcopy(body)))
        if self.offline: raise TimeoutError()
        if path == "/policy": return {"policy":sign(self.policy,"PSHP1")}
        if path == "/refresh":
            claims = {**fixture["claims"],"installation_id":body["installation_id"],"entitlement_revision":self.revision}
            if self.denied: claims.update(features=[],source_state="subscription_required",expires_at=NOW+300,refresh_after=NOW+60)
            return {"permission":sign(claims)}
        if path == "/pairing/start":return {"pairing_code":"A"*43}
        if path == "/pairing/exchange":return {"paired":True,"installation_id":self.client.installation_id}
        raise AssertionError(path)

    async def activate(self):
        await self.client.initialize()
        await self.client.finish_pairing("A"*43)


def test_manual_scope_nests_for_its_task_but_child_tasks_wait():
    async def run():
        h = Harness(); await h.activate()
        manager = h.client
        entered = asyncio.Event()
        async def child():
            async with manager.manual_command_scope('one'):
                entered.set()
                await manager.relinquish('one')
        async with manager.manual_command_scope('one'):
            async with manager.manual_command_scope('one'):
                assert manager._manual_command_depth['one'] == 2
                await manager.relinquish('one')
            assert manager._manual_command_depth['one'] == 1
            queued = asyncio.create_task(child())
            await asyncio.sleep(0)
            assert not entered.is_set()
            # Another entry has independent ownership.
            async with manager.manual_command_scope('two'):
                await manager.relinquish('two')
        await asyncio.wait_for(queued, 1)
        assert entered.is_set()
        assert manager.entries['one']['generation'] == 2
        assert not manager._manual_command_owners
        assert not manager._manual_command_depth
        assert not manager.control_lock('one').locked()
    asyncio.run(run())


@pytest.mark.parametrize('cancel_inside', [False, True])
def test_cancelled_manual_scope_releases_ownership(cancel_inside):
    async def run():
        h = Harness(); await h.activate()
        manager = h.client
        entered = asyncio.Event()
        async def command():
            async with manager.manual_command_scope('one'):
                entered.set()
                await asyncio.Event().wait()
        if cancel_inside:
            queued = asyncio.create_task(command())
            await entered.wait()
        else:
            await manager.control_lock('one').acquire()
            queued = asyncio.create_task(command())
            await asyncio.sleep(0)
            assert not entered.is_set()
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        if not cancel_inside:
            manager.control_lock('one').release()
        assert not manager._manual_command_owners
        assert not manager._manual_command_depth
        async with manager.manual_command_scope('one'):
            await manager.relinquish('one')
        assert not manager.control_lock('one').locked()
    asyncio.run(run())

def test_offline_restart_and_clock_rollback_do_not_extend_permission():
    async def run():
        h=Harness();await h.activate();identity=h.client.installation_id;secret=h.client.credential
        assert h.client.permits("one")
        h.mono=6*86400;h.wall=NOW+6*86400;h.offline=True
        await h.client.maintain(force=True)
        assert h.client.permits("one")
        assert h.stored["observed_at"]==NOW+6*86400
        h.wall=NOW-86400;h.mono=0
        client=h.new_client();await client.initialize()
        assert client.installation_id==identity and client.credential==secret
        assert client.permits("one")
        h.mono=86400
        assert not client.permits("one"),"wall-clock rollback cannot replenish the seven-day lease"
        await client.maintain(force=True)
        assert not client.permits("one")
    asyncio.run(run())

def test_denial_revision_survives_restart_and_paid_return_needs_resume():
    async def run():
        h=Harness();await h.activate();h.denied=True;h.revision=2
        await h.client.maintain(force=True)
        assert not h.client.permits("one") and h.client.admission.suspended
        h.client=h.new_client();await h.client.initialize()
        h.denied=False;h.revision=1
        await h.client.maintain(force=True)
        assert not h.client.permits("one"),"a stale allow cannot replace a signed denial"
        h.revision=3;await h.client.maintain(force=True)
        assert h.client.status("one")["requires_resume"]
        await h.client.resume("one")
        assert h.client.permits("one")
    asyncio.run(run())

def test_command_journal_precedes_write_and_old_queued_generation_cannot_resume():
    async def run():
        h=Harness();await h.activate();events=[];waiting=asyncio.Event();continue_work=asyncio.Event()
        owner=types.SimpleNamespace(hass=types.SimpleNamespace(data={runtime.DATA_KEY:h.client}),entry_id="one")
        @guard.optimizer_work
        async def solve(self):
            waiting.set();await continue_work.wait()
            await guard.fenced_call(self,lambda:write(),"set_backup_reserve")
        async def write():
            assert h.stored["entries"]["one"]["recovery_pending"]
            events.append("write")
        task=asyncio.create_task(solve(owner));await waiting.wait()
        h.denied=True;h.revision=2;await h.client.maintain(force=True)
        h.denied=False;h.revision=3;await h.client.maintain(force=True);await h.client.resume("one")
        continue_work.set()
        with pytest.raises(guard.OptimizerAccessLost):await task
        assert events==[]
        await solve(owner)
        assert events==["write"]
    asyncio.run(run())

def test_entries_resume_independently_and_recovery_cannot_force_charge():
    async def run():
        h=Harness();await h.activate()
        h.client.entries={"one":{"resume_required":True},"two":{"resume_required":True,"recovery_pending":True}}
        await h.client.resume("one")
        assert h.client.permits("one") and not h.client.permits("two")
        with pytest.raises(ValueError,match="recovery_pending"):await h.client.resume("two")
        owner=types.SimpleNamespace(hass=types.SimpleNamespace(data={runtime.DATA_KEY:h.client}),entry_id="one")
        with guard.recovery_scope():
            with pytest.raises(guard.OptimizerAccessLost):await guard.fenced_call(owner,lambda:asyncio.sleep(0),"force_charge")
            await guard.fenced_call(owner,lambda:asyncio.sleep(0),"set_backup_reserve")
        await h.client.relinquish("two")
        assert not h.client.entries["two"]["recovery_pending"]
    asyncio.run(run())

def test_fixed_transition_and_policy_rollback():
    async def run():
        h=Harness();h.policy=rollout(legacy_access_until=NOW+86400)
        await h.client.initialize();await h.client.maintain(force=True)
        assert h.client.permits("one") and h.client.status("one")["access_state"]=="transition"
        h.mono=86400;h.offline=True;await h.client.maintain(force=True)
        assert not h.client.permits("one"),"late pairing/reinstallation cannot reset the fixed notice deadline"
        h.offline=False;h.policy=rollout(policy_epoch=2,rollout_mode="shadow",activation_at=None,legacy_access_until=None,issued_at=NOW+86400)
        await h.client.maintain(force=True);assert h.client.permits("one")
        h.policy=rollout();await h.client.maintain(force=True)
        assert h.client.policy.epoch==2
    asyncio.run(run())

def test_storage_failure_closes_admission_before_control():
    async def run():
        h=Harness();await h.activate()
        async def broken(_value):raise OSError()
        h.client._save=broken
        with pytest.raises(OSError):await h.client.journal("one")
        assert not h.client.permits("one")
    asyncio.run(run())

def test_repaired_fast_clock_requires_a_new_signed_revision():
    async def run():
        h=Harness();await h.activate()
        h.wall=NOW+10*365*86400;h.offline=True;await h.client.maintain(force=True)
        h.client=h.new_client();await h.client.initialize();h.wall=NOW;h.offline=False
        h.revision=1;await h.client.maintain(force=True)
        assert not h.client.permits("one"),"an old signed response cannot reset the time watermark"
        h.revision=2;await h.client.maintain(force=True)
        assert h.client.now()==NOW
        await h.client.resume("one");assert h.client.permits("one")
    asyncio.run(run())

def test_reload_generation_fences_only_the_unloaded_entry():
    async def run():
        h=Harness();await h.activate();writes=[];waiting=asyncio.Event();release=asyncio.Event()
        h.client.entries={"one":{"generation":1},"two":{"generation":1}}
        @guard.optimizer_work
        async def delayed(owner):
            waiting.set();await release.wait()
            await guard.fenced_call(owner,lambda:write(owner.entry_id),"set_backup_reserve")
        async def write(entry):writes.append(entry)
        one=types.SimpleNamespace(hass=types.SimpleNamespace(data={runtime.DATA_KEY:h.client}),entry_id="one")
        two=types.SimpleNamespace(hass=one.hass,entry_id="two")
        task=asyncio.create_task(delayed(one));await waiting.wait()
        h.client.entries["one"]["generation"]+=1
        release.set()
        with pytest.raises(guard.OptimizerAccessLost):await task
        await delayed(two)
        assert writes==["two"]
        routing=load("entry_routing")
        async def handle_one(call):return "one"
        async def handle_two(call):return "two"
        routing.register_handlers(one.hass,"one",{"restore_normal":handle_one})
        routing.register_handlers(one.hass,"two",{"restore_normal":handle_two})
        router=routing.routed_handler(one.hass,"two","restore_normal",handle_two)
        assert await router(types.SimpleNamespace(data={"entry_id":"one"}))=="one"
        assert await router(types.SimpleNamespace(data={}))=="two"
        with pytest.raises(ValueError):await router(types.SimpleNamespace(data={"entry_id":"missing"}))
    asyncio.run(run())

@pytest.mark.parametrize("changes",[{"policy_epoch":0},{"activation_at":None},{"legacy_access_until":True},{"audience":"cloud"},{"expires_at":NOW+8*86400}])
def test_invalid_signed_rollout_policy(changes):
    with pytest.raises(ValueError):policy.verify_policy(sign(rollout(**changes),"PSHP1"),KEYS,NOW)

def test_worker_signed_rollout_fixture():
    verified = policy.verify_policy(fixture['policy'], KEYS, NOW)
    assert verified.enforces(NOW) and verified.epoch == fixture['policy_claims']['policy_epoch']


def test_parallel_first_writes_wait_for_durable_journal():
    async def run():
        h=Harness();await h.activate();saving=asyncio.Event();release=asyncio.Event();writes=[]
        old_save=h.save
        async def delayed(value):
            if value['entries'].get('one',{}).get('recovery_pending'):
                saving.set();await release.wait()
            await old_save(value)
        h.client._save=delayed
        owner=types.SimpleNamespace(hass=types.SimpleNamespace(data={runtime.DATA_KEY:h.client}),entry_id='one')
        @guard.optimizer_command
        async def set_backup_reserve(owner):
            assert h.stored['entries']['one']['recovery_pending']
            writes.append('write')
        first=asyncio.create_task(set_backup_reserve(owner));await saving.wait()
        second=asyncio.create_task(set_backup_reserve(owner));await asyncio.sleep(0)
        assert not writes
        release.set();await asyncio.gather(first,second);assert writes==['write','write']
    asyncio.run(run())


def test_manual_takeover_waits_for_inflight_mutation_and_fences_old_solve():
    async def run():
        h=Harness();await h.activate();started=asyncio.Event();release=asyncio.Event();events=[]
        owner=types.SimpleNamespace(hass=types.SimpleNamespace(data={runtime.DATA_KEY:h.client}),entry_id='one')
        @guard.optimizer_command
        async def force_charge(owner):
            started.set();await release.wait();events.append('old mutation completed')
        task=asyncio.create_task(force_charge(owner));await started.wait()
        manual=asyncio.create_task(h.client.relinquish('one'));await asyncio.sleep(0)
        assert not manual.done()
        release.set();await task;await manual;events.append('manual takeover')
        assert events==['old mutation completed','manual takeover']
        with guard.recovery_scope():
            @guard.optimizer_command
            async def _start_charging(owner,config):raise AssertionError('new control during recovery')
            with pytest.raises(guard.OptimizerAccessLost):await _start_charging(owner,None)
    asyncio.run(run())


def test_new_policy_epoch_invalidates_previous_permission_offline():
    async def run():
        h=Harness();await h.activate();h.policy=rollout(policy_epoch=2)
        old_request=h.request
        async def request(path,body):
            if path=='/refresh':raise TimeoutError()
            return await old_request(path,body)
        h.client._request=request;await h.client.maintain(force=True)
        assert not h.client.permits('one'),'cached older policy epoch cannot grant after key/environment policy cutover'
    asyncio.run(run())

def manager_method(name):
    import ast, logging
    source=ROOT/'custom_components/power_sync/licensing/manager.py'
    tree=ast.parse(source.read_text())
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='HALicenseManager')
    function=next(n for n in cls.body if isinstance(n,(ast.AsyncFunctionDef,ast.FunctionDef)) and n.name==name)
    namespace={'asyncio':asyncio,'json':json,'Path':Path,'LicenseRuntime':runtime.LicenseRuntime,'recovery_scope':guard.recovery_scope,'_LOGGER':logging.getLogger(__name__)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function],type_ignores=[])),str(source),'exec'),namespace)
    return namespace[name]


def test_recovery_journal_survives_restart_and_failed_cleanup():
    async def run():
        h=Harness();await h.activate();await h.client.journal('one')
        h.denied=True;h.revision=2;await h.client.maintain(force=True)
        calls=[]
        async def fail(**kwargs):
            calls.append(kwargs);assert h.stored['entries']['one']['recovery_pending'];raise OSError('offline inverter')
        coordinator=types.SimpleNamespace(_enabled=True,_pre_idle_backup_reserve=None,_solar_export_hold=None,
            _get_active_force_state=lambda:None,_monitoring_mode_active=lambda:False,disable=fail)
        h.client.coordinators={'one':coordinator}
        await manager_method('reconcile_entries')(h.client)
        assert h.stored['entries']['one']['recovery_pending'] and h.stored['entries']['one']['resume_required']
        h.client=h.new_client();await h.client.initialize()
        async def restored(**kwargs):coordinator._enabled=False;calls.append(kwargs)
        coordinator.disable=restored;h.client.coordinators={'one':coordinator}
        await manager_method('reconcile_entries')(h.client)
        assert not h.stored['entries']['one']['recovery_pending']
        assert h.stored['entries']['one']['resume_required'] and not h.client.permits('one')
        assert len(calls)==2
    asyncio.run(run())


def test_supported_ha_silent_storage_failure_cannot_admit_control(tmp_path):
    async def run():
        data={'permission':'signed'}
        path=tmp_path/'storage.json'
        class Store:
            async def async_save(self,data):pass  # HA logs a WriteError and returns normally.
        store=Store();store.path=str(path)
        async def executor(fn):return fn()
        manager=types.SimpleNamespace(_save_lock=asyncio.Lock(),_store=store,hass=types.SimpleNamespace(async_add_executor_job=executor))
        with pytest.raises(FileNotFoundError):await manager_method('_verified_save')(manager,data)
        path.write_text(json.dumps({'data':{'permission':'old'}}))
        with pytest.raises(OSError):await manager_method('_verified_save')(manager,data)
        path.write_text(json.dumps({'data':data}))
        await manager_method('_verified_save')(manager,data)
    asyncio.run(run())

def test_expired_shadow_policy_cannot_bypass_the_rollout_forever():
    async def run():
        h=Harness();h.policy=rollout(rollout_mode='shadow',activation_at=None,legacy_access_until=None)
        await h.client.initialize();await h.client.maintain(force=True)
        assert h.client.permits('one')
        h.offline=True;h.wall=NOW+7*86400;h.mono=7*86400
        await h.client.maintain(force=True)
        assert not h.client.permits('one')
        h.client=h.new_client();await h.client.initialize()
        assert not h.client.permits('one')
    asyncio.run(run())

def test_expiry_during_admitted_command_keeps_recovery_and_does_not_publish_success():
    async def run():
        h=Harness();await h.activate();events=[]
        owner=types.SimpleNamespace(hass=types.SimpleNamespace(data={runtime.DATA_KEY:h.client}),entry_id='one')
        async def native():
            events.append('accepted write')
            h.wall=NOW+7*86400;h.mono=7*86400
            return True
        @guard.optimizer_work
        async def execute(owner):
            await guard.fenced_call(owner,native,'set_backup_reserve')
            events.append('published active')
        with pytest.raises(guard.OptimizerAccessLost):await execute(owner)
        assert events==['accepted write'] and h.stored['entries']['one']['recovery_pending']
    asyncio.run(run())


def test_paid_verification_does_not_reenable_explicitly_disabled_entry():
    async def run():
        h=Harness();await h.activate()
        async def enable():raise AssertionError('user disabled this entry')
        h.client.coordinators={'one':types.SimpleNamespace(_enabled=False,_entry=types.SimpleNamespace(options={'optimization_enabled':False},data={}),enable=enable)}
        await manager_method('reconcile_entries')(h.client)
    asyncio.run(run())

def test_corrupt_entry_storage_closes_paid_access_without_breaking_status():
    async def run():
        h=Harness();await h.activate();h.stored['entries']={'one':17}
        h.client=h.new_client();await h.client.initialize()
        assert not h.client.permits('one')
        assert h.client.status('one')['verification_error']=='licensing_storage_unavailable'
    asyncio.run(run())

def test_manual_takeover_fences_read_only_solve_before_first_control():
    async def run():
        h=Harness();await h.activate();waiting=asyncio.Event();release=asyncio.Event()
        owner=types.SimpleNamespace(hass=types.SimpleNamespace(data={runtime.DATA_KEY:h.client}),entry_id='one')
        @guard.optimizer_work
        async def solve(owner):
            waiting.set();await release.wait()
            await guard.fenced_call(owner,lambda:asyncio.sleep(0),'set_backup_reserve')
        task=asyncio.create_task(solve(owner));await waiting.wait()
        await h.client.relinquish('one');release.set()
        with pytest.raises(guard.OptimizerAccessLost):await task
    asyncio.run(run())

def test_storage_outage_does_not_block_free_manual_takeover():
    async def run():
        h=Harness();await h.activate();await h.client.journal('one')
        h.client.entries['one']['pre_idle_reserve']=20
        async def failed(_data):raise OSError('disk unavailable')
        h.client._save=failed
        await h.client.relinquish('one')
        assert not h.client.permits('one')
        assert 'pre_idle_reserve' not in h.client.entries['one']
    asyncio.run(run())

def test_manager_manual_takeover_clears_old_reserve_and_keeps_manual_available():
    async def run():
        h=Harness();await h.activate();await h.client.journal('one')
        h.client.entries['one'].update(pre_idle_reserve=20,reserve_config_at_journal=20)
        c=types.SimpleNamespace(_pre_idle_backup_reserve=20,_idle_hold_reserve=70,_last_optimizer_self_consumption_reserve_target=70)
        h.client.coordinators={'one':c}
        async def failed(_data):raise OSError('disk unavailable')
        h.client._save=failed
        await manager_method('relinquish')(h.client,'one')
        assert c._pre_idle_backup_reserve is None and c._idle_hold_reserve is None
        assert guard.service_allowed(types.SimpleNamespace(data={runtime.DATA_KEY:h.client}),{'source':'user'},'handle_force_charge')
        assert not h.client.permits('one')
    asyncio.run(run())


def test_restart_does_not_restore_an_old_reserve_over_newer_manual_preferences():
    import ast
    source=ROOT/'custom_components/power_sync/licensing/manager.py'
    cls=next(n for n in ast.parse(source.read_text()).body if isinstance(n,ast.ClassDef) and n.name=='HALicenseManager')
    function=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='attach')
    namespace={}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function],type_ignores=[])),str(source),'exec'),namespace)
    manager=types.SimpleNamespace(entries={'one':{'pre_idle_reserve':20,'reserve_config_at_journal':20,'recovery_pending':True}},coordinators={},_task=True)
    c=types.SimpleNamespace(entry_id='one',_pre_idle_backup_reserve=None,_configured_startup_backup_reserve=lambda:(65,'manual'),_executor=None,_ev_coordinator=None)
    namespace['attach'](manager,c)
    assert c._pre_idle_backup_reserve is None
    assert 'pre_idle_reserve' not in manager.entries['one']
    assert manager.entries['one']['recovery_pending']

def test_active_owned_control_journal_does_not_report_a_paused_optimiser():
    async def run():
        h=Harness();await h.activate();await h.client.journal('one')
        assert h.stored['entries']['one']['recovery_pending']
        assert h.client.status('one')['can_optimize']
        assert not h.client.status('one')['recovery_pending']
        h.denied=True;h.revision=2;await h.client.maintain(force=True)
        assert h.client.status('one')['recovery_pending']
    asyncio.run(run())

def test_long_lived_loop_starts_fresh_work_after_manual_generation_changes():
    async def run():
        h=Harness();await h.activate();first=asyncio.Event();next_tick=asyncio.Event();events=[]
        owner=types.SimpleNamespace(hass=types.SimpleNamespace(data={runtime.DATA_KEY:h.client}),entry_id='one',_licensing_entry_generation=0,_executor=None,_ev_coordinator=None)
        h.client.coordinators={'one':owner}
        @guard.optimizer_work
        async def decide(owner):events.append(guard.control_entry_generation())
        @guard.background_work
        async def loop(owner):
            await decide(owner);first.set();await next_tick.wait();await decide(owner)
        @guard.optimizer_work
        async def enable(owner):return asyncio.create_task(loop(owner))
        task=await enable(owner);await first.wait()
        await h.client.relinquish('one')
        manager_method('finish_manual_command')(h.client,'one')
        next_tick.set();await task
        assert events==[0,1],'the producer keeps running, while each decision captures its own current generation'
    asyncio.run(run())


def test_unloaded_object_cannot_start_new_work_in_a_reloaded_entry():
    async def run():
        h=Harness();await h.activate();h.client.entries={'one':{'generation':2}}
        old=types.SimpleNamespace(hass=types.SimpleNamespace(data={runtime.DATA_KEY:h.client}),entry_id='one',_licensing_entry_generation=1)
        new=types.SimpleNamespace(hass=old.hass,entry_id='one',_licensing_entry_generation=2)
        @guard.optimizer_work
        async def decision(owner):return True
        assert await decision(old) is False
        assert await decision(new) is True
    asyncio.run(run())


def test_superseded_solve_cannot_leave_a_cached_schedule_for_later_dispatch():
    async def run():
        h=Harness();await h.activate();ready=asyncio.Event();release=asyncio.Event()
        owner=types.SimpleNamespace(hass=types.SimpleNamespace(data={runtime.DATA_KEY:h.client}),entry_id='one',_last_optimizer_result=None,_current_schedule=None)
        @guard.optimizer_work
        async def _run_optimization(owner):
            ready.set();await release.wait();owner._last_optimizer_result=owner._current_schedule='old plan'
        task=asyncio.create_task(_run_optimization(owner));await ready.wait()
        await h.client.relinquish('one');release.set()
        with pytest.raises(guard.OptimizerAccessLost):await task
        assert owner._last_optimizer_result is None and owner._current_schedule is None
    asyncio.run(run())


def test_shadow_subscription_denial_does_not_invent_a_future_resume_requirement():
    async def run():
        h=Harness();h.policy=rollout(rollout_mode='shadow',activation_at=None,legacy_access_until=None);h.denied=True
        await h.activate();assert not h.client.admission.suspended
        h.denied=False;h.revision=2;await h.client.maintain(force=True)
        h.policy=rollout();await h.client.maintain(force=True)
        assert h.client.permits('one') and not h.client.status('one')['requires_resume']
    asyncio.run(run())


@pytest.mark.parametrize('blocked_by', ['resume', 'recovery', 'denial', 'storage', 'permission'])
def test_loaded_entry_keeps_remaining_access_and_recovery_gates(blocked_by):
    async def run():
        h=Harness();await h.activate()
        h.client.entries={'one':{'generation':4,'unloaded':True},
                          'two':{'generation':7,'unloaded':True}}
        if blocked_by in ('resume','recovery'):
            h.client.entries['one']['resume_required']=True
            if blocked_by=='recovery':h.client.entries['one']['recovery_pending']=True
        elif blocked_by=='denial':
            h.denied=True;h.revision=2;await h.client.maintain(force=True)
        elif blocked_by=='storage':
            h.client.storage_ok=False
        else:
            h.client.admission.permission=None
        before=copy.deepcopy(h.client.entries['one'])
        manager_method('entry_loaded')(h.client,'one')
        assert h.client.entries['one']=={**before,'unloaded':False}
        assert h.client.entries['two']=={'generation':7,'unloaded':True}
        assert not h.client.permits('one') and not h.client.permits('two')
    asyncio.run(run())
