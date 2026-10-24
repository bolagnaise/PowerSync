"""Native solar protocol, uncertain readback, and serialized ownership checks."""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location('native_solar', Path(__file__).parents[1] / 'custom_components/power_sync/teslemetry_solar.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
VIN = '5YJ3E1EA7KF000001'
SETTINGS = dict(enabled=True, lowerChargeLimit=50, upperChargeLimit=80)

@pytest.mark.parametrize('value', [{}, {**SETTINGS, 'enabled': 1}, {**SETTINGS, 'lowerChargeLimit': True}, {**SETTINGS, 'lowerChargeLimit': 81}, {**SETTINGS, 'upperChargeLimit': 101}])
def test_validation(value):
    with pytest.raises(m.SolarError): m.validate_settings(value)

class Response:
    def __init__(self, body, status=200): self.body, self.status = body, status
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def json(self): return self.body

class Session:
    def __init__(self, response): self.response, self.calls = response, []
    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response

@pytest.mark.asyncio
async def test_explicit_get_and_ack_are_distinct():
    s = Session(Response({'response': SETTINGS}))
    assert await m.request_settings(s, 'secret', VIN) == SETTINGS
    assert s.calls[0][0][0] == 'GET'
    s = Session(Response({'response': {'result': True}}))
    assert await m.request_settings(s, 'secret', VIN, SETTINGS) is None
    with pytest.raises(m.SolarError): await m.request_settings(s, 'secret', VIN)
    s = Session(Response({'response': {**SETTINGS, 'result': False}}))
    with pytest.raises(m.SolarError): await m.request_settings(s, 'secret', VIN)

class Store:
    def __init__(self): self.value = None
    async def async_load(self): return self.value
    async def async_save(self, value): self.value = value

@pytest.mark.asyncio
async def test_restart_and_scoped_hold():
    store = Store(); a = m.SolarOwnership(store); await a.load()
    await a.hold(VIN, {'status': 'unconfirmed'})
    b = m.SolarOwnership(store); await b.load()
    assert b.blocked(VIN) and b.blocked(None)
    assert not b.blocked('5YJ3E1EA7KF000002')
    await b.release(VIN)
    assert not b.blocked(VIN)

@pytest.mark.asyncio
async def test_ownership_waits_for_inflight_action_then_blocks(monkeypatch):
    gate = m.SolarOwnership(Store()); await gate.load()
    async def ownership(*args): return gate
    monkeypatch.setattr(m, 'ownership', ownership)
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = []
    @m.guard_ev_control
    async def action(hass, config_entry, params):
        calls.append(params)
        entered.set(); await finish.wait(); return True
    entry = SimpleNamespace(entry_id='one')
    task = asyncio.create_task(action(None, entry, {'vehicle_vin': VIN}))
    await entered.wait()
    async def handover():
        async with gate.lock: await gate.hold(VIN, {'status': 'unconfirmed'})
    transfer = asyncio.create_task(handover()); await asyncio.sleep(0)
    assert not transfer.done()
    finish.set(); assert await task; await transfer
    assert not await action(None, entry, {'vehicle_vin': VIN})
    assert len(calls) == 1
    assert await action(None, entry, {'vehicle_vin': '5YJ3E1EA7KF000002'})

@pytest.mark.asyncio
async def test_service_provider_monitoring_handover_and_readback(monkeypatch):
    import sys
    import types
    # Registration schemas are HA infrastructure; domain validation is tested above.
    vol = types.ModuleType('voluptuous')
    vol.Required = vol.Optional = lambda key, **kwargs: key
    vol.Schema = lambda value: value
    vol.All = lambda *args: args
    vol.Range = lambda **kwargs: kwargs
    monkeypatch.setitem(sys.modules, 'voluptuous', vol)
    root = types.ModuleType('native_solar_service_test')
    root.__path__ = []
    provider = ['teslemetry']
    root.get_tesla_vehicle_api_token = lambda *args: ('secret', provider[0])
    root.get_tesla_api_token = lambda *args: (None, 'none')
    monkeypatch.setitem(sys.modules, root.__name__, root)
    monkeypatch.setattr(m, '__package__', root.__name__)
    const = types.ModuleType(root.__name__ + '.const')
    const.DOMAIN, const.CONF_MONITORING_MODE = 'power_sync', 'monitoring_mode'
    monkeypatch.setitem(sys.modules, const.__name__, const)
    actions = types.ModuleType(root.__name__ + '.automations.actions')
    actions._dynamic_ev_state = {}
    monkeypatch.setitem(sys.modules, actions.__name__, actions)
    ha_core = types.ModuleType('homeassistant.core')
    ha_core.SupportsResponse = SimpleNamespace(ONLY='only', OPTIONAL='optional')
    monkeypatch.setitem(sys.modules, ha_core.__name__, ha_core)
    ha_ex = types.ModuleType('homeassistant.exceptions')
    ha_ex.HomeAssistantError = m.SolarError
    monkeypatch.setitem(sys.modules, ha_ex.__name__, ha_ex)
    ha_client = types.ModuleType('homeassistant.helpers.aiohttp_client')
    ha_client.async_get_clientsession = lambda hass: None
    monkeypatch.setitem(sys.modules, ha_client.__name__, ha_client)
    gate = m.SolarOwnership(Store()); await gate.load()
    async def ownership(*args): return gate
    monkeypatch.setattr(m, 'ownership', ownership)
    responses, calls = [], []
    async def request(*args):
        calls.append(args)
        response = responses.pop(0)
        if isinstance(response, Exception): raise response
        return response
    monkeypatch.setattr(m, 'request_settings', request)
    services = {}
    def register(domain, name, handler, **kwargs): services[name] = handler
    entry = SimpleNamespace(entry_id='one', data={}, options={})
    hass = SimpleNamespace(data={'power_sync': {'one': {}}},
        config_entries=SimpleNamespace(async_entries=lambda _: [entry]),
        services=SimpleNamespace(async_register=register))
    m.register_services(hass)
    async def call(service, **extra):
        return await services[service](SimpleNamespace(service=service, data={
            'entry_id':'one', 'vin':VIN, 'enabled':True, 'confirm_eligible':True,
            'lower_charge_limit':50, 'upper_charge_limit':80, **extra}))
    get, put = 'get_teslemetry_charge_on_solar', 'set_teslemetry_charge_on_solar'
    provider[0] = 'tesla_fleet'
    with pytest.raises(m.SolarError): await call(get)
    provider[0] = 'teslemetry'; entry.options['monitoring_mode'] = True
    with pytest.raises(m.SolarError): await call(put)
    entry.options.clear()
    with pytest.raises(m.SolarError): await call(put, confirm_eligible=False)
    actions._dynamic_ev_state['one'] = {'v':{'active':True}}
    with pytest.raises(m.SolarError): await call(get)
    assert not calls
    actions._dynamic_ev_state.clear()
    # A rate lease in another PowerSync entry uses the shared physical VIN.
    hass.data['power_sync']['two'] = {'tesla_native_rate':SimpleNamespace(sessions={VIN:{}})}
    hass.data['power_sync_native_rate_budget'] = SimpleNamespace(native_claimed=lambda vin: vin == VIN)
    with pytest.raises(m.SolarError, match='native-window rate management'):
        await call(put)
    assert not calls
    hass.data.pop('power_sync_native_rate_budget')
    hass.data['power_sync'].pop('two')
    responses.extend([None, m.SolarError('readback failed')])
    with pytest.raises(m.SolarError): await call(put)
    assert gate.blocked(VIN)
    gate.requests.clear()
    responses.append({**SETTINGS, 'enabled': False})
    result = await call(get)
    assert result['readback_confirmed'] and not result['powersync_control_blocked']
    assert not gate.blocked(VIN)
    # The independently implemented zero-solar setting blocks new activation,
    # without discarding an uncertain hold or preventing explicit disabling.
    await gate.hold(VIN, {'status': 'unconfirmed'})
    entry.options['no_solar_installed'] = True
    previous_calls = len(calls)
    with pytest.raises(m.SolarError, match='No solar installed'):
        await call(put)
    assert len(calls) == previous_calls and gate.blocked(VIN)
    gate.requests.clear()
    responses.extend([None, {**SETTINGS, 'enabled': False}])
    result = await call(put, enabled=False)
    assert result['readback_confirmed'] and not gate.blocked(VIN)
    # Fleet's Cloud transaction already includes readback: exactly one call.
    entry.options.clear()
    provider[0] = 'none'
    root.get_tesla_api_token = lambda *args: ('powersync-secret', 'powersync')
    fleet_calls = []
    async def fleet_request(*args, **kwargs):
        fleet_calls.append((args, kwargs))
        return SETTINGS
    monkeypatch.setattr(m, 'request_settings', fleet_request)
    gate.requests.clear()
    result = await call(put)
    assert result['readback_confirmed'] and gate.blocked(VIN)
    assert len(fleet_calls) == 1
    assert fleet_calls[0][0][1] == 'powersync-secret'
    assert fleet_calls[0][1] == {'provider': 'powersync'}
    entry.options['monitoring_mode'] = True
    with pytest.raises(m.SolarError): await call(put, enabled=False)
    assert len(fleet_calls) == 1 and gate.blocked(VIN)


@pytest.mark.asyncio
async def test_powersync_fleet_uses_one_serialized_cloud_write_and_readback():
    s = Session(Response({'settings': SETTINGS, 'readback_confirmed': True, 'powersync_control_blocked': True}))
    assert await m.request_settings(s, 'powersync-secret', VIN, SETTINGS, provider='powersync') == SETTINGS
    assert len(s.calls) == 1
    args, options = s.calls[0]
    assert args == ('POST', f'https://api.powersync.cc/v1/vehicles/{VIN}/native-solar')
    assert options['json'] == {'action': 'set', 'provider': 'fleet', 'settings': SETTINGS, 'confirm_eligible': True}
    assert options['headers'] == {'Authorization': 'Bearer powersync-secret'}
    assert options['allow_redirects'] is False


@pytest.mark.asyncio
async def test_powersync_read_is_explicit_and_acknowledgement_is_not_readback():
    s = Session(Response({'settings': SETTINGS}))
    assert await m.request_settings(s, 'secret', VIN, provider='powersync') == SETTINGS
    assert s.calls[0][1]['json'] == {'action': 'refresh', 'provider': 'fleet'}
    for body in ({'response': {'result': True}}, {'settings': {'enabled': False}}, None):
        with pytest.raises(m.SolarError):
            await m.request_settings(Session(Response(body)), 'secret', VIN, SETTINGS, provider='powersync')
    with pytest.raises(m.SolarError, match='HTTP 404'):
        await m.request_settings(Session(Response({}, 404)), 'secret', VIN, provider='powersync')


def test_native_connection_uses_provider_specific_credentials(monkeypatch):
    import sys
    import types
    package = types.ModuleType('native_connection_probe')
    package.get_tesla_vehicle_api_token = lambda *args: ('teslemetry-secret', 'teslemetry')
    package.get_tesla_api_token = lambda *args: ('powersync-secret', 'powersync')
    monkeypatch.setitem(sys.modules, package.__name__, package)
    monkeypatch.setattr(m, '__package__', package.__name__)
    assert m.native_solar_connection(None, None) == ('teslemetry-secret', 'teslemetry')
    package.get_tesla_vehicle_api_token = lambda *args: ('ha-fleet-oauth', 'tesla_fleet')
    assert m.native_solar_connection(None, None) == ('powersync-secret', 'powersync')
    package.get_tesla_api_token = lambda *args: ('other-token', 'fleet_api')
    assert m.native_solar_connection(None, None) == (None, 'none')
