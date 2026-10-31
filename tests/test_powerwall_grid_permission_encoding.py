"""Native config omits its false grid-charge disallow value on every firmware."""
import asyncio
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from conftest import load_power_sync_function

ROOT = Path(__file__).resolve().parents[1] / 'custom_components/power_sync'
PKG = 'native_grid_encoding_probe'
for name, path in ((PKG, ROOT), (PKG+'.powerwall_local', ROOT/'powerwall_local')):
    package = ModuleType(name)
    package.__path__ = [str(path)]
    sys.modules[name] = package
client_mod = importlib.import_module(PKG+'.powerwall_local.client')
normalization = importlib.import_module(PKG+'.powerwall_local.normalization')


def native_config():
    return {'default_real_mode': 'autonomous', 'battery_blocks': [{}], 'meters': [{}],
            'strategy': {}, 'site_info': {'backup_reserve_percent': 100,
                'max_site_meter_power_ac': 14.9, 'min_site_meter_power_ac': -10.1,
                'customer_preferred_export_rule': 'battery_ok'}}


def dcq():
    return {'control': {'meterAggregates': [
        {'location': name, 'realPowerW': value} for name, value in
        [('site', 1000), ('battery', 0), ('solar', 0), ('load', 1000)]],
        'systemStatus': {'nominalFullPackEnergyWh': 27000, 'nominalEnergyRemainingWh': 14000},
        'islanding': {'contactorClosed': True, 'gridOK': True, 'microGridOK': True}}}


def bare_client(config=None, firmware=None):
    client = object.__new__(client_mod.PowerwallLocalClient)
    client._din = 'gateway'
    client._host = '192.0.2.10'
    client._local_access_enabled = True
    client._fetch_dcq_local = AsyncMock(return_value=dcq())
    client._transport = SimpleNamespace(read_config=AsyncMock(return_value=native_config() if config is None else config),
        get_system_info=AsyncMock(return_value={} if firmware is None else firmware))
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize('firmware',[{}, {'firmware_version':'26.34.0'}, {'firmware_version':'26.34.2 56d7ba06'}, {'firmware_version':'future'}])
async def test_native_false_default_does_not_depend_on_firmware(firmware):
    client = bare_client(firmware=firmware)
    client._transport.get_system_info.side_effect = AssertionError('permission parsing must not query firmware')
    snapshot = await client.get_snapshot()
    assert snapshot.grid_charging_enabled is True
    assert snapshot.grid_charging_permission_source == 'native_default'
    assert 'disallow_charge_from_grid_with_solar_installed' not in snapshot.raw['config']['site_info']
    client._transport.get_system_info.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('field',[False,True,None,'false',0,[],{}])
async def test_explicit_permission_is_strictly_boolean(field):
    config = native_config()
    config['site_info']['disallow_charge_from_grid_with_solar_installed'] = field
    snapshot = await bare_client(config).get_snapshot()
    assert snapshot.grid_charging_enabled is (not field if type(field) is bool else None)


@pytest.mark.parametrize('config',[{'site_info':{}},native_config()])
def test_native_false_default_needs_no_unrelated_mode_meter_or_reserve_fields(config):
    reading = normalization.native_grid_charging_reading(config)
    assert reading.disallowed is False and reading.source == 'native_default'


@pytest.mark.parametrize('config',[None,{}, {'site_info':None},{'site_info':[]}, {'components':{}}])
def test_unavailable_native_configuration_is_unknown(config):
    reading = normalization.native_grid_charging_reading(config)
    assert reading.disallowed is None and reading.source == 'unknown'


@pytest.mark.asyncio
async def test_failed_native_read_does_not_become_permission():
    client = bare_client()
    client._transport.read_config.side_effect = TimeoutError
    snapshot = await client.get_snapshot()
    assert snapshot.grid_charging_enabled is None
    client._transport.get_system_info.assert_not_awaited()


@pytest.mark.asyncio
async def test_grid_toggle_false_default_true_false_default_is_not_cached():
    client = bare_client()
    field = 'disallow_charge_from_grid_with_solar_installed'
    assert (await client.get_snapshot()).grid_charging_enabled is True
    client._transport.read_config.return_value['site_info'][field] = True
    assert (await client.get_snapshot()).grid_charging_enabled is False
    client._transport.read_config.return_value['site_info'].pop(field)
    assert (await client.get_snapshot()).grid_charging_enabled is True
    client._transport.read_config.side_effect = TimeoutError
    assert (await client.get_snapshot()).grid_charging_enabled is None
    client._transport.get_system_info.assert_not_awaited()


def confirmation_writer():
    """Run the production confirmation loop with actual native parsing."""
    clock = SimpleNamespace(now=100.0)

    async def sleep(delay):
        clock.now += delay

    namespace = {
        'asyncio': SimpleNamespace(sleep=sleep, CancelledError=asyncio.CancelledError,
                                  TimeoutError=asyncio.TimeoutError),
        'time': SimpleNamespace(monotonic=lambda: clock.now),
        'native_grid_charging_reading': normalization.native_grid_charging_reading,
    }
    path = ROOT / 'powerwall_local/dispatch.py'
    denied = load_power_sync_function('_SettingsPolicyDenied', namespace, path=path)
    writer = load_power_sync_function('write_config_confirmed', namespace, path=path)
    return writer, denied


@pytest.mark.asyncio
@pytest.mark.parametrize('config', [{'site_info': {}}, native_config()])
async def test_native_enable_confirmation_accepts_fresh_omitted_false_default(config):
    writer, _ = confirmation_writer()
    transport = SimpleNamespace(write_config=AsyncMock(return_value=True),
                                read_config=AsyncMock(return_value=config))
    updates = {'site_info.disallow_charge_from_grid_with_solar_installed': False}
    assert await writer(transport, 'gateway', updates,
                        is_current=lambda: True, deadline=105) is True
    transport.write_config.assert_awaited_once()
    transport.read_config.assert_awaited_once_with('gateway')


@pytest.mark.asyncio
@pytest.mark.parametrize('expected', [False, True])
@pytest.mark.parametrize('observed', [False, True])
async def test_native_confirmation_requires_exact_explicit_permission(expected, observed):
    writer, _ = confirmation_writer()
    field = 'disallow_charge_from_grid_with_solar_installed'
    transport = SimpleNamespace(write_config=AsyncMock(return_value=True),
                                read_config=AsyncMock(return_value={'site_info': {field: observed}}))
    assert await writer(transport, 'gateway', {'site_info.' + field: expected},
                        is_current=lambda: True, deadline=105) is (expected is observed)


@pytest.mark.asyncio
async def test_native_omitted_false_cannot_confirm_disabling_grid_charging():
    writer, _ = confirmation_writer()
    transport = SimpleNamespace(write_config=AsyncMock(return_value=True),
                                read_config=AsyncMock(return_value=native_config()))
    assert await writer(transport, 'gateway', {
        'site_info.disallow_charge_from_grid_with_solar_installed': True},
        is_current=lambda: True, deadline=105) is False
    assert transport.read_config.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('config', [None, {}, {'site_info': None}, {'site_info': []},
    {'site_info': 'unknown'}, {'response': {'site_info': {}}},
    *[{'site_info': {'disallow_charge_from_grid_with_solar_installed': value}}
      for value in (None, 0, 'false', [], {})]])
async def test_native_confirmation_keeps_unavailable_or_malformed_permission_unknown(config):
    writer, _ = confirmation_writer()
    transport = SimpleNamespace(write_config=AsyncMock(return_value=True),
                                read_config=AsyncMock(return_value=config))
    assert await writer(transport, 'gateway', {
        'site_info.disallow_charge_from_grid_with_solar_installed': False},
        is_current=lambda: True, deadline=105) is False
    assert transport.read_config.await_count == 3


@pytest.mark.asyncio
async def test_unknown_native_readback_cannot_confirm_an_unknown_update():
    writer, _ = confirmation_writer()
    transport = SimpleNamespace(write_config=AsyncMock(return_value=True),
                                read_config=AsyncMock(return_value={'site_info': None}))
    assert await writer(transport, 'gateway', {
        'site_info.disallow_charge_from_grid_with_solar_installed': None},
        is_current=lambda: True, deadline=105) is False


@pytest.mark.asyncio
async def test_native_enable_confirmation_retries_fresh_config_after_opposite_readback():
    writer, _ = confirmation_writer()
    field = 'disallow_charge_from_grid_with_solar_installed'
    transport = SimpleNamespace(write_config=AsyncMock(return_value=True),
        read_config=AsyncMock(side_effect=[{'site_info': {field: True}}, native_config(), native_config()]),
        get_snapshot=AsyncMock(side_effect=AssertionError('confirmation must read raw native config')))
    assert await writer(transport, 'gateway', {'site_info.' + field: False},
                        is_current=lambda: True, deadline=105) is True
    assert transport.read_config.await_count == 2
    transport.write_config.assert_awaited_once()
    transport.get_snapshot.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['site_info.unrelated_flag', 'unrelated_flag'])
async def test_native_false_default_is_not_applied_to_other_fields(field):
    writer, _ = confirmation_writer()
    transport = SimpleNamespace(write_config=AsyncMock(return_value=True),
                                read_config=AsyncMock(return_value={'site_info': {}}))
    assert await writer(transport, 'gateway', {field: False},
                        is_current=lambda: True, deadline=105) is False


@pytest.mark.asyncio
async def test_native_default_does_not_confirm_another_mismatching_setting():
    writer, _ = confirmation_writer()
    transport = SimpleNamespace(write_config=AsyncMock(return_value=True),
                                read_config=AsyncMock(return_value={'site_info': {}}))
    assert await writer(transport, 'gateway', {
        'site_info.disallow_charge_from_grid_with_solar_installed': False,
        'default_real_mode': 'autonomous'},
        is_current=lambda: True, deadline=105) is False


@pytest.mark.asyncio
@pytest.mark.parametrize('revoked', ['generation', 'permission'])
async def test_native_default_readback_cannot_bypass_current_owner_or_permission(revoked):
    writer, denied = confirmation_writer()
    allowed = SimpleNamespace(current=True, permission=True)

    async def read(_din):
        if revoked == 'generation':
            allowed.current = False
        else:
            allowed.permission = False
        return native_config()

    transport = SimpleNamespace(write_config=AsyncMock(return_value=True), read_config=read)
    error = asyncio.CancelledError if revoked == 'generation' else denied
    with pytest.raises(error):
        await writer(transport, 'gateway', {
            'site_info.disallow_charge_from_grid_with_solar_installed': False},
            is_current=lambda: allowed.current, is_allowed=lambda: allowed.permission, deadline=105)


@pytest.mark.asyncio
async def test_native_rejected_update_cannot_confirm_from_default_readback():
    writer, _ = confirmation_writer()
    transport = SimpleNamespace(write_config=AsyncMock(return_value=False),
                                read_config=AsyncMock(return_value=native_config()))
    assert await writer(transport, 'gateway', {
        'site_info.disallow_charge_from_grid_with_solar_installed': False},
        is_current=lambda: True, deadline=105) is False
    transport.read_config.assert_not_awaited()


def test_cloud_omitted_permission_keeps_unknown_without_native_default():
    namespace = {}
    path = ROOT / 'tesla_grid_control.py'
    load_power_sync_function('_optional_bool', namespace, path=path)
    parse = load_power_sync_function('tesla_grid_charging_enabled_from_site_info', namespace, path=path)
    assert parse(native_config()['site_info']) is None
    assert parse({'components': {}}) is None
