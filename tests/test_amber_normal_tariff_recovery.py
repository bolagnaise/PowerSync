"""Recovery builds the same current Amber schedule without ordinary sync writes."""
import ast
import copy
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
import sys

import pytest
from conftest import load_source_node

ROOT = Path(__file__).parents[1] / 'custom_components' / 'power_sync'


@pytest.fixture
def builder(monkeypatch):
    clock = SimpleNamespace(now=datetime(2026, 11, 6, 1, 1, tzinfo=timezone.utc))
    dt = SimpleNamespace(utcnow=lambda: clock.now, now=lambda: clock.now,
        parse_datetime=lambda value: datetime.fromisoformat(value.replace('Z', '+00:00')))
    ns = dict(__package__='amber_recovery_builder', copy=copy, datetime=datetime, Any=Any,
        dt_util=dt, _LOGGER=logging.getLogger(__name__), DOMAIN='power_sync')
    node = load_source_node(ROOT / '__init__.py', '_handle_sync_tou_internal')
    constants = ast.parse((ROOT / 'const.py').read_text())
    for item in constants.body:
        if isinstance(item, ast.Assign):
            try: value = ast.literal_eval(item.value)
            except (ValueError, TypeError): continue
            for target in item.targets:
                if isinstance(target, ast.Name): ns[target.id] = value
    runtime = {}
    entry = SimpleNamespace(entry_id='entry', data={'battery_system': 'tesla',
        'electricity_provider': 'amber', 'tesla_energy_site_id': 'site'}, options={})
    current = [{'type': 'CurrentInterval', 'channelType': channel, 'duration': 5,
                'nemTime': '2026-11-06T01:05:00+00:00', 'perKwh': price}
               for channel, price in [('general', 42), ('feedIn', 8)]]
    coord = SimpleNamespace(data={'last_update': clock.now, 'current': current,
        'forecast': [{'channelType': 'general', 'perKwh': 42}]}, last_update_success=True,
        async_refresh=AsyncMock(), async_request_refresh=AsyncMock())
    converter = ModuleType('amber_recovery_builder.tariff_converter')
    helper = load_source_node(ROOT / 'tariff_converter.py', 'extract_current_price_interval')
    helper_ns = dict(datetime=datetime, timedelta=timedelta, dt_util=dt, Any=Any,
                     _LOGGER=logging.getLogger(__name__))
    exec(compile(ast.Module(body=[helper], type_ignores=[]), '<current-price>', 'exec'), helper_ns)
    converter.extract_current_price_interval = helper_ns['extract_current_price_interval']
    calls = []
    def convert(forecast, **kwargs):
        calls.append((copy.deepcopy(forecast), kwargs))
        return {'code': 'POWER_SYNC:AMBER', 'name': 'fresh Amber',
                'energy_charges': {'Summer': {'rates': {'NOW': .42}}}}
    converter.convert_amber_to_tesla_tariff = convert
    def unexpected(*args, **kwargs): raise AssertionError('Unexpected ordinary sync side effect')
    for name in ('convert_custom_tariff_to_tesla_tariff', 'compare_forecast_types',
                 'detect_price_spikes', 'format_spike_alert_time', 'spike_alert_key_time',
                 'spike_is_imminent'):
        setattr(converter, name, unexpected)
    converter.apply_export_boost = lambda tariff, *args: dict(tariff, boosted=True)
    converter.apply_chip_mode = lambda tariff, *args, **kwargs: dict(tariff, chip=True)
    pkg = ModuleType('amber_recovery_builder'); pkg.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, pkg.__name__, pkg)
    monkeypatch.setitem(sys.modules, converter.__name__, converter)
    ns.update(entry=entry, hass=SimpleNamespace(data={'power_sync': {'entry': runtime}}),
        _command_generation=[0], _aemo_dispatch_entry_data=lambda: runtime,
        _is_monitoring_mode=lambda: False, force_charge_state={'active': True},
        force_discharge_state={'active': False}, amber_coordinator=coord,
        aemo_sensor_coordinator=None, flow_power_kwatch_coordinator=None,
        localvolts_coordinator=None, octopus_coordinator=None, tesla_coordinator=None,
        _static_tou_tariff_schedule_for_sync=lambda: None,
        _static_tou_custom_tariff_for_sync=lambda: None,
        _record_flow_power_twap_sample=lambda *args: None,
        currency_for_entry=lambda *args: 'AUD', send_tariff_to_tesla=unexpected,
        async_dispatcher_send=unexpected)
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<amber-sync>', 'exec'), ns)
    return SimpleNamespace(ns=ns, entry=entry, coord=coord, calls=calls, clock=clock,
                           run=ns['_handle_sync_tou_internal'])


@pytest.mark.asyncio
async def test_recovery_uses_fresh_current_prices_and_existing_user_adjustments(builder):
    b = builder
    b.entry.options.update(export_boost_enabled=True, chip_mode_enabled=True,
        demand_charge_enabled=True, demand_charge_rate=12, spike_protection_enabled=True,
        amber_forecast_type='high')
    tariff, current = await b.run(None, build_only=True)
    assert tariff['boosted'] and tariff['chip'] and current()
    b.coord.async_refresh.assert_awaited_once()
    b.coord.async_request_refresh.assert_not_awaited()
    _, kwargs = b.calls[0]
    assert kwargs['current_actual_interval']['general']['perKwh'] == 42
    assert kwargs['demand_charge_enabled'] and kwargs['spike_protection_enabled']
    assert kwargs['forecast_type'] == 'high' and kwargs['currency'] == 'AUD'
    assert 'tariff_schedule' not in b.ns['_aemo_dispatch_entry_data']()
    b.ns['_command_generation'][0] += 1
    assert not current()


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['refresh_failed', 'stale', 'missing_time',
                                 'old_interval', 'wrong_provider', 'wrong_battery'])
async def test_recovery_never_uploads_cached_or_wrong_provider_schedule(builder, bad):
    b = builder
    if bad == 'refresh_failed': b.coord.last_update_success = False
    elif bad == 'stale': b.coord.data['last_update'] -= timedelta(minutes=10)
    elif bad == 'missing_time': b.coord.data.pop('last_update')
    elif bad == 'old_interval':
        for interval in b.coord.data['current']: interval['nemTime'] = '2026-11-05T01:00:00+00:00'
    elif bad == 'wrong_provider': b.entry.options['electricity_provider'] = 'flow_power'
    elif bad == 'wrong_battery': b.entry.data['battery_system'] = 'sungrow'
    assert await b.run(None, build_only=True) is None
    assert b.calls == []


@pytest.mark.asyncio
async def test_built_schedule_expires_at_current_interval_boundary(builder):
    tariff, current = await builder.run(None, build_only=True)
    assert tariff and current()
    builder.clock.now = datetime(2026, 11, 6, 1, 5, tzinfo=timezone.utc)
    assert not current()
