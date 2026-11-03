"""Notify when Tesla reports active Storm Watch protection."""
from __future__ import annotations

import asyncio
import logging

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
_TITLE = "Storm Watch Active"
_MESSAGE = (
    "Tesla has activated Storm Watch for your Powerwall. It may charge from "
    "the grid or pause normal battery discharge while storm protection is active."
)


def update_storm_watch_alert(hass, entry_id: str, site_id: str, active: object) -> None:
    """Alert once per observed activation, including an already-active startup.

    Only an explicit boolean from live_status changes the alert. Missing or
    malformed readings cannot clear protection or rearm repeated pushes.
    Domain-level records survive integration reloads; no device setting changes.
    """
    if type(active) is not bool:
        return
    domain = hass.data.setdefault(DOMAIN, {})
    alerts = domain.setdefault("_tesla_storm_watch_alerts", {})
    key = (entry_id, str(site_id))
    notification_id = f"{DOMAIN}_{entry_id}_{site_id}_storm_watch_active"
    if not active:
        if alerts.pop(key, None) is not None:
            try:
                from homeassistant.components import persistent_notification

                persistent_notification.async_dismiss(hass, notification_id)
            except Exception:
                _LOGGER.exception("Could not clear Storm Watch notification")
        return
    if key in alerts:
        return
    record = object()
    alerts[key] = record
    try:
        from homeassistant.components import persistent_notification

        persistent_notification.async_create(
            hass, _MESSAGE, title=_TITLE, notification_id=notification_id)
    except Exception:
        _LOGGER.exception("Could not create Storm Watch notification")

    async def push():
        if alerts.get(key) is not record:
            return
        try:
            from .automations.actions import _send_expo_push

            await asyncio.wait_for(_send_expo_push(hass, _TITLE, _MESSAGE), timeout=20)
        except Exception:
            _LOGGER.exception("Could not send Storm Watch push notification")

    if domain.get("push_tokens"):
        pending = push()
        try:
            hass.async_create_task(pending)
        except Exception:
            pending.close()
            _LOGGER.exception("Could not schedule Storm Watch push notification")
