"""Mobile requests use the native service, and status never contacts Tesla."""
import ast
import __future__
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1] / 'custom_components/power_sync'
VIN = '5YJ3E1EA7KF000001'
SETTINGS = dict(enabled=True, lowerChargeLimit=50, upperChargeLimit=80)


@pytest.fixture
def view(monkeypatch):
    package = ModuleType('native_solar_api_probe')
    package.__path__ = [str(ROOT)]
    # Load the real credential helper without importing the full integration.
    # A fabricated helper here previously hid an import that broke HA setup.
    const = ModuleType(package.__name__ + '.const')
    for node in ast.parse((ROOT / 'const.py').read_text()).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id.startswith(
                ('CONF_TESLA', 'CONF_TESLEMETRY', 'TESLA_PROVIDER', 'TESLA_EV_API_PROVIDER')
            ):
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    continue
                setattr(const, target.id, value)
                package.__dict__[target.id] = value
    monkeypatch.setitem(sys.modules, const.__name__, const)
    for helper_name in ('get_tesla_vehicle_api_token', 'get_tesla_api_token'):
        helper = next(node for node in ast.parse((ROOT / '__init__.py').read_text()).body
                      if isinstance(node, ast.FunctionDef) and node.name == helper_name)
        exec(compile(ast.Module(body=[helper], type_ignores=[]), str(ROOT / '__init__.py'),
                     'exec', flags=__future__.annotations.compiler_flag), package.__dict__)
    monkeypatch.setitem(sys.modules, package.__name__, package)
    modules = {}
    for name in ('teslemetry_solar', 'teslemetry_solar_api'):
        spec = importlib.util.spec_from_file_location(f'{package.__name__}.{name}', ROOT / f'{name}.py')
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
        modules[name] = module
    native = modules['teslemetry_solar']
    class Store:
        async def async_load(self): return None
        async def async_save(self, value): pass
    gate = native.SolarOwnership(Store())
    async def ownership(*args): return gate
    monkeypatch.setattr(modules['teslemetry_solar_api'], 'ownership', ownership)
    http = ModuleType('homeassistant.components.http')
    class Base:
        def json(self, data, status_code=200): return status_code, data
    http.HomeAssistantView = Base
    monkeypatch.setitem(sys.modules, http.__name__, http)
    exceptions = ModuleType('homeassistant.exceptions')
    exceptions.HomeAssistantError = type('HomeAssistantError', (Exception,), {})
    monkeypatch.setitem(sys.modules, exceptions.__name__, exceptions)
    entry = SimpleNamespace(entry_id='one', title='Home', data={
        const.CONF_TESLA_API_PROVIDER: const.TESLA_PROVIDER_POWERSYNC,
        const.CONF_TESLEMETRY_API_TOKEN: 'site-secret',
        const.CONF_TESLA_EV_API_PROVIDER: const.TESLA_EV_API_PROVIDER_TESLEMETRY,
        const.CONF_TESLA_EV_TELEMETRY_TOKEN: 'secret',
    }, options={})
    calls, views = [], []
    async def service(*args, **kwargs):
        calls.append((args, kwargs))
        return {'settings': SETTINGS, 'readback_confirmed': True, 'powersync_control_blocked': True}
    hass = SimpleNamespace(data={'power_sync': {'one': {}}},
        config_entries=SimpleNamespace(async_entries=lambda _: [entry]),
        services=SimpleNamespace(async_call=service),
        http=SimpleNamespace(register_view=views.append))
    modules['teslemetry_solar_api'].register_http_view(hass)
    return views[0], gate, entry, calls


@pytest.mark.asyncio
@pytest.mark.parametrize('provider,energy_provider,available,native_provider', [
    ('teslemetry', 'powersync', True, 'teslemetry'),
    ('none', 'powersync', True, 'powersync'),
    ('none', 'teslemetry', False, 'none'),
])
async def test_startup_registration_and_status_use_real_vehicle_credentials(view, provider, energy_provider, available, native_provider):
    api, _, entry, calls = view
    entry.data['tesla_ev_api_provider'] = provider
    entry.data['tesla_api_provider'] = energy_provider
    status, result = await api.get(request())
    assert api.url == '/api/power_sync/ev/native_solar'
    assert status == 200 and result['success']
    assert result['entries'][0]['available'] is available
    assert result['entries'][0]['provider'] == native_provider
    assert calls == []
    assert 'secret' not in str(result)


def request(body=None):
    async def json(): return body
    return SimpleNamespace(query={'vin': VIN}, json=json)


@pytest.mark.asyncio
async def test_get_returns_uncertain_hold_without_any_vehicle_or_service_request(view):
    api, gate, entry, calls = view
    await gate.load()
    await gate.hold(VIN, {'status': 'unconfirmed'})
    entry.options['no_solar_installed'] = True
    status, result = await api.get(request())
    assert api.requires_auth and status == 200
    assert calls == []
    assert result['entries'][0]['status'] == 'unconfirmed'
    assert result['entries'][0]['powersync_control_blocked']
    assert result['entries'][0]['no_solar_installed']
    assert result['entries'][0]['cached_settings'] is None
    assert 'secret' not in str(result)


@pytest.mark.asyncio
async def test_get_cached_settings_exclude_internal_hold_aliases(view):
    api, gate, _, calls = view
    await gate.load()
    await gate.hold(VIN, {'enabled': True, 'lowerChargeLimit': 20, 'upperChargeLimit': 80,
                          'aliases': ['BLE_TESLA_FLINN']})
    status, result = await api.get(request())
    assert status == 200 and calls == []
    assert result['entries'][0]['status'] == 'cached'
    assert result['entries'][0]['cached_settings'] == {
        'enabled': True, 'lowerChargeLimit': 20, 'upperChargeLimit': 80}


@pytest.mark.asyncio
async def test_powersync_fleet_metadata_never_contacts_cloud_or_tesla(view):
    api, _, entry, calls = view
    entry.data.update(tesla_ev_api_provider='none', tesla_api_provider='powersync', teslemetry_api_token='psync-secret')
    status, result = await api.get(request())
    assert status == 200 and not calls
    assert result['entries'][0]['available']
    assert result['entries'][0]['provider'] == 'powersync'
    assert 'psync-secret' not in str(result)


@pytest.mark.asyncio
async def test_post_read_and_write_delegate_to_existing_interlocked_services(view):
    api, _, _, calls = view
    status, result = await api.post(request({'action': 'read', 'entry_id': 'one', 'vin': VIN.lower()}))
    assert status == 200 and result['readback_confirmed']
    assert calls[0] == (('power_sync', 'get_teslemetry_charge_on_solar', {'entry_id': 'one', 'vin': VIN}),
                        {'blocking': True, 'return_response': True})
    await api.post(request({'action': 'write', 'entry_id': 'one', 'vin': VIN,
                            'settings': SETTINGS, 'confirm_eligible': 'true'}))
    assert calls[1][0][1] == 'set_teslemetry_charge_on_solar'
    assert calls[1][0][2]['confirm_eligible'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [None, [], {},
    {'action': 'read', 'entry_id': 'missing', 'vin': VIN},
    {'action': 'wake', 'entry_id': 'one', 'vin': VIN},
    {'action': 'write', 'entry_id': 'one', 'vin': VIN, 'settings': {**SETTINGS, 'lowerChargeLimit': 81}},
])
async def test_invalid_requests_never_reach_a_service(view, body):
    api, _, _, calls = view
    status, result = await api.post(request(body))
    assert status == 400 and not result['success'] and calls == []
