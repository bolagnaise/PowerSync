"""The attested 26.34.2 native config omits false grid-charge permission."""
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


def system_info():
    return {'din': 'gateway', 'firmware_version': '26.34.2 56d7ba06',
            'firmware_githash': '56d7ba064772ad6d740825723c7e9c8524cd31b1'}


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
        get_system_info=AsyncMock(return_value=system_info() if firmware is None else firmware))
    return client


@pytest.mark.asyncio
async def test_fresh_native_snapshot_recognizes_attested_omitted_false():
    client = bare_client()
    snapshot = await client.get_snapshot()
    assert snapshot.grid_charging_enabled is True
    assert snapshot.grid_charging_permission_source == 'attested_omitted'
    assert 'disallow_charge_from_grid_with_solar_installed' not in snapshot.raw['config']['site_info']
    client._transport.get_system_info.assert_awaited_once_with('gateway')


@pytest.mark.asyncio
@pytest.mark.parametrize('field', [False, True, None, 'false', 0, [], {}])
async def test_explicit_permission_never_uses_omission_exception(field):
    config = native_config()
    config['site_info']['disallow_charge_from_grid_with_solar_installed'] = field
    snapshot = await bare_client(config).get_snapshot()
    assert snapshot.grid_charging_enabled is (not field if type(field) is bool else None)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['version', 'hash', 'din', 'missing', 'failure'])
async def test_unattested_omission_stays_unknown(change):
    firmware = system_info()
    if change == 'version': firmware['firmware_version'] = '26.34.3 56d7ba06'
    if change == 'hash': firmware['firmware_githash'] = '0'*40
    if change == 'din': firmware['din'] = 'other-gateway'
    if change == 'missing': firmware = {}
    client = bare_client(firmware=firmware)
    if change == 'failure': client._transport.get_system_info.side_effect = TimeoutError
    snapshot = await client.get_snapshot()
    assert snapshot.grid_charging_enabled is None


@pytest.mark.parametrize('missing', ['default_real_mode','battery_blocks','meters','strategy',
    'site_info', 'backup_reserve_percent','max_site_meter_power_ac','min_site_meter_power_ac',
    'customer_preferred_export_rule'])
def test_partial_native_config_does_not_inherit_a_false_default(missing):
    config = native_config()
    (config if missing in config else config['site_info']).pop(missing)
    reading = normalization.native_grid_charging_reading(config, system_info(), 'gateway')
    assert reading.disallowed is None and reading.source == 'unknown'


@pytest.mark.parametrize('key,value', [('backup_reserve_percent',None),('backup_reserve_percent',101),
    ('max_site_meter_power_ac',float('nan')),('max_site_meter_power_ac',True),
    ('min_site_meter_power_ac',1),('customer_preferred_export_rule','unknown')])
def test_malformed_native_envelope_does_not_inherit_a_false_default(key,value):
    config = native_config()
    config['site_info'][key] = value
    assert normalization.native_grid_charging_reading(config,system_info(),'gateway').disallowed is None


@pytest.mark.asyncio
async def test_attestation_expires_and_is_not_reused_after_failed_refresh(monkeypatch):
    clock = [100.]
    monkeypatch.setattr(client_mod.time,'monotonic',lambda:clock[0])
    client = bare_client()
    await client.refresh_native_grid_charging_firmware()
    assert client.native_grid_charging_reading(native_config()).disallowed is False
    clock[0] = 106
    assert client.native_grid_charging_reading(native_config()).disallowed is None
    clock[0] = 100
    client._din = 'other-gateway'
    assert client.native_grid_charging_reading(native_config()).disallowed is None
    client._din = 'gateway'
    client._transport.get_system_info.side_effect = TimeoutError
    await client.refresh_native_grid_charging_firmware()
    assert client.native_grid_charging_reading(native_config()).disallowed is None


@pytest.mark.asyncio
async def test_new_firmware_read_invalidates_previous_build_attestation():
    client = bare_client()
    assert (await client.get_snapshot()).grid_charging_enabled is True
    client._transport.get_system_info.return_value = {**system_info(),'firmware_githash':'f'*40}
    assert (await client.get_snapshot()).grid_charging_enabled is None
