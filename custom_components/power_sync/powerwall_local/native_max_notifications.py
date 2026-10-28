"""User feedback for actionable native charge/discharge admission failures."""
from __future__ import annotations

import asyncio
import logging

from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)
_DISCHARGE_MESSAGE = (
    "Scheduled battery discharge did not start because PowerSync could not "
    "confirm the Powerwall's Grid Export setting. Check the PowerSync logs "
    "and contact support if this continues."
)
_CHARGE_REASONS = {
    'configured_charge_disabled': 'the configured battery charging limit is disabled',
    'grid_charging_not_permitted': 'the gateway does not currently permit grid charging',
    'native_import_limit_blocks_charge': 'the gateway import limit blocks charging',
    'software_import_limit_invalid': 'the configured grid-import limit is invalid',
    'software_import_limit_requires_local_control': 'native charging cannot enforce the configured grid-import limit',
    'native_limit_evidence_unavailable': 'the required gateway import-limit readings are unavailable or stale',
    'native_site_limits_invalid': 'the gateway site limits could not be confirmed',
    'native_limit_or_config_invalid': 'the gateway limits or charging settings could not be validated',
    'native_capacity_unavailable': 'the gateway battery power rating is unavailable',
    'native_snapshot_unavailable': 'current gateway readings are unavailable',
    'native_max_local_access_disabled': 'the required local gateway access is disabled',
    'native_max_dispatch_config_invalid': 'the charging configuration is invalid',
    'native_max_policy_denied': 'the native charging checks did not pass',
}


def update_native_discharge_alert(hass, entry_id: str, reason: str | None) -> None:
    if reason is None or reason == 'native_export_rule_invalid':
        _update_native_alert(hass, entry_id, 'discharge', _DISCHARGE_MESSAGE, reason)


def update_native_charge_alert(hass, entry_id: str, reason: str | None) -> None:
    """Report actionable refusals, leaving SOC/EV/ownership deferrals quiet."""
    if reason is not None and reason not in _CHARGE_REASONS:
        return
    message = (
        f'Scheduled battery charging did not start: {_CHARGE_REASONS[reason]}. '
        'Check the PowerSync logs and charging settings; contact support if this continues.'
        if reason is not None else ''
    )
    _update_native_alert(hass, entry_id, 'charge', message, reason)


def _update_native_alert(hass, entry_id, direction, message, reason):
    """Alert once per continuing refusal; clear when admission succeeds.

    Domain-level state survives integration reloads. Notifications never hold
    the physical control route while a mobile push waits on the network.
    """
    domain = hass.data.setdefault(DOMAIN, {})
    alerts = domain.setdefault(f'_native_{direction}_alerts', {})
    notification_id = f'{DOMAIN}_{entry_id}_native_{direction}_blocked'
    title = 'Powerwall charging blocked' if direction == 'charge' else 'Powerwall discharge blocked'
    if reason is None:
        if alerts.pop(entry_id, None) is not None:
            try:
                from homeassistant.components import persistent_notification
                persistent_notification.async_dismiss(hass, notification_id)
            except Exception:
                _LOGGER.exception('Could not clear Powerwall %s notification', direction)
        return
    previous = alerts.get(entry_id)
    # A v2.12.1527 discharge alert is an opaque marker; retain its deduplication.
    if previous is not None and not isinstance(previous, dict):
        return
    if previous is not None and previous['message'] == message:
        return
    record = previous if previous is not None else {}
    record['message'] = message
    alerts[entry_id] = record
    try:
        from homeassistant.components import persistent_notification
        persistent_notification.async_create(
            hass, message, title=title, notification_id=notification_id)
    except Exception:
        _LOGGER.exception('Could not create Powerwall %s notification', direction)

    async def push():
        # A newer successful admission can supersede a queued alert.
        if alerts.get(entry_id) is not record:
            return
        try:
            from ..automations.actions import _send_expo_push
            await asyncio.wait_for(_send_expo_push(hass, title, record['message']), timeout=20)
        except Exception:
            _LOGGER.exception('Could not send Powerwall %s push notification', direction)

    if previous is None and domain.get('push_tokens'):
        pending = push()
        try:
            hass.async_create_task(pending)
        except Exception:
            pending.close()
            _LOGGER.exception('Could not schedule Powerwall %s push notification', direction)
