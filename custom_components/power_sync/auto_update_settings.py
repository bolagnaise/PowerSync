"""Dashboard access to the existing PowerSync auto-update schedule."""

from __future__ import annotations

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .auto_update import normalize_auto_update_time
from .const import CONF_AUTO_UPDATE_TIME, DEFAULT_AUTO_UPDATE_TIME, DOMAIN


class AutoUpdateSettingsView(HomeAssistantView):
    """Edit the schedule for the entry owning the dashboard's switch."""

    url = "/api/power_sync/auto_update/settings"
    name = "api:power_sync:auto_update:settings"
    requires_auth = True

    def __init__(self, hass):
        self._hass = hass

    def _entry(self, request):
        entity_id = request.query.get("entity_id", "")
        entity = er.async_get(self._hass).async_get(entity_id)
        if entity is None or entity.platform != DOMAIN:
            return None
        entry = self._hass.config_entries.async_get_entry(entity.config_entry_id)
        return entry if entry is not None and entry.domain == DOMAIN else None

    async def get(self, request):
        entry = self._entry(request)
        if entry is None:
            return web.json_response({"success": False, "error": "PowerSync entry not found"}, status=404)
        return web.json_response({"success": True, "config": {
            CONF_AUTO_UPDATE_TIME: entry.options.get(
                CONF_AUTO_UPDATE_TIME, entry.data.get(CONF_AUTO_UPDATE_TIME, DEFAULT_AUTO_UPDATE_TIME)
            ),
        }})

    async def post(self, request):
        user = request.get("hass_user")
        if user is None or not user.is_admin:
            return web.json_response({"success": False, "error": "An administrator must change the update schedule."}, status=403)
        entry = self._entry(request)
        if entry is None:
            return web.json_response({"success": False, "error": "PowerSync entry not found"}, status=404)
        try:
            data = await request.json()
            if not isinstance(data, dict) or set(data) != {CONF_AUTO_UPDATE_TIME} or not data[CONF_AUTO_UPDATE_TIME]:
                raise ValueError("Provide only the scheduled update time.")
            value = normalize_auto_update_time(data[CONF_AUTO_UPDATE_TIME])
        except (TypeError, ValueError) as err:
            return web.json_response({"success": False, "error": str(err)}, status=400)
        options = {**entry.options, CONF_AUTO_UPDATE_TIME: value}
        if options != entry.options:
            # The minute-tick scheduler reads the current entry options. A
            # schedule edit does not need an integration reload.
            entry_data = self._hass.data.get(DOMAIN, {}).get(entry.entry_id)
            if isinstance(entry_data, dict):
                entry_data["_skip_reload"] = True
            self._hass.config_entries.async_update_entry(entry, options=options)
        async_dispatcher_send(self._hass, f"{DOMAIN}_{entry.entry_id}_auto_update_state")
        return web.json_response({"success": True, "config": {CONF_AUTO_UPDATE_TIME: value}})
