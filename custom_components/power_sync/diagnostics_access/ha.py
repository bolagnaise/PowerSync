"""HA admin API and lifecycle. Independent from ticket support and controls."""
import asyncio
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from .runtime import AccessRuntime, DATA
from .wire import WireError, loads


class DiagnosticsView(HomeAssistantView):
    url = "/api/power_sync/diagnostics"
    name = "api:power_sync:diagnostics"
    requires_auth = True

    def __init__(self, hass):
        self.hass = hass

    def runtime(self, request):
        user = request.get("hass_user")
        if not user or not user.is_admin:
            raise web.HTTPForbidden()
        runtime = self.hass.data.get(DATA, {}).get("runtime")
        if runtime is None:
            raise web.HTTPServiceUnavailable()
        return user, runtime

    async def get(self, request):
        _, runtime = self.runtime(request)
        return web.json_response(runtime.status(), headers={"Cache-Control": "no-store"})

    async def post(self, request):
        user, runtime = self.runtime(request)
        try:
            raw = bytearray()
            async for chunk in request.content.iter_chunked(1024):
                raw.extend(chunk)
                if len(raw) > 8192:
                    raise WireError("request_limit")
            value = loads(bytes(raw), maximum=8192)
            if set(value) - {"action", "code", "authority_key", "challenge", "grant"}:
                raise WireError("invalid_request")
            action = value.get("action")
            if action == "identity":
                result = await runtime.setup_identity()
            elif action == "review":
                result = await runtime.review(user.id, value.get("code"), value.get("authority_key"))
            elif action == "confirm":
                result = await runtime.confirm(user.id, value.get("challenge"))
            elif action == "revoke":
                await runtime.revoke(value.get("grant"))
                result = runtime.status()
            else:
                raise WireError("unsupported_action")
            return web.json_response(result, headers={"Cache-Control": "no-store"})
        except (WireError, KeyError, TypeError):
            return web.json_response({"error": "diagnostics_request_denied"}, status=409)


async def setup(hass, entry):
    data = hass.data.setdefault(DATA, {"entries": set()})
    if "runtime" not in data:
        from ..ha_read.adapters import create_backend
        runtime = AccessRuntime(hass, Store(hass, 1, "power_sync.diagnostics_access", private=True, atomic_writes=True), async_get_clientsession(hass), create_backend(hass))
        await runtime.load()
        data["runtime"] = runtime
        if not hass.data.get(DATA + "_view"):
            hass.http.register_view(DiagnosticsView(hass))
            hass.data[DATA + "_view"] = True
    data["entries"].add(entry.entry_id)


async def unload(hass, entry):
    data = hass.data.get(DATA)
    if data:
        data["entries"].discard(entry.entry_id)
        if not data["entries"]:
            await data["runtime"].stop()
            hass.data.pop(DATA, None)


async def remove(hass, entry):
    data = hass.data.get(DATA)
    if data and data["entries"] - {entry.entry_id}:
        return
    if not data:
        await setup(hass, entry)
        data = hass.data[DATA]
    await data["runtime"].revoke()
    try:
        await asyncio.wait_for(data["runtime"].drain(), timeout=10)
    finally:
        await unload(hass, entry)
