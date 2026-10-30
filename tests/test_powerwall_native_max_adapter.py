"""Native Max preserves permissions and restores only its own exact settings."""
import asyncio
import ast
from copy import deepcopy
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest
from test_tesla_tariff_rate_limit import upload_harness

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
        'customer_preferred_export_rule':'pv_only',
        'disallow_charge_from_grid_with_solar_installed':False}}, tariff={'name':'real tariff','code':'REAL'},
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
            working = deepcopy(state.config)
            if not validate_config(working): return False
            for key, value in updates.items():
                if key.startswith('site_info.'): working['site_info'][key[10:]] = value
                else: working[key] = value
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
async def test_native_export_uses_cloud_when_v1r_omits_original_permission(setup):
    s = setup
    s.adapter.direction = 'discharge'
    s.settings.update(optimization_max_discharge_w=14700)
    s.state.config['site_info'].pop('customer_preferred_export_rule')
    reads = []
    async def cloud_read():
        reads.append(True)
        return 'pv_only'
    s.adapter.read_export_rule = cloud_read
    assert await s.adapter.guard()
    baseline = await s.adapter.capture()
    assert baseline['export_rule'] == 'pv_only'
    assert baseline['export_rule_source'] == 'cloud'
    assert reads
    assert 'customer_preferred_export_rule' not in s.state.config['site_info']
    assert not s.state.events


def cloud_export_fallback(s, rule):
    """Independent cloud permission; it never changes the local config fake."""
    s.adapter.direction = 'discharge'
    s.settings.update(optimization_max_discharge_w=14700)
    s.state.config['site_info'].pop('customer_preferred_export_rule', None)
    s.state.cloud_export_rule = rule
    s.state.cloud_write_ok = True
    s.state.cloud_reads = 0
    async def read():
        s.state.cloud_reads += 1
        return s.state.cloud_export_rule
    async def write(target, before_write, *, restoring=False):
        fence = await before_write()
        fence()
        assert s.store.data['baseline']['export_rule'] == rule
        assert s.store.data['export_intent'] == target
        s.state.events.append(('cloud_export', target))
        if s.state.cloud_write_ok:
            s.state.cloud_export_rule = target
        return s.state.cloud_write_ok
    s.adapter.read_export_rule = read
    s.adapter.write_export_rule = write


@pytest.mark.asyncio
@pytest.mark.parametrize('original_rule', ['battery_ok', 'pv_only', 'never'])
async def test_cloud_export_baseline_survives_complete_native_start_and_restore(setup, original_rule):
    s = setup
    cloud_export_fallback(s, original_rule)
    original = deepcopy(s.state.config), deepcopy(s.state.tariff)
    try:
        assert (await s.session.start(120))['active']
        assert s.state.cloud_export_rule == 'battery_ok'
        assert s.store.data['baseline']['export_rule_source'] == 'cloud'
        assert 'customer_preferred_export_rule' not in s.state.config['site_info']
        assert await s.session.stop()
        assert s.state.cloud_export_rule == original_rule
        assert (s.state.config, s.state.tariff) == original
        assert not s.session.pending and not s.budget.native_max_claim('site')
    finally:
        await s.session._cancel_watch()


@pytest.mark.asyncio
@pytest.mark.parametrize('cloud_rule', [None, '', 'invalid', True, {}, []])
async def test_native_export_missing_on_both_paths_never_starts(setup, cloud_rule):
    s = setup
    cloud_export_fallback(s, cloud_rule)
    assert not await s.adapter.guard()
    assert not s.state.events and not s.session.pending


def omitted_export_fallback(s, *, accepted=True, status='absent'):
    """A valid cloud reply omits the preference; the write has its own receipt."""
    permissions = importlib.import_module('native_max_adapter_probe.tesla_export_permissions')
    s.adapter.direction = 'discharge'
    s.adapter.startup_kick = True
    s.settings.update(optimization_max_discharge_w=14700,
                      optimization_max_grid_export_w=10000)
    s.state.config['site_info'].pop('customer_preferred_export_rule')
    s.adapter.read_export_rule = lambda: asyncio.sleep(
        0, result=permissions.ExportRuleReading(None, status))

    async def write(target, before_write, *, restoring=False, require_readback=True):
        assert not restoring and require_readback is False
        assert s.store.data['baseline']['export_permission_untracked'] is True
        assert 'export_rule' not in s.store.data['baseline']
        fence = await before_write()
        assert s.store.data['export_intent'] == target == 'battery_ok'
        assert s.state.config['site_info']['min_site_meter_power_ac'] == -10
        fence()
        s.state.events.append(('cloud_export', target))
        return accepted

    s.adapter.write_export_rule = write


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['absent', 'null'])
async def test_native_omitted_export_prepares_before_kick_and_restores_other_settings(setup, status):
    s = setup
    omitted_export_fallback(s, status=status)
    original = deepcopy(s.state.config), deepcopy(s.state.tariff)
    try:
        assert (await s.session.start(120))['active']
        kinds = [kind for kind, _ in s.state.events]
        assert kinds.index('cloud_export') < kinds.index('kick') < kinds.index('tariff')
        assert 'export_intent' not in s.store.data
        assert s.state.config['site_info']['min_site_meter_power_ac'] == -10
        assert await s.session.stop()
        assert (s.state.config, s.state.tariff) == original
        assert [event for event in s.state.events if event[0] == 'cloud_export'] == [
            ('cloud_export', 'battery_ok')]
        assert not s.session.pending and not s.budget.native_max_claim('site')
    finally:
        await s.session._cancel_watch()


@pytest.mark.asyncio
async def test_native_omitted_export_unacknowledged_write_blocks_forcing_and_retains_owner(setup):
    s = setup
    omitted_export_fallback(s, accepted=False)
    try:
        with pytest.raises(s.m.NativeMaxChargeError):
            await s.session.start(120)
        assert not any(kind in ('kick', 'tariff') for kind, _ in s.state.events)
        assert s.session.pending
        assert s.store.data['export_intent'] == 'battery_ok'
        assert s.budget.native_max_claim('site')
        # Restart cannot turn an omitted readback into evidence of completion.
        from copy import copy
        recovered = s.session.__class__(copy(s.adapter), s.store,
            clock=lambda: s.state.at, monotonic=lambda: s.state.at)
        with pytest.raises(s.m.NativeMaxChargeError):
            await recovered.recover()
        assert recovered.record['export_intent'] == 'battery_ok'
    finally:
        await s.session._cancel_watch()


@pytest.mark.asyncio
async def test_native_omitted_export_cannot_issue_a_startup_command_before_preparation(setup):
    from unittest.mock import AsyncMock
    s = setup
    omitted_export_fallback(s)
    assert await s.adapter.guard()
    baseline = await s.adapter.capture()
    with pytest.raises(s.m.NativeMaxChargeError, match='export preparation'):
        await s.adapter.kick(baseline, AsyncMock(), deadline=s.state.at + 120, write_intent=AsyncMock())
    assert not s.state.events


@pytest.mark.asyncio
@pytest.mark.parametrize('malformed', ['unsupported', False, {}, []])
async def test_native_omitted_cloud_cannot_replace_malformed_local_export(setup, malformed):
    s = setup
    omitted_export_fallback(s)
    s.state.config['site_info']['customer_preferred_export_rule'] = malformed
    with pytest.raises(s.m.NativeMaxChargeError):
        await s.session.start(120)
    assert not s.state.events and not s.session.pending


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['unavailable', 'invalid', 'conflict'])
async def test_native_export_requires_an_actual_cloud_omission_before_untracked_setup(setup, status):
    s = setup
    omitted_export_fallback(s, status=status)
    assert not await s.adapter.guard()
    assert not s.state.events and not s.session.pending


@pytest.mark.asyncio
async def test_cancelled_omitted_export_retains_terminal_receipt_and_restores_cap(setup):
    s = setup
    omitted_export_fallback(s)
    entered, release = asyncio.Event(), asyncio.Event()
    write = s.adapter.write_export_rule
    async def delayed(*args, **kwargs):
        result = await write(*args, **kwargs)
        entered.set()
        await release.wait()
        return result
    s.adapter.write_export_rule = delayed
    start = asyncio.create_task(s.session.start(120))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        start.cancel()
        await asyncio.sleep(0)
        assert not start.done()
        assert s.store.data['export_intent'] == 'battery_ok'
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await start
        assert not any(kind in ('kick', 'tariff') for kind, _ in s.state.events)
        assert not s.session.pending and s.store.data is None
        assert s.state.config['site_info']['min_site_meter_power_ac'] == -14.9
        assert not s.budget.native_max_claim('site')
    finally:
        release.set()
        await s.session._cancel_watch()


@pytest.mark.asyncio
async def test_uncertain_omitted_export_resolves_only_after_exact_later_readback(setup):
    s = setup
    omitted_export_fallback(s, accepted=False)
    try:
        with pytest.raises(s.m.NativeMaxChargeError):
            await s.session.start(120)
        assert s.session.record['export_intent'] == 'battery_ok'
        s.adapter.read_export_rule = lambda: asyncio.sleep(0, result='battery_ok')
        assert await s.session.stop()
        assert not s.session.pending and not s.budget.native_max_claim('site')
        assert [event for event in s.state.events if event[0] == 'cloud_export'] == [
            ('cloud_export', 'battery_ok')]
    finally:
        await s.session._cancel_watch()


@pytest.mark.asyncio
async def test_omitted_export_cleanup_preserves_later_external_preference(setup):
    s = setup
    omitted_export_fallback(s)
    try:
        await s.session.start(120)
        s.state.config['site_info']['customer_preferred_export_rule'] = 'never'
        assert not await s.adapter.guard()
        assert await s.session.stop()
        assert s.state.config['site_info']['customer_preferred_export_rule'] == 'never'
        assert s.state.config['site_info']['min_site_meter_power_ac'] == -14.9
        assert [event for event in s.state.events if event[0] == 'cloud_export'] == [
            ('cloud_export', 'battery_ok')]
    finally:
        await s.session._cancel_watch()


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['optimizer', 'user'])
async def test_automatic_routes_omitted_export_through_real_native_lifetime(setup, source):
    from test_powerwall_battery_dispatch import load_routing, call
    from unittest.mock import AsyncMock
    from datetime import timedelta
    s = setup
    omitted_export_fallback(s)
    s.settings['powerwall_native_capacity_w'] = 20000
    ns, local, client, _ = load_routing(tesla_control_method='auto',
        optimization_max_discharge_w=20000, optimization_max_grid_export_w=10000)
    # The route uses real selection with a fresh, balanced snapshot: site
    # export plus the home load exceeds the timed inverter ceiling.
    client.get_snapshot.return_value.load_w = 2000
    client.get_snapshot.return_value.solar_w = 0
    client.get_snapshot.return_value.battery_w = 0
    client.get_snapshot.return_value.grid_w = 2000
    now = ns['dt_util'].utcnow()
    ns['dt_util'].utcnow = lambda: now
    s.state.at = now.timestamp()
    ns.update(_native_max_context={}, _get_tesla_site_configs=lambda *_: [('site', 'token', 'provider')],
              _get_native_max_charge=AsyncMock(return_value=s.session),
              _create_discharge_tariff=lambda minutes: ({'name': 'Force export', 'code': 'FORCE'}, None),
              NativeMaxChargeError=s.m.NativeMaxChargeError)
    try:
        result = await ns['_try_local_powerwall_force']('discharge', call(source=source, power_w=0), 2, 0)
        assert result['control_path'] == 'powerwall_native_tariff'
        assert result['physical_effect_confirmed'] is False
        assert s.session.record['baseline']['export_permission_untracked'] is True
        assert s.session.record['expires_at'] == (now + timedelta(minutes=2)).timestamp()
        client.dispatch_battery_power.assert_not_awaited()
        assert not local.pending
        assert await s.session.stop()
    finally:
        await s.session._cancel_watch()


@pytest.mark.asyncio
@pytest.mark.parametrize('payload,status,expected', [
    ({'response': {'components': {'battery': True}}}, 200, 'absent'),
    ({'response': {'components': {'battery': True, 'customer_preferred_export_rule': None}}}, 200, 'null'),
    ({'response': {'components': {'battery': True, 'customer_preferred_export_rule': 'battery_ok'}}}, 200, 'known'),
    ({'response': {'components': {'battery': True, 'customer_preferred_export_rule': 'unknown'}}}, 200, 'invalid'),
    ({'response': {'site_name': 'thin reply'}}, 200, None),
    ({'response': {}}, 200, None),
    ({'response': {'components': {'battery': True}}}, 403, None),
])
async def test_native_cloud_reader_distinguishes_real_omission_from_failed_or_thin_reply(
        setup, payload, status, expected):
    from test_tesla_local_settings import HttpReply
    s = setup
    permissions = importlib.import_module('native_max_adapter_probe.tesla_export_permissions')
    grid = importlib.import_module('native_max_adapter_probe.tesla_grid_control')
    tree = ast.parse((ROOT / '__init__.py').read_text())
    factory = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
                   and n.name == '_get_native_max_charge_locked')
    read = deepcopy(next(n for n in factory.body if isinstance(n, ast.AsyncFunctionDef)
                         and n.name == 'read_export_rule'))
    read.body = [n for n in read.body if not isinstance(n, ast.ImportFrom)]
    namespace = dict(site_config=lambda: ('site', 'token', 'fleet'),
        read_export_preference=permissions.read_export_rule,
        tesla_site_info_has_structure=grid.tesla_site_info_has_structure,
        tesla_site_info_reports_components=grid.tesla_site_info_reports_components,
        hass=object(), entry=SimpleNamespace(data={}), CONF_FLEET_API_BASE_URL='fleet_base',
        get_tesla_api_base_url=lambda *args: 'https://tesla.test',
        async_get_clientsession=lambda hass: SimpleNamespace(get=lambda *args, **kwargs: HttpReply(payload, status)),
        aiohttp=__import__('aiohttp'))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[read], type_ignores=[])),
                 '<native-export-read>', 'exec'), namespace)
    result = await namespace['read_export_rule']()
    assert (result.status if result is not None else None) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('malformed', [
    {'direction': 'charge'}, {'export_rule': 'never'},
    {'export_permission_untracked': 'true'}, {'export_rule_source': 'cloud'},
])
async def test_untracked_native_journal_rejects_contradictory_baselines(setup, malformed):
    from copy import copy
    s = setup
    omitted_export_fallback(s)
    try:
        await s.session.start(120)
        await s.session._cancel_watch()
        journal = deepcopy(s.store.data)
        journal['baseline'].update(malformed)
        original_store = s.store
        fake_store = copy(original_store)
        fake_store.data = journal
        recovered = s.session.__class__(copy(s.adapter), fake_store,
            clock=lambda: s.state.at, monotonic=lambda: s.state.at)
        with pytest.raises(s.m.NativeMaxChargeError, match='journal'):
            await recovered.load()
    finally:
        await s.session.stop()
        await s.session._cancel_watch()


@pytest.mark.asyncio
@pytest.mark.parametrize('ack,accepted', [
    ({'response': {'result': True}}, True),
    ({'response': {'code': 204, 'message': 'Updated'}}, True),
    ({'response': ''}, True),
    ({'response': {'result': False}}, False),
    ({}, False),
])
async def test_native_omitted_export_uses_real_http_receipt_without_export_readback(
        setup, monkeypatch, ack, accepted):
    from test_tesla_local_settings import export_confirmation_context
    s = setup
    omitted_export_fallback(s)
    real_sleep = asyncio.sleep
    helper, _ns, _runtime, http = export_confirmation_context(monkeypatch, ack, {})
    monkeypatch.setattr(asyncio, 'sleep', real_sleep)
    tree = ast.parse((ROOT / '__init__.py').read_text())
    factory = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
                   and n.name == '_get_native_max_charge_locked')
    write = next(n for n in factory.body if isinstance(n, ast.AsyncFunctionDef)
                 and n.name == 'write_export_rule')
    namespace = dict(site_config=lambda: ('site', 'token', 'fleet'),
        _tesla_force_apply_export_rule=helper, _native_max_identity=lambda: 'paired', identity='paired',
        NativeMaxExportNotSubmitted=s.m.NativeMaxExportNotSubmitted,
        NativeMaxExportRejected=s.m.NativeMaxExportRejected)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[write], type_ignores=[])),
                 '<native-export-write>', 'exec'), namespace)
    s.adapter.write_export_rule = namespace['write_export_rule']
    try:
        if accepted:
            assert (await s.session.start(120))['active']
            assert await s.session.stop()
        else:
            with pytest.raises(s.m.NativeMaxChargeError):
                await s.session.start(120)
            assert not any(kind in ('kick', 'tariff') for kind, _ in s.state.events)
        http.post.assert_called_once()
        http.get.assert_not_called()
    finally:
        await s.session._cancel_watch()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['identity', 'policy', 'expired'])
async def test_cloud_export_read_cannot_outlive_native_authority(setup, change):
    s = setup
    cloud_export_fallback(s, 'battery_ok')
    async def delayed():
        if change == 'identity': s.state.identity = 'other-site'
        if change == 'policy': s.state.policy = False
        if change == 'expired': s.state.at += 6
        return 'battery_ok'
    s.adapter.read_export_rule = delayed
    assert not await s.adapter.guard()
    assert not s.state.events


@pytest.mark.asyncio
async def test_known_local_export_does_not_require_cloud_availability(setup):
    from unittest.mock import AsyncMock
    s = setup
    s.adapter.direction = 'discharge'
    s.settings.update(optimization_max_discharge_w=14700)
    s.adapter.read_export_rule = AsyncMock(side_effect=AssertionError('unnecessary cloud read'))
    assert await s.adapter.guard()
    s.adapter.read_export_rule.assert_not_awaited()


@pytest.mark.asyncio
async def test_unconfirmed_cloud_permission_write_retains_restore_journal(setup):
    s = setup
    cloud_export_fallback(s, 'pv_only')
    s.state.cloud_write_ok = False
    try:
        with pytest.raises(s.m.NativeMaxChargeError):
            await s.session.start(120)
        assert s.session.pending
        assert s.store.data['export_intent'] == 'battery_ok'
        assert s.budget.native_max_claim('site')
        assert [e for e in s.state.events if e[0] == 'cloud_export'] == [('cloud_export', 'battery_ok')]
        with pytest.raises(s.m.NativeMaxChargeError):
            await s.session.stop()
        assert [e for e in s.state.events if e[0] == 'cloud_export'] == [('cloud_export', 'battery_ok')]
        # A later read proves the previously uncertain request arrived.
        s.state.cloud_export_rule = 'battery_ok'
        s.state.cloud_write_ok = True
        assert await s.session.stop()
        assert s.state.cloud_export_rule == 'pv_only'
        assert not s.session.pending
    finally:
        await s.session._cancel_watch()


@pytest.mark.asyncio
async def test_cloud_export_original_and_uncertain_write_survive_restart(setup):
    from copy import copy
    s = setup
    cloud_export_fallback(s, 'never')
    s.state.cloud_write_ok = False
    try:
        with pytest.raises(s.m.NativeMaxChargeError):
            await s.session.start(120)
    finally:
        await s.session._cancel_watch()
    adapter = copy(s.adapter)
    adapter._export_observation = None
    adapter._cloud_export_owned = False
    recovered = s.session.__class__(adapter, s.store,
        clock=lambda: s.state.at, monotonic=lambda: s.state.at)
    await recovered.load()
    assert recovered.record['baseline']['export_rule'] == 'never'
    assert recovered.record['baseline']['export_rule_source'] == 'cloud'
    try:
        with pytest.raises(s.m.NativeMaxChargeError):
            await recovered.stop()
        assert recovered.record['export_intent'] == 'battery_ok'
        s.state.cloud_export_rule = 'battery_ok'
        s.state.cloud_write_ok = True
        assert await recovered.stop()
        assert s.state.cloud_export_rule == 'never'
        assert 'customer_preferred_export_rule' not in s.state.config['site_info']
    finally:
        await recovered._cancel_watch()


@pytest.mark.asyncio
async def test_cloud_outage_does_not_prevent_native_stop_but_keeps_export_recovery(setup):
    s = setup
    cloud_export_fallback(s, 'pv_only')
    original_config = deepcopy(s.state.config)
    read = s.adapter.read_export_rule
    try:
        await s.session.start(120)
        async def unavailable(): return None
        s.adapter.read_export_rule = unavailable
        with pytest.raises(s.m.NativeMaxChargeError):
            await s.session.stop()
        assert s.state.config == original_config
        assert s.state.tariff == s.original[1]
        assert s.state.cloud_export_rule == 'battery_ok'
        assert s.session.pending and s.budget.native_max_claim('site')
        s.adapter.read_export_rule = read
        assert await s.session.stop()
        assert s.state.cloud_export_rule == 'pv_only'
    finally:
        await s.session._cancel_watch()


@pytest.mark.asyncio
async def test_uncertain_cloud_restoration_is_not_resubmitted_or_reversed(setup):
    s = setup
    cloud_export_fallback(s, 'pv_only')
    try:
        await s.session.start(120)
        s.state.cloud_write_ok = False
        with pytest.raises(s.m.NativeMaxChargeError):
            await s.session.stop()
        assert s.store.data['export_intent'] == 'pv_only'
        sent = [e for e in s.state.events if e[0] == 'cloud_export']
        with pytest.raises(s.m.NativeMaxChargeError):
            await s.session.stop()
        assert [e for e in s.state.events if e[0] == 'cloud_export'] == sent
        s.state.cloud_export_rule = 'pv_only'
        assert await s.session.stop()
        assert [e for e in s.state.events if e[0] == 'cloud_export'] == sent
    finally:
        await s.session._cancel_watch()


@pytest.mark.asyncio
async def test_explicit_export_conflict_cannot_disappear_by_losing_one_source(setup):
    from copy import copy
    s = setup
    cloud_export_fallback(s, 'battery_ok')
    try:
        await s.session.start(120)
        s.state.config['site_info']['customer_preferred_export_rule'] = 'never'
        assert not await s.adapter.guard()
        with pytest.raises(s.m.NativeMaxChargeError):
            await s.session.stop()
        assert s.store.data['export_conflict'] is True
    finally:
        await s.session._cancel_watch()
    s.state.config['site_info'].pop('customer_preferred_export_rule')
    adapter = copy(s.adapter)
    adapter._export_observation = None
    adapter._export_conflict = False
    recovered = s.session.__class__(adapter, s.store,
        clock=lambda: s.state.at, monotonic=lambda: s.state.at)
    try:
        with pytest.raises(s.m.NativeMaxChargeError):
            await recovered.stop()
        assert recovered.record['export_conflict'] is True
        s.state.config['site_info']['customer_preferred_export_rule'] = 'battery_ok'
        assert await recovered.stop()
    finally:
        await recovered._cancel_watch()


@pytest.mark.asyncio
async def test_manual_max_uses_native_settings_and_restores_exact_original(setup):
    s=setup
    assert (await s.session.start(120))['active']
    assert s.state.config['default_real_mode']=='autonomous'
    assert s.state.config['site_info']['backup_reserve_percent']==100
    assert s.state.events[0][0]=='resume'
    assert s.state.config['site_info']['customer_preferred_export_rule']=='pv_only'
    assert s.state.config['site_info']['disallow_charge_from_grid_with_solar_installed'] is False
    assert s.settings['optimization_max_charge_w']==14700
    assert await s.session.stop()
    assert (s.state.config,s.state.tariff)==s.original
    assert not s.budget.native_max_claim('site')


@pytest.mark.asyncio
async def test_drained_price_upload_becomes_exact_native_restore_baseline(setup):
    s = setup
    reserve = importlib.import_module(s.m.__package__.split('.')[0]+'.tesla_tariff_upload').reserve_tariff_upload
    key = ('provider', 'base', 'site')
    slot = {'busy': True, 'retry_at': 0.0}
    domain = {'_tesla_tou_uploads': {key: slot}}
    latest = {'code': 'REAL', 'name': 'latest normal price tariff'}
    async def acquire():
        async with reserve(domain, key, timeout=1):
            return await s.session.start(120)
    request = asyncio.create_task(acquire())
    try:
        await asyncio.sleep(0)
        assert not s.store.data and not s.state.events
        s.state.tariff = deepcopy(latest)
        slot['busy'] = False
        assert (await request)['active']
        assert s.store.data['baseline']['tariff'] == latest
        await s.session.stop()
        assert s.state.tariff == latest
        assert not s.session.pending and not s.budget.native_max_claim('site')
    finally:
        if not request.done():
            request.cancel()
        await asyncio.gather(request, return_exceptions=True)
    assert 'reservation' not in slot


@pytest.mark.asyncio
@pytest.mark.parametrize('direction', ['charge', 'discharge'])
async def test_native_max_guard_recovers_empty_dcq_without_writing_or_claiming(setup, direction):
    from unittest.mock import AsyncMock
    from test_powerwall_v1r_teslemetry_parity import (
        _bare_client, _dcq_snapshot_reply, transport_mod,
    )
    s = setup
    gateway = _bare_client()
    gateway._transport = SimpleNamespace(post_v1r=AsyncMock(side_effect=[
        transport_mod.TEDAPIResponse(True, None), _dcq_snapshot_reply()]),
        read_config=AsyncMock(side_effect=lambda *_: deepcopy(s.state.config)))
    s.adapter.client.get_snapshot = gateway.get_snapshot
    s.adapter.direction = direction

    assert await s.adapter.guard()
    assert gateway._transport.post_v1r.await_count == 2
    assert not s.state.events
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
@pytest.mark.parametrize('planning', [False, True], ids=['manual_max', 'optimizer'])
@pytest.mark.parametrize('permission', ['missing', None, 'false', 0, 1, [], {}])
async def test_unknown_grid_permission_never_admits_native_charge(setup, planning, permission):
    s = setup
    s.adapter.planning_request = planning
    s.settings['powerwall_native_capacity_w'] = 14700
    field = 'disallow_charge_from_grid_with_solar_installed'
    if permission == 'missing':
        del s.state.config['site_info'][field]
    else:
        s.state.config['site_info'][field] = permission
    assert not await s.adapter.guard()
    assert s.adapter.guard_denial_reason == 'grid_charging_permission_unknown'
    with pytest.raises(s.m.NativeMaxChargeError, match='policy denied'):
        await s.session.start(120)
    assert s.state.events == []
    assert s.store.data is None and not s.session.pending
    assert not s.budget.native_max_claim('site')


@pytest.mark.asyncio
@pytest.mark.parametrize('planning', [False, True], ids=['manual_max', 'optimizer'])
async def test_native_charge_does_not_accept_permission_disappearing_before_write(setup, planning):
    s = setup
    s.adapter.planning_request = planning
    s.settings['powerwall_native_capacity_w'] = 14700
    assert await s.adapter.guard()
    del s.state.config['site_info']['disallow_charge_from_grid_with_solar_installed']
    assert not s.adapter._forcing_permitted(s.state.config)
    assert s.adapter.guard_denial_reason == 'grid_charging_permission_unknown'
    assert not s.state.events


@pytest.mark.asyncio
@pytest.mark.parametrize('settings,expected', [
    ({'optimization_max_grid_import_w': 14000}, 'software_import_limit_requires_local_control'),
    ({'optimization_grid_charge_soc_cap': .4}, 'charge_soc_limit_reached_or_unknown'),
])
async def test_native_charge_guard_retains_the_specific_admission_failure(setup, settings, expected):
    s = setup
    s.settings.update(settings)
    assert not await s.adapter.guard()
    assert s.adapter.guard_denial_reason == expected
    assert not s.state.events and not s.budget.native_max_claim('site')
    s.settings.update(optimization_max_grid_import_w=15000, optimization_grid_charge_soc_cap=1)
    assert await s.adapter.guard()
    assert s.adapter.guard_denial_reason is None


@pytest.mark.asyncio
@pytest.mark.parametrize('case,expected', [
    ('missing_export_limit', 'native_export_limit_missing'),
    ('invalid_export_limit', 'native_site_limits_invalid'),
    ('null_export_limit', 'native_export_limit_invalid'),
    ('invalid_export_rule', 'native_export_rule_invalid'),
    ('zero_export_limit', 'native_export_limit_blocks_discharge'),
    ('reserve_floor', 'discharge_reserve_floor_reached'),
    ('missing_soc', 'battery_soc_unavailable'),
    ('off_grid', 'grid_connection_unconfirmed'),
    ('missing_capacity', 'native_capacity_unavailable'),
    ('stale_snapshot', 'native_snapshot_stale'),
    ('managed_ev', 'managed_ev_ownership_present'),
    ('foreign_claim', 'native_max_site_already_owned'),
    ('identity_changed', 'native_max_identity_changed'),
    ('policy_denied', 'native_max_control_policy_denied'),
    ('local_disabled', 'native_max_local_access_disabled'),
    ('invalid_dispatch_config', 'native_max_dispatch_config_invalid'),
])
async def test_discharge_admission_logs_reason_without_starting_control(setup, caplog, case, expected):
    s = setup
    s.adapter.direction = 'discharge'
    info = s.state.config['site_info']
    if case == 'missing_export_limit':
        del info['min_site_meter_power_ac']
    elif case == 'invalid_export_limit':
        info['min_site_meter_power_ac'] = 'SECRET_INVALID_GATEWAY_VALUE'
    elif case == 'null_export_limit':
        info['min_site_meter_power_ac'] = None
    elif case == 'invalid_export_rule':
        info['customer_preferred_export_rule'] = 'SECRET_EXPORT_RULE'
    elif case == 'zero_export_limit':
        info['min_site_meter_power_ac'] = 0
    elif case == 'reserve_floor':
        s.state.soc = 5
    elif case == 'missing_soc':
        s.state.soc = None
    elif case == 'off_grid':
        s.state.grid = 'SystemIslandedActive'
    elif case == 'missing_capacity':
        s.adapter.planning_request = True
    elif case == 'stale_snapshot':
        original = s.adapter.client.get_snapshot
        async def slow_snapshot():
            result = await original()
            s.state.at += 6
            return result
        s.adapter.client.get_snapshot = slow_snapshot
    elif case == 'managed_ev':
        await s.budget.admit_managed('SECRET_VIN',
            {'site': 'site', 'identity': 'SECRET_EV', 'reserved_w': 0}, {}, lambda: True)
    elif case == 'foreign_claim':
        await s.budget.claim_native_max('other', 'site', 'SECRET_TOKEN', s.state.at + 120)
    elif case == 'identity_changed':
        s.state.identity = 'SECRET_IDENTITY'
    elif case == 'policy_denied':
        s.state.policy = False
    elif case == 'local_disabled':
        s.adapter.client.local_access_enabled = False
    elif case == 'invalid_dispatch_config':
        s.settings['optimization_max_discharge_w'] = 0
    before = deepcopy(s.state.config), deepcopy(s.state.tariff)
    with pytest.raises(s.m.NativeMaxChargeError, match='policy denied'):
        await s.session.start(120)
    assert s.adapter.guard_denial_reason == expected
    assert f'reason={expected}' in caplog.text
    assert 'SECRET_' not in caplog.text
    assert not s.state.events and not s.session.pending
    assert (s.state.config, s.state.tariff) == before


@pytest.mark.asyncio
async def test_export_cap_recheck_logs_values_and_clears_after_success(setup, caplog):
    s = setup
    s.adapter.direction = 'discharge'
    s.settings['optimization_max_grid_export_w'] = 10000
    assert await s.adapter.guard()  # Acquisition may tighten this native cap.
    assert not s.adapter._forcing_permitted(require_export_limit=True)
    assert s.adapter.guard_denial_reason == 'native_export_limit_exceeds_software_limit'
    assert "'native_export_limit_w': 14900.0" in caplog.text
    assert "'software_export_limit_w': 10000" in caplog.text
    assert not s.state.events
    s.state.config['site_info']['min_site_meter_power_ac'] = -10
    assert await s.adapter.guard()
    assert s.adapter.guard_denial_reason is None


@pytest.mark.asyncio
async def test_optimizer_discharge_admits_reported_capacity_after_reserve_cache_invalidation(setup):
    """Exercise the service config reader and real adapter in the reported gap."""
    from test_powerwall_battery_dispatch import load_routing
    import time
    s = setup
    ns, _, _, _ = load_routing(optimization_max_discharge_w=20000,
                               optimization_max_grid_export_w=30000)
    ns['_local_powerwall_runtime']['tesla_coordinator'] = SimpleNamespace(
        _site_info_cache=None, _site_info_last_fetch=0,
        _site_capacity_cache={'nameplate_power': 20000},
        _site_capacity_last_fetch=time.monotonic())
    s.adapter.direction = 'discharge'
    s.adapter.planning_request = True
    s.adapter.config = ns['_local_powerwall_dispatch_config']
    assert s.adapter.config()['powerwall_native_capacity_w'] == 20000
    assert (await s.session.start(240))['active']
    assert await s.session.stop()
    assert (s.state.config, s.state.tariff) == s.original


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


def native_http_upload(setup, upload_harness):
    """Use the real native closure and sender against the fake HTTP boundary."""
    s = setup
    sender, hass, http, clock = upload_harness
    tree = ast.parse((ROOT/'__init__.py').read_text())
    get = next(node for node in ast.walk(tree)
               if isinstance(node, ast.AsyncFunctionDef) and node.name == '_get_native_max_charge_locked')
    upload = next(node for node in get.body
                  if isinstance(node, ast.AsyncFunctionDef) and node.name == 'upload')
    namespace = dict(site_config=lambda: ('site', 'token', 'fleet_api'),
                     _local_powerwall_runtime={}, hass=hass,
                     entry=SimpleNamespace(data={}), CONF_FLEET_API_BASE_URL='fleet_base',
                     send_tariff_to_tesla=sender, _native_max_identity=lambda: 'paired',
                     identity='paired', HomeAssistantError=s.m.NativeMaxChargeError,
                     NativeMaxTariffNotSubmitted=getattr(s.m, 'NativeMaxTariffNotSubmitted', s.m.NativeMaxChargeError),
                     NativeMaxTariffRejected=getattr(s.m, 'NativeMaxTariffRejected', s.m.NativeMaxChargeError))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[upload], type_ignores=[])),
                 '<native-tariff-upload>', 'exec'), namespace)
    s.adapter.upload = namespace['upload']
    post = http.post

    def apply_accepted_tariff(url, **kwargs):
        response = post(url, **kwargs)
        if response.status == 200:
            s.state.tariff = deepcopy(kwargs['json']['tou_settings']['tariff_content_v2'])
        return response

    http.post = apply_accepted_tariff
    return http, clock


@pytest.mark.asyncio
@pytest.mark.parametrize('direction', ['charge', 'discharge'])
async def test_policy_denial_after_journaling_before_post_releases_native_claim(setup, upload_harness, direction):
    s = setup
    s.adapter.direction = direction
    http, _clock = native_http_upload(s, upload_harness)
    save = s.store.async_save

    async def deny_after_intent(data):
        await save(data)
        if data and data.get('tariff_intent') == 'forcing':
            s.state.policy = False

    s.store.async_save = deny_after_intent
    try:
        with pytest.raises(s.m.NativeMaxChargeError):
            await s.session.start(120)
        assert http.posts == []
        assert not s.session.pending and s.store.data is None
        assert not s.budget.native_max_claim('site')
        assert (s.state.config, s.state.tariff) == s.original
    finally:
        await s.session._cancel_watch()


@pytest.mark.asyncio
async def test_revoked_application_still_records_drained_provider_as_unsent(setup, upload_harness):
    s = setup
    s.adapter.direction = 'discharge'
    http, _clock = native_http_upload(s, upload_harness)
    save = s.store.async_save
    intent, resume = asyncio.Event(), asyncio.Event()

    async def pause_after_intent(data):
        await save(data)
        if data and data.get('tariff_intent') == 'forcing':
            intent.set()
            await resume.wait()

    s.store.async_save = pause_after_intent
    task = asyncio.create_task(s.session.start(120))
    try:
        await asyncio.wait_for(intent.wait(), 2)
        s.state.policy = False
        task.cancel()
        resume.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert http.posts == []
        assert s.store.data is None and not s.session.pending
        assert not s.budget.native_max_claim('site')
        assert (s.state.config, s.state.tariff) == s.original
    finally:
        resume.set()
        if not task.done():
            task.cancel()
        await s.session._cancel_watch()


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['timeout', 'server_error'])
async def test_http_boundary_unknown_outcome_keeps_native_intent_across_restart(setup, upload_harness, outcome):
    s = setup
    http, clock = native_http_upload(s, upload_harness)
    http.responses = [500]
    if outcome == 'timeout':
        def timed_out(url, **kwargs):
            http.posts.append((clock.now, url, kwargs['json']))
            raise TimeoutError('request may have been accepted')
        http.post = timed_out
    recovered = None
    try:
        with pytest.raises(s.m.NativeMaxChargeError):
            await s.session.start(120)
        await s.session._cancel_watch()
        assert len(http.posts) == 1
        assert s.store.data['tariff_intent'] == 'forcing'
        assert s.state.tariff == s.original[1]
        recovered = type(s.session)(s.adapter, s.store, clock=lambda: s.state.at,
                                    monotonic=lambda: s.state.at)
        with pytest.raises(s.m.NativeMaxChargeError):
            await recovered.recover()
        assert len(http.posts) == 1
        assert recovered.pending and s.budget.native_max_claim('site')
    finally:
        await s.session._cancel_watch()
        if recovered:
            await recovered._cancel_watch()


@pytest.mark.asyncio
@pytest.mark.parametrize('direction', ['charge', 'discharge'])
async def test_rejected_native_tariff_does_not_quarantine_site_or_bypass_cooldown(setup, upload_harness, direction):
    s = setup
    s.adapter.direction = direction
    http, clock = native_http_upload(s, upload_harness)
    try:
        with pytest.raises(s.m.NativeMaxChargeError):
            await s.session.start(120)
        assert not s.session.pending
        assert s.store.data is None and not s.budget.native_max_claim('site')
        assert (s.state.config, s.state.tariff) == s.original
        with pytest.raises(s.m.NativeMaxChargeError):
            await s.session.start(120)
        assert len(http.posts) == 1 and not s.session.pending
        clock.now = 282
        s.state.at += 282
        assert (await s.session.start(120))['active']
        assert len(http.posts) == 2
        assert await s.session.stop()
        assert (s.state.config, s.state.tariff) == s.original
    finally:
        await s.session._cancel_watch()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['self_consumption', 'autonomous'])
async def test_rejected_baseline_restore_can_retry_after_restart_and_cooldown(setup, upload_harness, mode):
    s = setup
    s.state.config['default_real_mode'] = mode
    original = deepcopy(s.state.config), deepcopy(s.state.tariff)
    http, clock = native_http_upload(s, upload_harness)
    http.responses = [200, 429, 200]
    recovered = None
    try:
        assert (await s.session.start(120))['active']
        with pytest.raises(s.m.NativeMaxChargeError):
            await s.session.stop()
        assert s.session.pending and s.budget.native_max_claim('site')
        assert not s.store.data.get('tariff_intent')
        await s.session._cancel_watch()
        recovered = type(s.session)(s.adapter, s.store, clock=lambda: s.state.at,
                                    monotonic=lambda: s.state.at)
        with pytest.raises(s.m.NativeMaxChargeError):
            await recovered.recover()
        assert len(http.posts) == 2 and recovered.pending
        await recovered._cancel_watch()
        clock.now = 282
        s.state.at += 282
        assert await recovered.recover()
        assert len(http.posts) == 3
        assert (s.state.config, s.state.tariff) == original
        assert s.store.data is None and not s.budget.native_max_claim('site')
    finally:
        await s.session._cancel_watch()
        if recovered:
            await recovered._cancel_watch()


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
@pytest.mark.parametrize('baseline_rule', ['battery_ok', 'pv_only', 'never'])
@pytest.mark.parametrize('readback', ['missing', 'null', 'offline', 'external'])
@pytest.mark.parametrize('unsettled', [False, True])
async def test_restart_releases_unchanged_export_rule_but_waits_for_physical_settling(
        setup, readback, unsettled, baseline_rule):
    s = setup
    s.adapter.direction = 'discharge'
    s.settings.update(optimization_max_discharge_w=20000,
                      optimization_max_grid_export_w=15000,
                      optimization_backup_reserve=.2)
    s.state.config['site_info']['customer_preferred_export_rule'] = baseline_rule
    await s.session.start(120)
    await s.session._cancel_watch()
    # Match a persisted discharge recovery whose other settings are released.
    s.store.data['phase'] = 'restoring'
    s.store.data['cleanup'] = dict(reserve_released=True, mode_released=True,
                                 tariff_released=True)
    s.state.config['default_real_mode'] = 'self_consumption'
    s.state.config['site_info']['backup_reserve_percent'] = 5
    s.state.tariff = deepcopy(s.original[1])
    s.state.battery_w = 10000 if unsettled else 0
    if readback == 'external':
        s.state.config['site_info']['customer_preferred_export_rule'] = 'never'
    read_config = s.adapter.client._transport.read_config
    async def unavailable_export(din):
        if readback == 'offline':
            raise OSError('local config unavailable')
        config = await read_config(din)
        if readback == 'missing':
            config['site_info'].pop('customer_preferred_export_rule')
        elif readback == 'null':
            config['site_info']['customer_preferred_export_rule'] = None
        return config
    s.adapter.client._transport.read_config = unavailable_export
    recovered = type(s.session)(s.adapter, s.store, clock=lambda: s.state.at,
                                monotonic=lambda: s.state.at)
    events = deepcopy(s.state.events)
    try:
        if baseline_rule != 'battery_ok' and readback != 'external':
            with pytest.raises(s.m.NativeMaxChargeError, match='restore was not confirmed'):
                await recovered.recover()
            assert s.store.data['phase'] == 'restoring'
            assert not s.store.data['cleanup'].get('export_released')
            assert s.budget.native_max_claim('site')
            assert s.state.events == events
            return
        if unsettled:
            with pytest.raises(s.m.NativeMaxChargeError, match='physical settling'):
                await recovered.recover()
            await recovered._cancel_watch()
            assert s.store.data['cleanup']['export_released']
            assert s.store.data['phase'] == 'settling'
            assert s.budget.native_max_claim('site')
            s.state.battery_w = 0
        assert await recovered.recover()
        assert not recovered.pending and not s.store.data
        assert not s.budget.native_max_claim('site')
        assert s.state.events == events
        assert s.state.config['site_info']['customer_preferred_export_rule'] == (
            'never' if readback == 'external' else 'battery_ok')
    finally:
        await recovered._cancel_watch()


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
async def test_charge_restore_accepts_solar_surplus_without_releasing_grid_charging(setup):
    s = setup
    await s.session.start(120)
    assert s.store.data['baseline']['settle_charge_w'] == 0
    s.state.solar_w = 4500
    s.state.load_w = 1000
    s.state.battery_w = -20000
    with pytest.raises(s.m.NativeMaxChargeError, match='settling'):
        await s.session.stop()
    assert s.session.pending and s.budget.native_max_claim('site')
    assert (s.state.config, s.state.tariff) == s.original
    count = len(s.state.events)
    s.state.battery_w = -3500
    assert await s.session.stop()
    assert len(s.state.events) == count
    assert not s.session.pending and not s.budget.native_max_claim('site')


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
@pytest.mark.parametrize('startup_kick', [False, True])
async def test_optimizer_charge_enables_initially_disabled_grid_then_restores_it(setup, startup_kick):
    s = setup
    field = 'disallow_charge_from_grid_with_solar_installed'
    s.state.config['site_info'][field] = True
    original = deepcopy(s.state.config), deepcopy(s.state.tariff)
    s.adapter.planning_request = True
    s.adapter.startup_kick = startup_kick
    s.settings.update(powerwall_native_capacity_w=20000, optimization_max_charge_w=20000)
    try:
        assert (await s.session.start(180 * 60))['active']
        assert s.state.events[0] == ('config', {'site_info.' + field: False})
        assert s.state.config['site_info'][field] is False
        assert s.store.data['baseline']['grid_charge_blocked'] is True
        # A later device-side disable ends forcing rather than being enabled again.
        s.state.config['site_info'][field] = True
        assert not await s.adapter.guard()
        s.state.config['site_info'][field] = False
        assert await s.session.stop()
        assert (s.state.config, s.state.tariff) == original
        assert not s.budget.native_max_claim('site')
    finally:
        await s.session.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('denial', ['permission', 'site_cap', 'soc', 'grid', 'policy', 'ev'])
async def test_optimizer_grid_setup_cannot_bypass_current_safety_policy(setup, denial):
    s = setup
    s.adapter.planning_request = True
    s.settings['powerwall_native_capacity_w'] = 20000
    s.state.config['site_info']['disallow_charge_from_grid_with_solar_installed'] = True
    if denial == 'permission': s.settings['optimization_allow_grid_charge'] = False
    if denial == 'site_cap': s.settings['optimization_max_grid_import_w'] = 14000
    if denial == 'soc': s.state.soc = 100
    if denial == 'grid': s.state.grid = 'SystemIslandedActive'
    if denial == 'policy': s.state.policy = False
    if denial == 'ev': s.budget.armed_sites.add('site')
    with pytest.raises(s.m.NativeMaxChargeError, match='policy denied'):
        await s.session.start(120)
    assert not s.state.events and not s.session.pending


@pytest.mark.asyncio
async def test_uncertain_grid_enable_restores_after_restart_without_starting_charge(setup):
    s = setup
    field = 'disallow_charge_from_grid_with_solar_installed'
    s.state.config['site_info'][field] = True
    original = deepcopy(s.state.config), deepcopy(s.state.tariff)
    s.adapter.planning_request = True
    s.settings['powerwall_native_capacity_w'] = 20000
    transport = s.adapter.client._transport
    write = transport.write_config
    async def uncertain(din, updates, **kwargs):
        if updates.get('site_info.' + field) is False:
            assert s.store.data['baseline']['grid_charge_blocked'] is True
            result = await write(din, updates, **kwargs)
            assert result and s.store.data['grid_charge_intent'] is True
            raise TimeoutError('enable may have arrived')
        if updates.get('site_info.' + field) is True:
            return False
        return await write(din, updates, **kwargs)
    transport.write_config = uncertain
    with pytest.raises(s.m.NativeMaxChargeError, match='provider timed out'):
        await s.session.start(120)
    assert s.session.pending and s.budget.native_max_claim('site')
    assert s.state.config['site_info'][field] is False
    assert all(event[0] == 'config' for event in s.state.events)
    await s.session._cancel_watch()
    transport.write_config = write
    restarted = s.session.__class__(s.adapter, s.store, clock=lambda: s.state.at, monotonic=lambda: s.state.at)
    assert await restarted.recover()
    assert (s.state.config, s.state.tariff) == original
    assert not s.budget.native_max_claim('site') and not s.store.data


@pytest.mark.asyncio
async def test_grid_setup_requires_confirmed_readback_before_any_force_write(setup):
    s = setup
    field = 'disallow_charge_from_grid_with_solar_installed'
    s.state.config['site_info'][field] = True
    s.adapter.planning_request = True
    s.settings['powerwall_native_capacity_w'] = 20000
    write = s.adapter.client._transport.write_config
    async def unchanged(din, updates, **kwargs):
        result = await write(din, updates, **kwargs)
        if updates.get('site_info.' + field) is False:
            s.state.config['site_info'][field] = True
        return result
    s.adapter.client._transport.write_config = unchanged
    with pytest.raises(s.m.NativeMaxChargeError, match='preparation readback'):
        await s.session.start(120)
    assert not any(event[0] in ('tariff', 'kick', 'resume') for event in s.state.events)
    assert s.state.config['site_info'][field] is True
    assert not s.session.pending


@pytest.mark.asyncio
async def test_external_enable_before_grid_setup_is_not_adopted_as_owned(setup):
    s = setup
    field = 'disallow_charge_from_grid_with_solar_installed'
    s.state.config['site_info'][field] = True
    s.adapter.planning_request = True
    s.settings['powerwall_native_capacity_w'] = 20000
    write = s.adapter.client._transport.write_config
    async def external_enable(din, updates, **kwargs):
        if updates.get('site_info.' + field) is False:
            s.state.config['site_info'][field] = False
        return await write(din, updates, **kwargs)
    s.adapter.client._transport.write_config = external_enable
    with pytest.raises(s.m.NativeMaxChargeError, match='preparation readback'):
        await s.session.start(120)
    assert s.state.config['site_info'][field] is False
    assert not s.state.events and not s.session.pending


@pytest.mark.asyncio
async def test_grid_disabled_again_before_tariff_submission_is_preserved(setup):
    s = setup
    field = 'disallow_charge_from_grid_with_solar_installed'
    s.state.config['site_info'][field] = True
    s.adapter.planning_request = True
    s.settings['powerwall_native_capacity_w'] = 20000
    async def external_disable(tariff, before_write):
        s.state.config['site_info'][field] = True
        await before_write()
        raise AssertionError('must not authorize tariff')
    s.adapter.upload = external_disable
    with pytest.raises(s.m.NativeMaxChargeError, match='policy denied'):
        await s.session.start(120)
    assert not any(event[0] == 'tariff' for event in s.state.events)
    assert s.state.config['site_info'][field] is True
    assert not s.session.pending


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
@pytest.mark.parametrize('readback_seconds', [5, 6])
async def test_slow_confirmed_export_cap_refreshes_evidence_without_rewriting(setup, original_limit, readback_seconds):
    s = setup
    s.adapter.direction = 'discharge'
    s.adapter.planning_request = True
    s.settings.update(powerwall_native_capacity_w=15000, optimization_max_discharge_w=15000,
                      optimization_max_grid_export_w=10000)
    s.state.config['site_info']['min_site_meter_power_ac'] = original_limit
    original = deepcopy(s.state.config)
    read = s.adapter.client._transport.read_config
    delayed = False

    async def slow_cap_readback(din):
        nonlocal delayed
        current = await read(din)
        if not delayed and current['site_info']['min_site_meter_power_ac'] == -10:
            delayed = True
            s.state.at += readback_seconds
        return current

    s.adapter.client._transport.read_config = slow_cap_readback
    try:
        assert (await s.session.start(120))['active']
        assert delayed
        assert s.session.record['expires_at'] == 1120
        assert sum(e == ('config', {'site_info.min_site_meter_power_ac': -10})
                   for e in s.state.events) == 1
        assert any(e[0] == 'tariff' for e in s.state.events)
        assert await s.session.stop()
        assert s.state.config == original
        assert not s.budget.native_max_claim('site')
    finally:
        await s.session._cancel_watch()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['policy', 'raised_cap', 'unknown_cap', 'slow_snapshot'])
async def test_slow_export_cap_readback_never_bypasses_new_safety_evidence(setup, change):
    s = setup
    s.adapter.direction = 'discharge'
    s.settings.update(optimization_max_discharge_w=15000, optimization_max_grid_export_w=10000)
    s.state.config['site_info']['min_site_meter_power_ac'] = -1e9
    read = s.adapter.client._transport.read_config
    snapshot = s.adapter.client.get_snapshot
    delayed = False

    async def slow_cap_readback(din):
        nonlocal delayed
        current = await read(din)
        if not delayed and current['site_info']['min_site_meter_power_ac'] == -10:
            delayed = True
            s.state.at += 6
            if change == 'policy':
                s.state.policy = False
            elif change == 'raised_cap':
                s.state.config['site_info']['min_site_meter_power_ac'] = -20
            elif change == 'unknown_cap':
                s.state.config['site_info'].pop('min_site_meter_power_ac')
        return current

    async def fresh_snapshot():
        if delayed and change == 'slow_snapshot':
            s.state.at += 6
        return await snapshot()

    s.adapter.client._transport.read_config = slow_cap_readback
    s.adapter.client.get_snapshot = fresh_snapshot
    try:
        with pytest.raises(s.m.NativeMaxChargeError):
            await s.session.start(120)
        assert delayed
        assert not any(e[0] == 'tariff' for e in s.state.events)
    finally:
        await s.session._cancel_watch()


@pytest.mark.asyncio
async def test_cancel_during_export_cap_evidence_refresh_restores_without_forcing(setup):
    s = setup
    s.adapter.direction = 'discharge'
    s.settings.update(optimization_max_discharge_w=15000, optimization_max_grid_export_w=10000)
    s.state.config['site_info']['min_site_meter_power_ac'] = -1e9
    original = deepcopy(s.state.config)
    read = s.adapter.client._transport.read_config
    snapshot = s.adapter.client.get_snapshot
    delayed = blocked = False
    waiting = asyncio.Event()

    async def slow_cap_readback(din):
        nonlocal delayed
        current = await read(din)
        if not delayed and current['site_info']['min_site_meter_power_ac'] == -10:
            delayed = True
            s.state.at += 6
        return current

    async def blocked_refresh():
        nonlocal blocked
        if delayed and not blocked:
            blocked = True
            waiting.set()
            await asyncio.Event().wait()
        return await snapshot()

    s.adapter.client._transport.read_config = slow_cap_readback
    s.adapter.client.get_snapshot = blocked_refresh
    task = asyncio.create_task(s.session.start(120))
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not any(e[0] == 'tariff' for e in s.state.events)
        assert s.state.config == original
        assert not s.session.pending and not s.budget.native_max_claim('site')
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await s.session._cancel_watch()


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


@pytest.mark.asyncio
async def test_guard_uses_successful_retry_acquisition_time_not_first_failed_read(setup):
    s = setup
    get_snapshot = s.adapter.client.get_snapshot
    async def internally_retried_snapshot():
        # A failed read consumed six seconds; the actual returned sample is new.
        s.state.at += 6
        snap = await get_snapshot()
        snap.observed_monotonic = s.state.at
        s.state.at += 1
        return snap
    s.adapter.client.get_snapshot = internally_retried_snapshot
    assert await s.adapter.guard()
    assert s.adapter._observed_at == s.state.at - 1
    assert not s.state.events


@pytest.mark.asyncio
async def test_guard_refreshes_once_after_slow_read_without_accepting_stale_sample(setup):
    s = setup
    get_snapshot = s.adapter.client.get_snapshot
    reads = 0
    async def slow_then_fresh():
        nonlocal reads
        reads += 1
        snap = await get_snapshot()
        if reads == 1: s.state.at += 6
        return snap
    s.adapter.client.get_snapshot = slow_then_fresh
    assert await s.adapter.guard()
    assert reads == 2
    assert not s.state.events


@pytest.mark.asyncio
async def test_native_config_read_refreshes_admission_before_submission(setup):
    s = setup
    original = s.adapter.client._transport.write_config
    async def delayed_config_read(*args, **kwargs):
        s.state.at += 6
        return await original(*args, **kwargs)
    s.adapter.client._transport.write_config = delayed_config_read
    assert (await s.session.start(120))['active']
    assert await s.session.stop()
    assert (s.state.config, s.state.tariff) == s.original


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['soc_full', 'grid_disconnected', 'policy_revoked'])
async def test_config_pre_submit_refresh_cannot_reuse_old_admission(setup, change):
    s = setup
    original = s.adapter.client._transport.write_config
    async def change_after_config_read(*args, **kwargs):
        s.state.at += 6
        if change == 'soc_full': s.state.soc = 100
        elif change == 'grid_disconnected': s.state.grid = 'SystemIslandedActive'
        else: s.state.policy = False
        return await original(*args, **kwargs)
    s.adapter.client._transport.write_config = change_after_config_read
    with pytest.raises(s.m.NativeMaxChargeError): await s.session.start(120)
    assert not any(kind == 'config' for kind, _ in s.state.events)


@pytest.mark.asyncio
@pytest.mark.parametrize('elapsed,expected', [(5, True), (5.001, False)])
async def test_final_config_submission_does_not_extend_freshness_through_storage(setup, elapsed, expected):
    s = setup
    assert await s.adapter.guard()
    baseline = await s.adapter.capture()
    async def authorize(config=None):
        assert await s.adapter.guard(native_config=config)
        return lambda: None
    async def store(*_): s.state.at += elapsed
    if expected:
        assert await s.adapter._write_config('autonomous', 100, s.adapter._owned_before(baseline, authorize), baseline, store)
    else:
        with pytest.raises(s.m.NativeMaxChargeError):
            await s.adapter._write_config('autonomous', 100, s.adapter._owned_before(baseline, authorize), baseline, store)
        assert not any(kind == 'config' for kind, _ in s.state.events)


@pytest.mark.asyncio
async def test_real_hash_checked_writer_refreshes_dcq_after_slow_config_read(setup, monkeypatch):
    import json
    from test_powerwall_v1r_teslemetry_parity import (
        _bare_client, _transport_without_key, _dcq_snapshot_reply,
        client_mod, transport_mod, transport_pb2,
    )
    s = setup
    client = _bare_client()
    transport = _transport_without_key()
    client._transport = transport
    monkeypatch.setattr(client_mod, 'time', SimpleNamespace(monotonic=lambda:s.state.at))
    posts = []
    async def post(payload, din, **kwargs):
        request = transport_pb2.MessageEnvelope.FromString(payload)
        if not request.HasField('filestore'):
            return _dcq_snapshot_reply()
        reply = transport_pb2.MessageEnvelope()
        if request.filestore.HasField('read_file_request'):
            s.state.at += 6
            reply.filestore.read_file_response.file.blob = json.dumps(s.state.config).encode()
            reply.filestore.read_file_response.hash = b'current-config-hash'
        else:
            update = request.filestore.update_file_request
            assert update.hash == b'current-config-hash'
            await kwargs['before_submit'](s.state.at - 2)
            kwargs['before_send']()
            # Admission used the observed original, never the proposed edit.
            assert s.adapter._snapshot.raw['config']['default_real_mode'] == 'self_consumption'
            assert s.adapter._snapshot.raw['config']['site_info']['backup_reserve_percent'] == 5
            s.state.config = json.loads(update.file.blob)
            posts.append(s.state.config)
            reply.filestore.update_file_response.SetInParent()
        return transport_mod.TEDAPIResponse(True, reply.SerializeToString())
    transport.post_v1r = post
    s.adapter.client = client
    baseline = {'mode':'self_consumption','reserve':5,'tariff':deepcopy(s.state.tariff),
                'forcing_tariff':deepcopy(s.adapter.forcing_tariff)}
    async def authorize(config=None):
        assert await s.adapter.guard(native_config=config)
        return lambda: None
    async def store(*_): pass
    assert await s.adapter._write_config('autonomous',100,
        s.adapter._owned_before(baseline, authorize),baseline,store)
    assert len(posts) == 1 and posts[0]['site_info']['backup_reserve_percent'] == 100


@pytest.mark.asyncio
@pytest.mark.parametrize('planning', [False, True], ids=['manual_max', 'optimizer'])
@pytest.mark.parametrize('initially_disabled', [False, True])
async def test_attested_native_omission_admits_and_restores_owned_grid_setting(setup, planning, initially_disabled):
    from test_powerwall_grid_permission_encoding import normalization, system_info
    s = setup
    field = 'disallow_charge_from_grid_with_solar_installed'
    s.state.config.update(battery_blocks=[{}], meters=[{}], strategy={})
    if initially_disabled:
        s.state.config['site_info'][field] = True
    else:
        s.state.config['site_info'].pop(field)
    original = deepcopy(s.state.config), deepcopy(s.state.tariff)
    client = s.adapter.client
    client.native_grid_charging_reading = lambda config: normalization.native_grid_charging_reading(config,system_info(),'gateway')
    async def refresh(): pass
    client.refresh_native_grid_charging_firmware = refresh
    write = client._transport.write_config
    async def sparse_write(din, updates, **kwargs):
        result = await write(din, updates, **kwargs)
        if result and s.state.config['site_info'].get(field) is False:
            s.state.config['site_info'].pop(field)
        return result
    client._transport.write_config = sparse_write
    s.adapter.planning_request = planning
    s.settings['powerwall_native_capacity_w'] = 14700
    try:
        if initially_disabled and not planning:
            with pytest.raises(s.m.NativeMaxChargeError):
                await s.session.start(120)
            assert s.state.events == []
        else:
            assert (await s.session.start(120))['active']
            assert field not in s.state.config['site_info']
            assert await s.session.stop()
            assert (s.state.config,s.state.tariff) == original
            assert not s.session.pending and s.store.data is None
    finally:
        await s.session.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('planning',[False,True])
@pytest.mark.parametrize('boundary',['tariff','local'])
async def test_native_omission_cannot_outlive_build_attestation_before_write(setup,planning,boundary):
    from test_powerwall_grid_permission_encoding import normalization, system_info
    s = setup
    s.state.config.update(battery_blocks=[{}],meters=[{}],strategy={})
    s.state.config['site_info'].pop('disallow_charge_from_grid_with_solar_installed')
    original = deepcopy(s.state.config), deepcopy(s.state.tariff)
    firmware = system_info()
    s.adapter.client.native_grid_charging_reading = lambda config: normalization.native_grid_charging_reading(config,firmware,'gateway')
    s.adapter.planning_request = planning
    s.settings['powerwall_native_capacity_w'] = 14700
    assert await s.adapter.guard()
    if boundary == 'local':
        # Tariff admission happens first; revoke evidence before local forcing.
        s.state.before_validate = lambda: firmware.update(firmware_githash='f'*40)
    else:
        upload = s.adapter.upload
        async def changed_build(tariff,before_write):
            if tariff.get('code') == 'UNIQUE_FORCE':
                firmware['firmware_githash'] = 'f'*40
            return await upload(tariff,before_write)
        s.adapter.upload = changed_build
    with pytest.raises(s.m.NativeMaxChargeError):
        await s.session.start(120)
    assert not any(kind in ('config','kick') for kind,_ in s.state.events)
    if boundary == 'tariff':
        assert not any(kind == 'tariff' and value.get('code') == 'UNIQUE_FORCE' for kind,value in s.state.events)
    assert (s.state.config,s.state.tariff) == original
    assert not s.session.pending and s.store.data is None
