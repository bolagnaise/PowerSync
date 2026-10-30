"""Native config omits its false grid-charge disallow value on every firmware."""
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

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
