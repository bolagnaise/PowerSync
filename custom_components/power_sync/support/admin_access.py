"""Owner-approved access to Home Assistant as a temporary administrator."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from hashlib import sha256
from hmac import compare_digest
import json
import logging
import re
import time
from uuid import uuid4

from aiohttp import web
from homeassistant.auth.const import GROUP_ID_ADMIN
from homeassistant.components.http import HomeAssistantView
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers.storage import Store

DATA = "power_sync_support_admin"
PATH = "/api/power_sync/support/admin"
APPROVAL_SECONDS = 300
ACCESS_SECONDS = 1800
USER_PREFIX = "PowerSync temporary support "
CHALLENGE = re.compile(r"[a-f0-9]{64}")
_LOGGER = logging.getLogger(__name__)


class AccessDenied(Exception):
    """The requested support action does not have a valid grant."""


class AdminAccess:
    def __init__(self, hass):
        self.hass = hass
        self.store = Store(hass, 1, "power_sync.support_admin", private=True, atomic_writes=True)
        self.lock = asyncio.Lock()
        self.pending = None
        self.user = None
        self.refresh = self.access_token = None
        self.deadline = 0
        self.expires_at = 0
        self.timer = None
        self.ready = False
        self.stopped = False
        self.cleanup_pending = False

    async def initialize(self):
        async with self.lock:
            await self._remove_account()
            self.ready = True

    def status(self):
        if self.cleanup_pending:
            return {"state": "cleanup_pending"}
        if not self.ready or self.stopped:
            return {"state": "unavailable"}
        if self.deadline <= time.monotonic():
            return {"state": "closed"}
        return {
            "state": "pending" if self.pending else "active",
            "expires_at": self.expires_at,
        }

    async def approve(self, owner, challenge):
        if (not owner or not owner.is_owner or not isinstance(challenge, str)
                or not CHALLENGE.fullmatch(challenge)):
            raise AccessDenied
        async with self.lock:
            if not self.ready or self.stopped or self.status()["state"] != "closed":
                raise AccessDenied
            await self._remove_account()
            self.pending = challenge
            self._schedule(APPROVAL_SECONDS)

    async def redeem(self, verifier):
        if not isinstance(verifier, str) or not CHALLENGE.fullmatch(verifier):
            raise AccessDenied
        async with self.lock:
            if (not self.ready or self.stopped or self.status()["state"] != "pending"
                    or not compare_digest(sha256(verifier.encode()).hexdigest(), self.pending)):
                raise AccessDenied
            self.pending = None
            self._schedule(ACCESS_SECONDS)
            name = USER_PREFIX + uuid4().hex
            try:
                # Record intent before creation so HA can remove the account after a crash.
                await self.store.async_save({"name": name})
                self.user = await self.hass.auth.async_create_system_user(
                    name, group_ids=[GROUP_ID_ADMIN]
                )
                self.refresh = await self.hass.auth.async_create_refresh_token(
                    self.user, access_token_expiration=timedelta(seconds=ACCESS_SECONDS)
                )
                if self.deadline <= time.monotonic():
                    raise AccessDenied
                self._issue_access_token()
            except BaseException:
                await self._close()
                raise
            return self._lease_status()

    def _issue_access_token(self):
        self.access_token = self.hass.auth.async_create_access_token(self.refresh)
        self._schedule(ACCESS_SECONDS)

    def _lease_status(self):
        return {
            "access_token": self.access_token,
            "expires_at": self.expires_at,
            "renew_at": self.expires_at - max(1, ACCESS_SECONDS // 6),
        }

    async def renew(self, user):
        async with self.lock:
            if (not user or self.user is None or user.id != self.user.id
                    or self.status()["state"] != "active"):
                raise AccessDenied
            now = time.monotonic()
            if now > self.deadline - max(1, ACCESS_SECONDS // 6):
                self._issue_access_token()
            return self._lease_status()

    async def revoke(self, user):
        async with self.lock:
            if not user or not (user.is_owner or (self.user is not None and user.id == self.user.id)):
                raise AccessDenied
            await self._close()
            self.ready = True

    async def stop(self, _event):
        async with self.lock:
            self.stopped = True
            await self._close()

    def _schedule(self, seconds):
        if self.timer:
            self.timer.cancel()
        self.deadline = time.monotonic() + seconds
        self.expires_at = int(time.time() + seconds)
        self.timer = self.hass.loop.call_later(max(0, self.deadline - time.monotonic()), self._expire, self.deadline)

    def _expire(self, deadline):
        self.hass.async_create_task(self._expire_locked(deadline))

    async def _expire_locked(self, deadline):
        async with self.lock:
            if deadline == self.deadline:
                await self._close()

    async def _close(self):
        if self.timer:
            self.timer.cancel()
            self.timer = None
        self.deadline = self.expires_at = 0
        self.refresh = self.access_token = None
        self.pending = None
        self.cleanup_pending = True
        await self._remove_account()
        self.cleanup_pending = False

    async def _remove_account(self):
        account = await self.hass.auth.async_get_user(self.user.id) if self.user is not None else None
        if account is None:
            record = await self.store.async_load()
            if record is None:
                return
            name = record.get("name") if isinstance(record, dict) else None
            if not isinstance(name, str) or not re.fullmatch(re.escape(USER_PREFIX) + "[a-f0-9]{32}", name):
                raise ValueError("Invalid temporary account record")
            account = next(
                (user for user in await self.hass.auth.async_get_users()
                 if user.system_generated and not user.is_owner and user.name == name),
                None,
            )
        if account is not None:
            # This also closes authenticated native WebSocket connections.
            for token in tuple(account.refresh_tokens.values()):
                self.hass.auth.async_remove_refresh_token(token)
            await self.hass.auth.async_remove_user(account)
        self.user = None
        await self.store.async_remove()


async def _body(request, fields):
    raw = bytearray()
    async for chunk in request.content.iter_chunked(1024):
        raw.extend(chunk)
        if len(raw) > 1024:
            raise AccessDenied
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise AccessDenied from None
    if type(value) is not dict or set(value) - fields:
        raise AccessDenied
    return value


def _response(value, status=200):
    return web.json_response(value, status=status, headers={"Cache-Control": "no-store"})


class AdminAccessView(HomeAssistantView):
    url = PATH
    name = "api:power_sync:support:admin"
    requires_auth = True

    def __init__(self, manager):
        self.manager = manager

    async def post(self, request):
        try:
            body = await _body(request, {"action", "challenge"})
            user = request.get("hass_user")
            if body.get("action") == "approve":
                await self.manager.approve(user, body.get("challenge"))
            elif body == {"action": "renew"}:
                return _response(await self.manager.renew(user))
            elif body == {"action": "revoke"}:
                await self.manager.revoke(user)
            else:
                raise AccessDenied
        except AccessDenied:
            return _response({"error": "admin_support_denied"}, 403)
        except Exception:
            _LOGGER.error("Could not change temporary PowerSync support access")
            return _response({"error": "admin_support_unavailable"}, 503)
        return _response(self.manager.status())


class AdminRedeemView(HomeAssistantView):
    url = PATH + "/redeem"
    name = "api:power_sync:support:admin:redeem"
    requires_auth = False

    def __init__(self, manager):
        self.manager = manager

    async def post(self, request):
        try:
            body = await _body(request, {"verifier"})
            return _response(await self.manager.redeem(body.get("verifier")))
        except AccessDenied:
            return _response({"error": "admin_support_denied"}, 403)
        except Exception:
            _LOGGER.error("Could not redeem temporary PowerSync support access")
            return _response({"error": "admin_support_unavailable"}, 503)


def setup(hass):
    if DATA in hass.data:
        return
    manager = hass.data[DATA] = AdminAccess(hass)
    hass.http.register_view(AdminAccessView(manager))
    hass.http.register_view(AdminRedeemView(manager))
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, manager.stop)
    hass.async_create_background_task(_initialize(manager), "power_sync_support_admin_cleanup", eager_start=False)


async def _initialize(manager):
    try:
        async with asyncio.timeout(5):
            await manager.initialize()
    except Exception:
        _LOGGER.error("Could not initialize temporary PowerSync support access")
