"""Read-only status and administrator-only activation, refresh and explicit Resume."""
from __future__ import annotations

import time
from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .admission import OptimizerAdmission
from .runtime import DATA_KEY as MANAGER_KEY

DATA_KEY = "power_sync_licensing"


def licensing_status(hass, entry_id=None) -> dict:
    """Keep trial/access status visible even without an optimizer coordinator."""
    if manager := hass.data.get(MANAGER_KEY):
        return manager.status(entry_id)
    admission = hass.data.get(DATA_KEY)
    if admission is None:
        admission = OptimizerAdmission(enforced=False)
        hass.data[DATA_KEY] = admission
    return admission.api_status(int(time.time()))


class LicensingStatusView(HomeAssistantView):
    url = "/api/power_sync/licensing"
    name = "api:power_sync:licensing"
    requires_auth = True

    def __init__(self, hass):
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        entry_id = request.query.get("entry_id")
        manager = self._hass.data.get(MANAGER_KEY)
        status = licensing_status(self._hass, entry_id)
        if manager:
            status["entries"] = {key: manager.status(key) for key in manager.coordinators}
        return web.json_response(status)

    async def post(self, request: web.Request) -> web.Response:
        user = request.get("hass_user")
        if not user or not user.is_admin:
            return web.json_response({"error": "admin_required"}, status=403)
        if request.content_length and request.content_length > 4096:
            return web.json_response({"error": "invalid_request"}, status=400)
        try:
            chunks, size = [], 0
            while chunk := await request.content.read(4097):
                size += len(chunk)
                if size > 4096:
                    raise ValueError("invalid_request")
                chunks.append(chunk)
            raw = b"".join(chunks)
            import json
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("invalid_request")
            from .manager import async_get_manager
            manager = await async_get_manager(self._hass)
            action = body.get("action")
            if action == "start_pairing":
                return web.json_response(await manager.start_pairing())
            if action == "finish_pairing":
                code = body.get("pairing_code")
                if not isinstance(code, str) or len(code) != 43:
                    raise ValueError("invalid_pairing_code")
                await manager.finish_pairing(code)
            elif action == "refresh":
                await manager.maintain(force=True)
                await manager.reconcile_entries()
            elif action == "resume":
                entry_id = body.get("entry_id")
                if not isinstance(entry_id, str):
                    raise ValueError("entry_id_required")
                await manager.resume_entry(entry_id)
            elif action == "reset_pairing":
                await manager.reset_pairing()
            else:
                raise ValueError("invalid_action")
            return web.json_response({"success": True, **manager.status(body.get("entry_id"))})
        except ValueError as error:
            return web.json_response({"error": str(error)}, status=409)
        except Exception:
            return web.json_response({"error": "licensing_temporarily_unavailable"}, status=503)


class ReferralStatusView(HomeAssistantView):
    """Administrator-only display endpoint; credentials stay on the HA server."""
    url = "/api/power_sync/referrals"
    name = "api:power_sync:referrals"
    requires_auth = True

    def __init__(self, hass):
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        user = request.get("hass_user")
        if not user or not user.is_admin:
            return web.json_response({"error": "admin_required"}, status=403)
        from .manager import async_get_manager
        manager = await async_get_manager(self._hass)
        response = web.json_response(await manager.async_referrals(force=request.query.get("refresh") == "1"))
        response.headers["Cache-Control"] = "no-store"
        return response
