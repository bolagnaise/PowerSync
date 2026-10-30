"""Explicit native Max never loses its baseline, deadline or cleanup owner."""
import asyncio
from copy import deepcopy
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

path=Path(__file__).parents[1]/'custom_components/power_sync/powerwall_local/native_max_charge.py'
spec=importlib.util.spec_from_file_location('native_max_lifetime_test',path)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


class Store:
    def __init__(self):self.data=None;self.fail_clear=False;self.fail_save=False;self.writes=[]
    async def async_load(self):return deepcopy(self.data)
    async def async_save(self,data):
        if self.fail_save or (self.fail_clear and data is None):raise OSError('store unavailable')
        self.data=deepcopy(data);self.writes.append(deepcopy(data))


class Adapter:
    identity='paired-site-and-key'
    def __init__(self,store):
        self.store=store;self.permitted=True;self.policy=True;self.restore_ok=True
        self.resume_ok=True;self.apply_ok=True;self.admission_ok=True;self.release_ok=True
        self.barrier=None;self.events=[];self.current={'mode':'self_consumption','reserve':5,'tariff':{'name':'real tariff'}}
        self.capture_hook=None;self.apply_hook=None
    def authority(self):return self.permitted
    async def guard(self):return self.policy
    async def capture(self):
        self.events.append('capture')
        if self.capture_hook:await self.capture_hook()
        return deepcopy(self.current)
    async def claim(self,token,deadline):
        assert self.store.data['phase']=='prepared'
        self.events.append('claim')
        if self.admission_ok:self.barrier=token
        return self.admission_ok
    async def resume(self,fence,*,write_intent):
        fence();assert self.store.data['phase']=='applying';self.events.append('resume')
        return self.resume_ok
    async def apply(self,baseline,before_write,*,write_intent):
        fence=await before_write();fence();self.events.append('apply_mode')
        assert self.barrier==self.store.data['id']
        self.current['mode']='autonomous';self.current['reserve']=100
        if self.apply_hook:await self.apply_hook()
        fence=await before_write();fence();self.events.append('apply_tariff')
        self.current['tariff']={'name':'force tariff'}
        return self.apply_ok
    async def restore(self,baseline,before_write,*,write_intent):
        fence=await before_write();fence();self.events.append('restore')
        assert self.barrier==self.store.data['id']
        if self.restore_ok:self.current={k:deepcopy(v) for k,v in baseline.items() if not k.startswith('_')}
        return self.restore_ok
    async def settled(self,baseline): return True
    async def release(self,token):
        assert self.store.data['phase']=='restored'
        self.events.append('release')
        if not self.release_ok:return False
        assert self.barrier in (None,token)
        self.barrier=None
        return True


def setup():
    store=Store();adapter=Adapter(store);clock=SimpleNamespace(now=1000.0)
    s=m.NativeMaxChargeSession(adapter,store,clock=lambda:clock.now,monotonic=lambda:clock.now,interval=.01)
    return s,adapter,store,clock


@pytest.mark.asyncio
async def test_persist_baseline_and_original_deadline_before_native_writes():
    s,a,store,clock=setup();original=deepcopy(a.current)
    result=await s.start(120)
    assert result['active'] and result['expires_at']==1120
    assert store.writes[0]['baseline']==original
    assert store.writes[0]['expires_at']==1120
    assert a.events.index('resume')<a.events.index('apply_mode')<a.events.index('apply_tariff')
    assert await s.stop()
    assert a.current==original and store.data is None and a.barrier is None


@pytest.mark.asyncio
async def test_settling_pending_is_distinct_and_keeps_journal_claim_until_retry():
    s, a, store, _ = setup()
    original = deepcopy(a.current)
    await s.start(120)
    async def still_settling(_baseline):
        return False
    a.settled = still_settling
    try:
        with pytest.raises(m.NativeMaxRestorePending):
            await s.stop()
        assert s.pending and store.data['phase'] == 'settling'
        assert a.barrier == store.data['id']
        assert a.current == original
        assert 'release' not in a.events
        async def physically_settled(_baseline):
            return True
        a.settled = physically_settled
        assert await s.stop()
        assert not s.pending and store.data is None and a.barrier is None
        assert a.events.count('restore') == 1
        assert a.events.count('apply_tariff') == 1
    finally:
        await s._cancel_watch()


@pytest.mark.asyncio
async def test_resume_ack_required_before_any_native_forcing_write():
    s,a,store,_=setup();a.resume_ok=False
    with pytest.raises(m.NativeMaxChargeError,match='resume'):
        await s.start(120)
    assert 'apply_mode' not in a.events and 'apply_tariff' not in a.events
    assert a.events.count('restore')==1
    assert not s.pending and store.data is None
    await s.stop()


@pytest.mark.asyncio
async def test_partial_native_apply_restores_exact_baseline():
    s,a,store,_=setup();original=deepcopy(a.current);a.apply_ok=False
    with pytest.raises(m.NativeMaxChargeError,match='readback'):
        await s.start(120)
    assert a.current==original and store.data is None and not s.pending
    await s.stop()


@pytest.mark.asyncio
async def test_failed_cleanup_retains_durable_owner_and_blocks_new_capture():
    s,a,store,_=setup();await s.start(120);a.restore_ok=False
    with pytest.raises(m.NativeMaxChargeError,match='restore'):
        await s.stop()
    assert s.pending and store.data['phase']=='restoring' and a.barrier
    captures=a.events.count('capture')
    with pytest.raises(m.NativeMaxChargeError,match='restore'):
        await s.start(120)
    assert a.events.count('capture')==captures
    a.restore_ok=True
    assert await s.stop()


@pytest.mark.asyncio
async def test_restart_only_restores_and_never_replays_charge():
    s,a,store,clock=setup();baseline=deepcopy(a.current);await s.start(120)
    await s._cancel_watch()  # the original process is gone
    a.events=[]
    recovered=m.NativeMaxChargeSession(a,store,clock=lambda:clock.now,monotonic=lambda:clock.now,interval=.01)
    assert await recovered.recover()
    assert a.current==baseline and a.events==['restore','release']
    assert store.data is None


@pytest.mark.asyncio
async def test_expired_queued_request_cannot_force_or_extend_deadline():
    s,a,store,clock=setup()
    async def slow_capture():clock.now+=20
    a.capture_hook=slow_capture
    with pytest.raises(m.NativeMaxChargeError):await s.start(10)
    assert 'resume' not in a.events and 'apply_mode' not in a.events
    assert store.writes[0]['expires_at']==1010
    assert not s.pending


@pytest.mark.asyncio
async def test_handoff_keeps_original_request_deadline():
    s,a,store,clock=setup()
    assert (await s.start(120,deadline=1030))['expires_at']==1030
    assert store.writes[0]['expires_at']==1030
    await s.stop()
    with pytest.raises(ValueError,match='extend'):
        await s.start(120,deadline=1121)


@pytest.mark.asyncio
async def test_cancelled_journal_commit_cannot_resurrect_intent_after_cleanup():
    s,a,store,_=setup();entered=asyncio.Event();finish=asyncio.Event()
    save=store.async_save
    async def delayed(data):
        if data and data['phase']=='prepared':entered.set();await finish.wait()
        await save(data)
    store.async_save=delayed
    start=asyncio.create_task(s.start(120));await entered.wait()
    start.cancel();await asyncio.sleep(0)
    assert not start.done() and not a.events.count('resume')
    finish.set()
    with pytest.raises(asyncio.CancelledError):await start
    assert store.data is None and not s.pending and a.barrier is None
    assert 'apply_mode' not in a.events
    await s.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('revocation', ['expiry','policy','stop'])
async def test_pending_provider_wait_is_interrupted_before_cleanup(revocation):
    s,a,store,clock=setup();entered=asyncio.Event();never=asyncio.Event()
    async def delayed():entered.set();await never.wait()
    a.apply_hook=delayed
    start=asyncio.create_task(s.start(120));await entered.wait()
    if revocation=='expiry':clock.now+=121
    elif revocation=='policy':a.policy=False
    else:await asyncio.wait_for(s.stop(),1)
    with pytest.raises(m.NativeMaxChargeError):await asyncio.wait_for(start,1)
    assert 'apply_tariff' not in a.events and a.events.count('restore')==1
    assert not s.pending and store.data is None and a.barrier is None
    await s.stop()


@pytest.mark.asyncio
async def test_final_ui_clear_remains_owned_until_new_acquisition_can_proceed():
    s,a,store,_=setup();await s.start(120)
    entered=asyncio.Event();finish=asyncio.Event();events=[]
    async def changed(status):
        if not status.get('phase'):
            entered.set();await finish.wait();events.append('ui_clear')
    s.changed=changed
    stop=asyncio.create_task(s.stop());await entered.wait()
    assert s.pending and s.record is None
    captures=a.events.count('capture')
    start=asyncio.create_task(s.start(120));await asyncio.sleep(0)
    assert a.events.count('capture')==captures
    finish.set();await stop;await start
    assert events==['ui_clear'] and s.pending and store.data['phase']=='active'
    s.changed=None;await s.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('progress',[None,{'mode_released':1},{'unknown_field':True}])
async def test_invalid_cleanup_journal_cannot_release_owned_native_settings(progress):
    s,a,store,_=setup();await s.start(120);await s._cancel_watch()
    store.data['cleanup']=progress
    recovered=type(s)(a,store)
    with pytest.raises(m.NativeMaxChargeError,match='cleanup journal'):
        await recovered.recover()
    assert 'restore' not in a.events and a.barrier
    await s.stop()


@pytest.mark.asyncio
async def test_stop_invalidates_inflight_write_then_finishes_cleanup_before_new_control():
    s,a,store,_=setup();entered=asyncio.Event();finish=asyncio.Event();original=deepcopy(a.current)
    async def wait():entered.set();await finish.wait()
    a.apply_hook=wait
    start=asyncio.create_task(s.start(120));await entered.wait()
    stop=asyncio.create_task(s.stop());await asyncio.sleep(0)
    finish.set()
    with pytest.raises(m.NativeMaxChargeError):await start
    assert await stop
    assert 'apply_tariff' not in a.events and a.current==original
    assert a.events.count('restore')==1 and store.data is None


@pytest.mark.asyncio
async def test_clear_failure_after_shared_release_cannot_repeat_old_restore_on_restart():
    s,a,store,clock=setup();await s.start(120);store.fail_clear=True
    with pytest.raises(OSError):await s.stop()
    assert store.data['phase']=='restored' and a.barrier is None
    restores=a.events.count('restore')
    await s._cancel_watch();store.fail_clear=False
    a.current={'mode':'new controller','reserve':30,'tariff':{'name':'new tariff'}}
    recovered=m.NativeMaxChargeSession(a,store,clock=lambda:clock.now,monotonic=lambda:clock.now,interval=.01)
    assert await recovered.recover()
    assert a.events.count('restore')==restores and a.current['mode']=='new controller'


@pytest.mark.asyncio
async def test_failed_shared_release_is_cleanup_only_and_preserves_barrier():
    s,a,store,_=setup();await s.start(120);a.release_ok=False
    with pytest.raises(m.NativeMaxChargeError,match='release'):
        await s.stop()
    assert store.data['phase']=='restored' and a.barrier
    restores=a.events.count('restore');a.release_ok=True
    assert await s.stop()
    assert a.events.count('restore')==restores


@pytest.mark.asyncio
async def test_changed_pairing_does_not_restore_using_a_different_identity():
    s,a,store,_=setup();await s.start(120);await s._cancel_watch();a.identity='different pair'
    with pytest.raises(m.NativeMaxChargeError,match='another paired'):
        await s.stop()
    assert a.events.count('restore')==0 and s.pending and a.barrier
    await s._cancel_watch();a.identity='paired-site-and-key';await s.stop()


@pytest.mark.asyncio
async def test_policy_loss_restores_without_reissuing_charge():
    s,a,store,_=setup();await s.start(120);a.policy=False
    for _ in range(50):
        if not s.pending:break
        await asyncio.sleep(.01)
    assert not s.pending and a.events.count('apply_tariff')==1
    assert a.events.count('restore')==1 and store.data is None
    await s.stop()


@pytest.mark.asyncio
async def test_initial_journal_failure_never_mutates_hardware():
    s,a,store,_=setup();store.fail_save=True
    with pytest.raises(OSError):await s.start(120)
    assert 'resume' not in a.events and 'apply_mode' not in a.events and 'restore' not in a.events
    await s._cancel_watch();store.fail_save=False;await s.stop()


@pytest.mark.asyncio
async def test_restart_closes_durable_acceptance_window_before_baseline_readback():
    s,a,store,clock=setup();await s.start(120);await s._cancel_watch()
    await s._write_intent('local',clock.now+.05)
    original=deepcopy(store.data['baseline']);a.current=deepcopy(original)
    restored=type(s)(a,store,clock=lambda:clock.now,monotonic=lambda:clock.now,interval=.01)
    task=asyncio.create_task(restored.recover());await asyncio.sleep(.02)
    assert not task.done() and a.barrier and 'restore' not in a.events
    # The abandoned process's signed envelope is accepted after the new
    # process already observed the original settings, but before expiry.
    a.current.update(mode='autonomous',reserve=100);clock.now+=2
    assert await task
    assert a.current==original and not a.barrier and store.data is None


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value',[('local_acceptance_until',True),('local_acceptance_until',float('inf')),
                                       ('tariff_intent','unknown')])
async def test_invalid_mutation_journal_retains_cleanup_barrier(field,value):
    s,a,store,_=setup();await s.start(120);await s._cancel_watch()
    store.data[field]=value
    recovered=type(s)(a,store)
    with pytest.raises(m.NativeMaxChargeError,match='mutation journal'):
        await recovered.recover()
    assert a.barrier
    await s.stop()


@pytest.mark.asyncio
async def test_shorter_optimizer_deadline_is_durable_without_restarting_force():
    s,a,store,clock=setup()
    await s.start(600)
    events=list(a.events)
    result=await s.shorten(1120)
    assert result['expires_at']==1120 and store.data['expires_at']==1120
    assert a.events==events
    assert (await s.shorten(1500))['expires_at']==1120
    await s._cancel_watch()
    recovered=m.NativeMaxChargeSession(a,store,clock=lambda:clock.now,monotonic=lambda:clock.now)
    assert await recovered.recover()
    assert a.events.count('apply_tariff')==1 and store.data is None


@pytest.mark.asyncio
async def test_shortened_deadline_expires_original_session():
    s,a,store,clock=setup();await s.start(600)
    await s.shorten(1001);clock.now=1002
    await asyncio.sleep(.04)
    assert not s.pending and store.data is None
    assert a.events.count('apply_tariff')==1


@pytest.mark.asyncio
async def test_shortened_deadline_save_failure_stops_forcing():
    s,a,store,clock=setup();await s.start(600)
    store.fail_save=True
    with pytest.raises(OSError):await s.shorten(1120)
    assert s._generation>1
    store.fail_save=False
    assert await s.stop()
    assert store.data is None


@pytest.mark.parametrize('restore_ok', [True, False])
@pytest.mark.parametrize('source,confirm,expected_pending', [
    ('user', False, False), ('manual', False, False),
    ('optimizer', False, True), ('user', True, True),
])
def test_restore_service_reports_manual_settling_without_releasing_owner(source, confirm, expected_pending, restore_ok):
    """The HA service accepts a manual restore in progress; internal proof stays strict."""
    import ast
    import logging
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    tree = ast.parse((Path(__file__).parents[1] / 'custom_components/power_sync/__init__.py').read_text())
    setup = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'async_setup_entry')
    handler = next(n for n in setup.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'handle_restore_normal')
    class StripImports(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            return ast.copy_location(ast.Pass(), node)
    handler = StripImports().visit(handler)
    handler.decorator_list = []
    async def run():
        store = Store()
        adapter = Adapter(store)
        session = m.NativeMaxChargeSession(adapter, store)
        await session.start(10)
        adapter.settled = AsyncMock(return_value=False)
        adapter.restore_ok = restore_ok
        stop = AsyncMock(side_effect=session.stop)
        ns = dict(ServiceCall=object, _control_call_source=lambda call: source,
                  _command_generation=[0], _stop_local_powerwall_dispatch=stop,
                  NativeMaxRestorePending=m.NativeMaxRestorePending,
                  _LOGGER=logging.getLogger(__name__))
        exec(compile(ast.fix_missing_locations(ast.Module(body=[handler], type_ignores=[])), '<restore_service>', 'exec'), ns)
        call = SimpleNamespace(context=SimpleNamespace(user_id=None, parent_id=None), data={'_confirm_restore': confirm})
        try:
            if not restore_ok:
                with pytest.raises(m.NativeMaxChargeError, match="restore was not confirmed"):
                    await ns['handle_restore_normal'](call)
            elif expected_pending:
                with pytest.raises(m.NativeMaxRestorePending):
                    await ns['handle_restore_normal'](call)
            else:
                await ns['handle_restore_normal'](call)
            assert session.pending
            assert session.record['phase'] == ('settling' if restore_ok else 'restoring')
            assert adapter.barrier == session.record['id']
            assert 'release' not in adapter.events
            adapter.settled.return_value = True
            adapter.restore_ok = True
            await session.stop()
            assert not session.pending
            assert adapter.barrier is None
        finally:
            await session._cancel_watch()
    asyncio.run(run())
