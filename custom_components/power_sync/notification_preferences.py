"""Device push preferences and the Home Assistant update notice setting."""

from __future__ import annotations

import asyncio
import re

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

DATA_KEY = f"{DOMAIN}.notification_preferences"
TYPE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}", re.ASCII)


def normalize_disabled_notification_types(value: object) -> list[str]:
    """Validate a full device preference list, including future event IDs."""
    if not isinstance(value, list) or len(value) > 128 or any(
        not isinstance(item, str) or not TYPE_PATTERN.fullmatch(item)
        for item in value
    ):
        raise ValueError("invalid_disabled_notification_types")
    return sorted(set(value))


class NotificationPreferences:
    """Serialize settings writes and expose only committed preferences."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store(hass, 1, DATA_KEY, private=True, atomic_writes=True)
        self._lock = asyncio.Lock()
        self._loaded = False
        self.update_alerts_enabled = True
        self.disabled_by_token: dict[str, list[str]] = {}

    async def async_load(self) -> None:
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            saved = await self._store.async_load() or {}
            self.update_alerts_enabled = saved.get("update_alerts_enabled") is not False
            self.disabled_by_token = saved.get("disabled_by_token") or {}
            self._loaded = True

    async def async_set_device_types(self, token: str, disabled: list[str]) -> None:
        async with self._lock:
            if self.disabled_by_token.get(token, []) == disabled:
                return
            updated = dict(self.disabled_by_token)
            if disabled:
                updated[token] = disabled
            else:
                updated.pop(token, None)
            await self._async_save(self.update_alerts_enabled, updated)
            self.disabled_by_token = updated

    async def async_set_update_alerts(self, enabled: bool) -> None:
        async with self._lock:
            if self.update_alerts_enabled == enabled:
                return
            await self._async_save(enabled, self.disabled_by_token)
            self.update_alerts_enabled = enabled

    async def _async_save(
        self, update_alerts_enabled: bool, disabled_by_token: dict[str, list[str]]
    ) -> None:
        await self._store.async_save({
            "update_alerts_enabled": update_alerts_enabled,
            "disabled_by_token": disabled_by_token,
        })


async def async_get_notification_preferences(hass: HomeAssistant) -> NotificationPreferences:
    preferences = hass.data.get(DATA_KEY)
    if preferences is None:
        preferences = NotificationPreferences(hass)
        hass.data[DATA_KEY] = preferences
    await preferences.async_load()
    return preferences


def update_alerts_enabled(hass: HomeAssistant) -> bool:
    preferences = hass.data.get(DATA_KEY)
    return preferences is None or preferences.update_alerts_enabled


def push_notification_enabled(hass: HomeAssistant, token: str, notification_type: str | None) -> bool:
    preferences = hass.data.get(DATA_KEY)
    return (
        notification_type is None or preferences is None
        or notification_type not in preferences.disabled_by_token.get(token, [])
    )


class NotificationSettingsView(HomeAssistantView):
    """Expose the update notice preference for this Home Assistant server."""

    url = "/api/power_sync/notifications/settings"
    name = "api:power_sync:notifications:settings"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        preferences = await async_get_notification_preferences(self._hass)
        user = request.get("hass_user")
        return web.json_response({
            "update_alerts_enabled": preferences.update_alerts_enabled,
            "can_edit": bool(user and user.is_admin),
        })

    async def post(self, request: web.Request) -> web.Response:
        user = request.get("hass_user")
        if user is None or not user.is_admin:
            return web.json_response({"error": "A Home Assistant administrator must change this setting."}, status=403)
        try:
            data = await request.json()
            if (
                not isinstance(data, dict) or set(data) != {"update_alerts_enabled"}
                or not isinstance(data["update_alerts_enabled"], bool)
            ):
                raise ValueError
        except (TypeError, ValueError):
            return web.json_response({"error": "Provide update_alerts_enabled as a boolean."}, status=400)

        preferences = await async_get_notification_preferences(self._hass)
        await preferences.async_set_update_alerts(data["update_alerts_enabled"])
        return web.json_response({
            "update_alerts_enabled": preferences.update_alerts_enabled,
            "can_edit": True,
        })
