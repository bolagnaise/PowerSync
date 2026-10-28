"""User feedback for an actionable native discharge admission failure."""
from __future__ import annotations

import asyncio
import logging

from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)
_ALERTS = '_native_discharge_alerts'
_REASON = 'native_export_rule_invalid'
_TITLE = 'Powerwall discharge blocked'
_MESSAGE = (
    "Scheduled battery discharge did not start because PowerSync could not "
    "confirm the Powerwall's Grid Export setting. Check the PowerSync logs "
    "and contact support if this continues."
)


def update_native_discharge_alert(hass, entry_id: str, reason: str | None) -> None:
    """Alert once per continuing refusal; clear when admission succeeds.

    Domain-level state survives integration reloads. Notifications never hold
    the physical control route while a mobile push waits on the network.
    """
    if reason is not None and reason != _REASON:
        return
    domain = hass.data.setdefault(DOMAIN, {})
    alerts = domain.setdefault(_ALERTS, {})
    notification_id = f'{DOMAIN}_{entry_id}_native_discharge_blocked'
    if reason is None:
        if alerts.pop(entry_id, None) is not None:
            try:
                from homeassistant.components import persistent_notification
                persistent_notification.async_dismiss(hass, notification_id)
            except Exception:
                _LOGGER.exception('Could not clear Powerwall discharge notification')
        return
    if entry_id in alerts:
        return
    record = object()
    alerts[entry_id] = record
    try:
        from homeassistant.components import persistent_notification
        persistent_notification.async_create(
            hass, _MESSAGE, title=_TITLE, notification_id=notification_id)
    except Exception:
        _LOGGER.exception('Could not create Powerwall discharge notification')

    async def push():
        # A newer successful admission can supersede a queued alert.
        if alerts.get(entry_id) is not record:
            return
        try:
            from ..automations.actions import _send_expo_push
            await asyncio.wait_for(_send_expo_push(hass, _TITLE, _MESSAGE), timeout=20)
        except Exception:
            _LOGGER.exception('Could not send Powerwall discharge push notification')

    if domain.get('push_tokens'):
        pending = push()
        try:
            hass.async_create_task(pending)
        except Exception:
            pending.close()
            _LOGGER.exception('Could not schedule Powerwall discharge push notification')
