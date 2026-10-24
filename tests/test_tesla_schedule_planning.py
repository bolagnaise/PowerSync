"""Forecast-only preferences persist without changing any vehicle controller."""
import asyncio
import ast
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys

import pytest

ROOT = Path(__file__).parents[1] / 'custom_components/power_sync'
SPEC = importlib.util.spec_from_file_location('tesla_schedule_planning_probe', ROOT / 'tesla_schedule_planning.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
VIN = '5YJ3E1EA7KF000001'
OTHER = '5YJ3E1EA7KF000002'


@pytest.fixture
def api(monkeypatch):
    disk, writes, refreshes = {}, [], []
    fail = [False]
    class Store:
        def __init__(self, hass, version, key): self.key = key
        async def async_load(self): return deepcopy(disk.get(self.key))
        async def async_save(self, value):
            if fail[0]: raise RuntimeError('storage unavailable')
            disk[self.key] = deepcopy(value)
            writes.append((self.key, deepcopy(value)))
    storage = ModuleType('homeassistant.helpers.storage')
    storage.Store = Store
    monkeypatch.setitem(sys.modules, storage.__name__, storage)
    http = ModuleType('homeassistant.components.http')
    class Base:
        def json(self, value, status_code=200): return status_code, value
    http.HomeAssistantView = Base
    monkeypatch.setitem(sys.modules, http.__name__, http)
    entries = [SimpleNamespace(entry_id=name, title=name) for name in ('one', 'two')]
    views = []
    def forbidden(*args, **kwargs): raise AssertionError('a planning preference must not call a control service')
    hass = SimpleNamespace(data={
        'power_sync': {name: {'optimization_coordinator': SimpleNamespace(
            _schedule_settings_reoptimization=lambda name=name: refreshes.append(name)),
            'automation_store': SimpleNamespace(_data={'smart_schedule': 'unchanged'})} for name in ('one', 'two')},
        'power_sync_native_solar': {'one': {'holds': {VIN: {'enabled': True}}}},
    }, config_entries=SimpleNamespace(async_entries=lambda _: entries),
       http=SimpleNamespace(register_view=views.append), services=SimpleNamespace(async_call=forbidden))
    m.register_http_view(hass)
    return SimpleNamespace(view=views[0], hass=hass, disk=disk, writes=writes, refreshes=refreshes, fail=fail)


def request(body=None, vin=VIN):
    async def json(): return body
    return SimpleNamespace(query={'vin': vin}, json=json)


@pytest.mark.asyncio
async def test_default_on_get_has_no_provider_request_or_saved_change(api):
    status, result = await api.view.get(request())
    assert status == 200 and api.view.requires_auth
    assert [entry['enabled'] for entry in result['entries']] == [True, True]
    assert api.writes == [] and api.refreshes == []


@pytest.mark.asyncio
async def test_toggle_is_per_car_per_entry_survives_restart_and_preserves_controls(api):
    holds = deepcopy(api.hass.data['power_sync_native_solar'])
    status, saved = await api.view.post(request({'entry_id': 'one', 'vin': VIN.lower(), 'enabled': False}))
    assert status == 200 and saved['enabled'] is False and saved['vin'] == VIN
    assert api.refreshes == ['one']
    assert api.hass.data['power_sync_native_solar'] == holds
    assert api.hass.data['power_sync']['one']['automation_store']._data == {'smart_schedule': 'unchanged'}
    api.hass.data.pop(m.RUNTIME_KEY)
    _, restored = await api.view.get(request())
    assert [entry['enabled'] for entry in restored['entries']] == [False, True]
    _, another = await api.view.get(request(vin=OTHER))
    assert [entry['enabled'] for entry in another['entries']] == [True, True]
    await api.view.post(request({'entry_id': 'one', 'vin': OTHER, 'enabled': False}))
    await api.view.post(request({'entry_id': 'one', 'vin': VIN, 'enabled': True}))
    assert api.disk['power_sync.tesla_schedule_planning.one'] == {'vehicles': {OTHER: False}}
    assert api.hass.data['power_sync_native_solar'] == holds


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [None, [], {}, {'entry_id': 'missing', 'vin': VIN, 'enabled': False},
    {'entry_id': 'one', 'vin': 'bad', 'enabled': False}, {'entry_id': 'one', 'vin': VIN, 'enabled': 0},
    {'entry_id': 'one', 'vin': VIN, 'enabled': 'false'}])
async def test_invalid_request_never_saves_or_reoptimizes(api, body):
    status, _ = await api.view.post(request(body))
    assert status == 400 and not api.writes and not api.refreshes


@pytest.mark.asyncio
async def test_failed_persistence_never_changes_active_preference(api):
    await api.view.post(request({'entry_id': 'one', 'vin': VIN, 'enabled': False}))
    api.fail[0] = True
    with pytest.raises(RuntimeError, match='storage unavailable'):
        await api.view.post(request({'entry_id': 'one', 'vin': VIN, 'enabled': True}))
    _, result = await api.view.get(request())
    assert result['entries'][0]['enabled'] is False
    assert api.refreshes == ['one']
    assert api.disk['power_sync.tesla_schedule_planning.one'] == {'vehicles': {VIN: False}}


@pytest.mark.asyncio
async def test_rate_management_is_explicit_default_off_and_separate_from_forecast(api):
    _, result = await api.view.get(request())
    assert all(not entry['rate_management'] for entry in result['entries'])
    api.hass.async_create_task = lambda task: task.close()
    api.hass.data['power_sync']['one']['tesla_native_rate'] = SimpleNamespace(status={})
    async def tick(): pass
    api.hass.data['power_sync']['one']['tesla_native_rate'].tick = tick
    status, result = await api.view.post(request({'entry_id':'one','vin':VIN,'enabled':True,'rate_management':True}))
    assert status == 200 and result['rate_management'] is True
    assert api.disk['power_sync.tesla_schedule_planning.one']['rate_management'] == {VIN:True}
    api.hass.data.pop(m.RUNTIME_KEY)
    _, result = await api.view.get(request())
    assert result['entries'][0]['rate_management'] is True
    assert result['entries'][1]['rate_management'] is False
    await api.view.post(request({'entry_id':'one','vin':OTHER,'enabled':True}))
    _, result = await api.view.get(request(vin=OTHER))
    assert result['entries'][0]['rate_management'] is False
    await api.view.post(request({'entry_id':'one','vin':VIN,'enabled':False}))
    _, result = await api.view.get(request())
    assert result['entries'][0]['rate_management'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('rate', [None, 'true', 1, [], {}])
async def test_malformed_rate_optin_never_saves(api, rate):
    status, _ = await api.view.post(request({'entry_id':'one','vin':VIN,'enabled':True,'rate_management':rate}))
    assert status == 400 and not api.writes


@pytest.mark.asyncio
async def test_concurrent_car_updates_cannot_overwrite_each_other(api, monkeypatch):
    setting = await m.preferences(api.hass, 'one')
    started, finish = asyncio.Event(), asyncio.Event()
    save = setting.store.async_save
    async def delayed(value):
        if not started.is_set():
            started.set()
            await finish.wait()
        await save(value)
    monkeypatch.setattr(setting.store, 'async_save', delayed)
    first = asyncio.create_task(api.view.post(request({'entry_id': 'one', 'vin': VIN, 'enabled': False})))
    await started.wait()
    second = asyncio.create_task(api.view.post(request({'entry_id': 'one', 'vin': OTHER, 'enabled': False})))
    await asyncio.sleep(0)
    assert not second.done()
    finish.set()
    assert (await first)[0] == (await second)[0] == 200
    assert api.disk['power_sync.tesla_schedule_planning.one'] == {'vehicles': {VIN: False, OTHER: False}}


@pytest.mark.asyncio
async def test_malformed_legacy_storage_is_not_treated_as_a_disable(api):
    api.disk['power_sync.tesla_schedule_planning.one'] = {'vehicles': {VIN: 'false', OTHER.lower(): False, 'invalid': False}}
    _, result = await api.view.get(request())
    assert result['entries'][0]['enabled'] is True
    _, result = await api.view.get(request(vin=OTHER))
    assert result['entries'][0]['enabled'] is False
    assert m.excluded_vins(api.hass, 'one') == {OTHER}


@pytest.mark.asyncio
async def test_optimizer_refresh_obeys_native_preferences_with_smart_ev_disabled(api, monkeypatch):
    await api.view.post(request({'entry_id': 'one', 'vin': VIN, 'enabled': False}))
    package = ModuleType('schedule_planning_coordinator_probe')
    package.__path__ = [str(ROOT)]
    optimization = ModuleType(package.__name__ + '.optimization')
    optimization.__path__ = [str(ROOT / 'optimization')]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    monkeypatch.setitem(sys.modules, optimization.__name__, optimization)
    monkeypatch.setitem(sys.modules, package.__name__ + '.tesla_schedule_planning', m)
    calls = []
    class Reader:
        async def refresh(self, hass, now, **kwargs): calls.append(kwargs['excluded_vins'])
    reader_module = ModuleType(optimization.__name__ + '.tesla_schedules')
    reader_module.TeslaScheduleReader = Reader
    monkeypatch.setitem(sys.modules, reader_module.__name__, reader_module)
    solar = ModuleType(package.__name__ + '.teslemetry_solar')
    class Gate:
        lock = asyncio.Lock()
        async def load(self): pass
    async def ownership(*args): return Gate()
    solar.ownership = ownership
    monkeypatch.setitem(sys.modules, solar.__name__, solar)
    source = ast.parse((ROOT / 'optimization/coordinator.py').read_text())
    fn = next(node for node in ast.walk(source) if isinstance(node, ast.AsyncFunctionDef) and node.name == '_refresh_ev_forecast_inputs')
    ns = {'__package__': optimization.__name__, 'dt_util': SimpleNamespace(now=lambda: datetime.now(timezone.utc)),
          '_LOGGER': SimpleNamespace(debug=lambda *args: None)}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), '<coordinator>', 'exec'), ns)
    coordinator = SimpleNamespace(hass=api.hass, _entry=SimpleNamespace(entry_id='one'), _ev_integration_enabled=False)
    await ns[fn.name](coordinator)
    assert calls == [{VIN}]
