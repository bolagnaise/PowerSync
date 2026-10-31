"""Scheduled native refusals must reach the user without alert spam."""
import ast
import asyncio
import importlib
import logging
from pathlib import Path
import sys
from datetime import datetime, timedelta, timezone
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

ROOT = Path(__file__).parents[1] / 'custom_components/power_sync'


@pytest.mark.asyncio
@pytest.mark.parametrize('source,failure', [('user', None), *[
    ('optimizer', failure) for failure in (
        None, 'mode', 'grid', 'reserve', 'tariff', 'unconfirmed_tariff', 'pulse',
        'command_replaced', 'reserve_replaced')]])
async def test_cloud_tariff_recovery_clears_old_local_refusal_only_after_confirmation(
        notification_probe, source, failure):
    p = notification_probe
    p.charge('grid_charging_permission_unknown')
    state = {'active': True, 'saved_operation_mode': 'self_consumption',
             'cancel_expiry_timer': None}
    now = datetime(2026, 10, 30, tzinfo=timezone.utc)
    mode = AsyncMock(return_value=failure != 'mode')
    grid = AsyncMock(return_value='denied' if failure == 'grid' else 'field_absent')
    reserve = AsyncMock(return_value=failure != 'reserve')
    pulse = AsyncMock(return_value=failure != 'pulse')

    async def upload(*args, **kwargs):
        # The compatibility warning is emitted before this boundary. It must
        # not dismiss the earlier refusal when later prerequisites can fail.
        assert p.persistent.async_dismiss.call_count == 0
        if failure != 'tariff':
            kwargs['accepted_status']['accepted'] = True
        return failure not in ('tariff', 'unconfirmed_tariff')

    module_ast = ast.parse((ROOT / '__init__.py').read_text())
    handler = next(n for n in ast.walk(module_ast) if isinstance(n, ast.AsyncFunctionDef)
                   and n.name == 'handle_force_charge')
    start = next(i for i, n in enumerate(handler.body) if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == 'tesla_force_charge_mutated'
                         for t in n.targets))
    # Exercise the actual Tesla preparation/upload/final-pulse result flow,
    # using an existing saved baseline to avoid unrelated HTTP capture fakes.
    wrapper = ast.AsyncFunctionDef(name='run_tariff', args=ast.arguments(
        posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=handler.body[start:], decorator_list=[])
    call = SimpleNamespace(data={}, context=None)

    async def persist():
        if state.get('command_status') == 'accepted':
            if failure == 'command_replaced':
                scope['_command_generation'][0] += 1
            elif failure == 'reserve_replaced':
                scope['_tesla_reserve_generation'][0] += 1

    scope = dict(__package__=p.package, _LOGGER=logging.getLogger(__name__), asyncio=asyncio,
        datetime=datetime, timedelta=timedelta, dt_util=SimpleNamespace(UTC=timezone.utc, utcnow=lambda: now),
        hass=p.hass, entry=SimpleNamespace(entry_id='one', data={}), source=source,
        call=call, duration=15, tesla_requested_expiry=now + timedelta(minutes=15),
        force_charge_state=state, force_discharge_state={'active': False},
        was_already_force_charging=True, _command_generation=[7], _restore_gen=7,
        _tesla_reserve_generation=[2], _reserve_gen=2,
        _get_tesla_site_configs=lambda *_: [('site', 'token', 'provider')],
        async_get_clientsession=lambda *_: object(),
        _tesla_force_apply_operation_mode=mode, _tesla_force_apply_grid_charging=grid,
        _tesla_force_apply_backup_reserve=reserve, _tesla_force_pulse_backup_reserve=pulse,
        _tesla_force_result_all_confirmed=lambda result, _: result is True,
        _tesla_force_result_all_grid_field_absent_safe=lambda result, _: result == 'field_absent',
        _monitoring_mode_should_block_control=lambda _: False,
        _demand_grid_charging_protection_active=lambda: False,
        persist_force_mode_state=persist, handle_restore_normal=AsyncMock(),
        _create_charge_tariff=lambda _: ({}, now + timedelta(minutes=30)),
        send_tariff_to_tesla=upload, _tesla_force_retry_expiry=lambda _, expiry, **kw: expiry,
        _schedule_tesla_charge_kick=Mock(), async_dispatcher_send=Mock(),
        async_track_point_in_utc_time=Mock(), _notify_api_error=AsyncMock(),
        ServiceCall=lambda *args, **kw: call, HomeAssistantError=RuntimeError,
        DOMAIN='power_sync', SERVICE_RESTORE_NORMAL='restore_normal',
        CONF_FLEET_API_BASE_URL='fleet_api_base_url')
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])),
                 str(ROOT / '__init__.py'), 'exec'), scope)
    result = await scope['run_tariff']()
    if failure is None:
        assert result['success'] is True
        p.persistent.async_dismiss.assert_called_once_with(
            p.hass, 'power_sync_one_native_charge_blocked')
        await p.queued[0]
        p.push.assert_not_awaited()
    else:
        if failure not in ('unconfirmed_tariff', 'command_replaced', 'reserve_replaced'):
            assert result['success'] is False
        p.persistent.async_dismiss.assert_not_called()
        assert 'one' in p.hass.data['power_sync']['_native_charge_alerts']


@pytest.mark.asyncio
@pytest.mark.parametrize('direction,source,allowed,reason,expected', [
    ('discharge', 'optimizer', False, 'native_export_rule_invalid', True),
    ('charge', 'optimizer', False, 'grid_charging_not_permitted', True),
    ('charge', 'optimizer', False, 'software_import_limit_requires_local_control', True),
    ('discharge', 'user', False, 'native_export_rule_invalid', False),
    ('charge', 'user', False, 'grid_charging_not_permitted', False),
    ('discharge', 'optimizer', True, None, True),
    ('discharge', 'user', True, None, True),
    ('charge', 'optimizer', True, None, True),
    ('charge', 'user', True, None, True),
])
async def test_native_route_notifies_scheduled_admission_failure(
        monkeypatch, direction, source, allowed, reason, expected):
    package = 'native_alert_route_probe'
    parent = ModuleType(package)
    parent.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, package, parent)
    host = ModuleType(package + '.powerwall_host')
    host.local_battery_dispatch_configured = lambda _: True
    monkeypatch.setitem(sys.modules, host.__name__, host)
    alerts = []
    notifications = ModuleType(package + '.powerwall_local.native_max_notifications')
    notifications.update_native_discharge_alert = lambda *args: alerts.append(('discharge', *args))
    notifications.update_native_charge_alert = lambda *args: alerts.append(('charge', *args))
    monkeypatch.setitem(sys.modules, notifications.__name__, notifications)
    adapter = SimpleNamespace(guard=AsyncMock(return_value=allowed),
                              guard_denial_reason=reason)
    controller = SimpleNamespace(pending=False, record=None, adapter=adapter,
                                 start=AsyncMock(), stop=AsyncMock())
    hass = SimpleNamespace()
    scope = dict(__package__=package, _LOGGER=logging.getLogger(__name__),
        _control_call_source=lambda call: source,
        _powerwall_prefers_native_tariff=lambda direction: True,
        entry=SimpleNamespace(entry_id='one', data={}), hass=hass,
        _get_tesla_site_configs=lambda *_: [('site', 'token', 'provider')],
        _local_powerwall_runtime={}, _local_powerwall_route_lock=asyncio.Lock(),
        _stop_local_powerwall_dispatch=AsyncMock(),
        force_charge_state={}, force_discharge_state={},
        hold_soc_state={}, self_consumption_state={},
        _get_native_max_charge=AsyncMock(return_value=controller),
        _native_max_context={}, HomeAssistantError=RuntimeError,
        _create_discharge_tariff=Mock(side_effect=RuntimeError('tariff boundary')),
        _create_charge_tariff=Mock(side_effect=RuntimeError('tariff boundary')))
    tree = ast.parse((ROOT / '__init__.py').read_text())
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
                and n.name == '_try_native_powerwall_force')
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<native-route>', 'exec'), scope)
    with pytest.raises(RuntimeError, match='tariff boundary' if allowed else reason):
        await scope['_try_native_powerwall_force'](
            direction, SimpleNamespace(data={'power_w': 0}), 5, 2000)
    assert alerts == ([(direction, hass, 'one', reason)] if expected else [])
    controller.start.assert_not_awaited()


@pytest.fixture
def notification_probe(monkeypatch):
    package = 'native_alert_helper_probe'
    for name, path in [(package, ROOT),
                       (package + '.powerwall_local', ROOT / 'powerwall_local')]:
        module = ModuleType(name)
        module.__path__ = [str(path)]
        monkeypatch.setitem(sys.modules, name, module)
    constants = ModuleType(package + '.const')
    constants.DOMAIN = 'power_sync'
    monkeypatch.setitem(sys.modules, constants.__name__, constants)
    actions = ModuleType(package + '.automations.actions')
    actions._send_expo_push = AsyncMock()
    monkeypatch.setitem(sys.modules, actions.__name__, actions)
    persistent = SimpleNamespace(async_create=Mock(), async_dismiss=Mock())
    components = ModuleType('homeassistant.components')
    components.persistent_notification = persistent
    monkeypatch.setitem(sys.modules, components.__name__, components)
    module = importlib.import_module(package + '.powerwall_local.native_max_notifications')
    queued = []
    hass = SimpleNamespace(data={'power_sync': {'push_tokens': {'device': {}}}},
                           async_create_task=queued.append)
    yield SimpleNamespace(hass=hass, package=package, update=lambda reason, entry='one':
                          module.update_native_discharge_alert(hass, entry, reason),
                          charge=lambda reason, entry='one':
                          module.update_native_charge_alert(hass, entry, reason),
                          persistent=persistent, push=actions._send_expo_push,
                          queued=queued)
    for pending in queued:
        pending.close()


@pytest.mark.asyncio
async def test_retries_and_integration_reload_share_one_notification(notification_probe):
    p = notification_probe
    p.update('native_export_rule_invalid')
    p.hass.data['power_sync']['one'] = {'replacement_runtime': True}
    p.update('native_export_rule_invalid')
    p.update('native_export_rule_invalid')
    p.persistent.async_create.assert_called_once()
    assert len(p.queued) == 1
    await p.queued[0]
    p.push.assert_awaited_once()
    assert 'did not start' in p.push.await_args.args[2]
    assert 'Grid Export' in p.push.await_args.args[2]


@pytest.mark.asyncio
async def test_recovery_clears_alert_and_supersedes_queued_push(notification_probe):
    p = notification_probe
    p.update('native_export_rule_invalid')
    p.update(None)
    p.update(None)
    p.persistent.async_dismiss.assert_called_once_with(
        p.hass, 'power_sync_one_native_discharge_blocked')
    await p.queued[0]
    p.push.assert_not_awaited()
    p.update('native_export_rule_invalid')
    await p.queued[1]
    assert p.persistent.async_create.call_count == 2
    p.push.assert_awaited_once()


@pytest.mark.asyncio
async def test_distinct_entries_get_independent_alerts(notification_probe):
    p = notification_probe
    p.update('native_export_rule_invalid')
    p.update('native_export_rule_invalid', entry='two')
    p.update(None)
    await asyncio.gather(*p.queued)
    p.push.assert_awaited_once()
    ids = [c.kwargs['notification_id'] for c in p.persistent.async_create.call_args_list]
    assert len(set(ids)) == 2


def test_ordinary_guard_refusals_do_not_generate_alerts(notification_probe):
    p = notification_probe
    for reason in ['discharge_reserve_floor_reached', 'native_snapshot_stale',
                   'managed_ev_ownership_present', None]:
        p.update(reason)
    p.persistent.async_create.assert_not_called()
    assert not p.queued


def test_home_assistant_alert_does_not_require_mobile_tokens(notification_probe):
    p = notification_probe
    p.hass.data['power_sync']['push_tokens'] = {}
    p.update('native_export_rule_invalid')
    p.persistent.async_create.assert_called_once()
    assert not p.queued


@pytest.mark.asyncio
async def test_notification_failures_do_not_escape_or_repeat(notification_probe):
    p = notification_probe
    p.persistent.async_create.side_effect = RuntimeError('notification unavailable')
    p.push.side_effect = OSError('push unavailable')
    p.update('native_export_rule_invalid')
    await p.queued[0]
    p.update('native_export_rule_invalid')
    p.persistent.async_create.assert_called_once()
    p.push.assert_awaited_once()


def test_push_scheduling_failure_cannot_replace_control_error(notification_probe):
    p = notification_probe
    p.hass.async_create_task = Mock(side_effect=RuntimeError('setup stopping'))
    p.update('native_export_rule_invalid')
    p.persistent.async_create.assert_called_once()
    assert not p.queued


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', [
    'configured_charge_disabled', 'grid_charging_not_permitted',
    'configured_grid_charging_disabled', 'grid_charging_permission_unknown',
    'native_import_limit_blocks_charge', 'software_import_limit_invalid',
    'software_import_limit_requires_local_control', 'native_limit_evidence_unavailable',
    'native_site_limits_invalid', 'native_limit_or_config_invalid',
    'native_capacity_unavailable', 'native_snapshot_unavailable',
    'native_max_local_access_disabled', 'native_max_dispatch_config_invalid',
    'native_max_settings_unresolved', 'native_max_local_settings_suspended',
    'native_max_local_control_unconfigured',
])
async def test_actionable_charge_refusals_notify_without_raw_reason_text(notification_probe, reason):
    p = notification_probe
    p.charge(reason)
    p.charge(reason)
    p.persistent.async_create.assert_called_once()
    assert not p.push.await_count  # Delivery is queued, never on the control path.
    await p.queued[0]
    p.push.assert_awaited_once()
    assert p.push.await_args.args[1] == 'Powerwall charging blocked'
    assert 'Scheduled battery charging did not start:' in p.push.await_args.args[2]
    assert reason not in p.push.await_args.args[2]


@pytest.mark.asyncio
async def test_charge_reason_changes_update_alert_without_repeated_push(notification_probe):
    p = notification_probe
    p.charge('grid_charging_not_permitted')
    p.hass.data['power_sync']['one'] = {'replacement_runtime': True}
    p.charge('grid_charging_not_permitted')
    p.charge('software_import_limit_requires_local_control')
    assert p.persistent.async_create.call_count == 2
    assert len(p.queued) == 1
    await p.queued[0]
    p.push.assert_awaited_once()
    assert 'cannot enforce the configured grid-import limit' in p.push.await_args.args[2]
    p.charge(None)
    p.charge('grid_charging_not_permitted')
    await p.queued[1]
    assert p.push.await_count == 2


def test_expected_charge_deferrals_do_not_alert(notification_probe):
    p = notification_probe
    for reason in ['charge_soc_limit_reached_or_unknown', 'ev_demand_requires_local_control',
                   'managed_ev_ownership_present', 'native_max_site_already_owned',
                   'native_max_control_policy_denied', 'unknown_private_reason', None]:
        p.charge(reason)
    p.persistent.async_create.assert_not_called()
    assert not p.queued


@pytest.mark.parametrize('reason', ['native_max_settings_unresolved',
    'native_max_local_settings_suspended', 'native_max_local_control_unconfigured'])
def test_actionable_policy_denials_notify_discharge_with_precise_plain_text(notification_probe, reason):
    p = notification_probe
    p.update(reason)
    p.update(reason)
    p.persistent.async_create.assert_called_once()
    message = p.persistent.async_create.call_args.args[1]
    assert 'Scheduled battery discharge did not start:' in message
    assert reason not in message and 'EV' not in message
    if reason in ('native_max_settings_unresolved', 'native_max_local_settings_suspended'):
        assert 'Repairs notice' in message
        assert 'clear' not in message.lower() and 'reset' not in message.lower()


@pytest.mark.parametrize('reason', ['native_max_startup_pending', 'native_max_dispatch_stopping',
    'native_max_control_method_changing', 'native_max_local_recovery_pending',
    'native_max_restart_restore_pending', 'native_max_restore_pending',
    'native_max_max_backup_active', 'native_max_monitoring_mode',
    'native_max_demand_charge_protection', 'native_max_network_export_guard_active',
    'native_max_license_denied',
    'native_max_policy_evidence_unavailable', 'native_max_policy_denied',
    'charge_soc_limit_reached_or_unknown', 'discharge_reserve_floor_reached',
    'managed_ev_ownership_present', 'native_max_site_already_owned'])
def test_expected_native_deferrals_remain_quiet_in_both_directions(notification_probe, reason):
    p = notification_probe
    p.update(reason)
    p.charge(reason)
    p.persistent.async_create.assert_not_called()
    assert not p.queued


@pytest.mark.asyncio
async def test_charge_recovery_does_not_clear_discharge_alert_or_send_stale_push(notification_probe):
    p = notification_probe
    p.update('native_export_rule_invalid')
    p.charge('grid_charging_not_permitted')
    p.charge(None)
    await asyncio.gather(*p.queued)
    p.push.assert_awaited_once()
    assert p.push.await_args.args[1] == 'Powerwall discharge blocked'
    p.persistent.async_dismiss.assert_called_once_with(
        p.hass, 'power_sync_one_native_charge_blocked')
    assert 'one' in p.hass.data['power_sync']['_native_discharge_alerts']


def test_previous_discharge_alert_marker_remains_deduplicated(notification_probe):
    p = notification_probe
    p.hass.data['power_sync']['_native_discharge_alerts'] = {'one': object()}
    p.update('native_export_rule_invalid')
    p.persistent.async_create.assert_not_called()
    p.update(None)
    p.persistent.async_dismiss.assert_called_once()
