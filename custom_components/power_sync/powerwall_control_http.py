"""Aiohttp-compatible final gates for Tesla energy-site POSTs.

Matching uses the Tesla endpoint path, including provider proxy prefixes.
Receipts use code-owned labels and never contain URLs, credentials or bodies.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
import inspect
import re
import sys
from urllib.parse import unquote, urlsplit

from .powerwall_control import ControlRequestStale, canonical_site_id, coordinator_for

_ENERGY_POST = re.compile(r"(?:^|/)api/1/energy_sites/([^/]+)/(.+?)/?$")
_LABELS = frozenset({"operation", "backup", "grid_import_export", "time_of_use_settings", "device_command", "command"})


def _cloud_settings_allowed(hass, site_id):
    """Keep every cloud writer behind the authoritative signed-write incident.

    The existing supervised recovery records an exact incident handoff, also
    restored during startup. This predicate only observes those records.
    """
    for runtime in hass.data.get("power_sync", {}).values():
        if not isinstance(runtime, Mapping):
            continue
        pending = runtime.get("powerwall_settings_pending")
        if not pending and not runtime.get("powerwall_settings_recovering"):
            continue
        control = runtime.get("powerwall_site_control")
        identity = pending.get("site_id") if isinstance(pending, Mapping) else None
        if identity is None:
            identity = getattr(control, "site_id", None)
        if identity is None:
            entry = runtime.get("entry")
            if entry is not None:
                config = {**getattr(entry, "data", {}), **getattr(entry, "options", {})}
                identity = config.get("tesla_energy_site_id") or config.get("powerwall_local_energy_site_id")
        try:
            if canonical_site_id(identity) != site_id:
                continue
        except (TypeError, ValueError):
            # An incident without any recoverable physical identity cannot be
            # proven unrelated to this site. Keep it fenced until identified.
            return False
        if runtime.get("powerwall_settings_recovering"):
            return False
        if pending and not (isinstance(pending, Mapping) and pending.get("resume_cloud") is True
                            and pending.get("id")
                            and runtime.get("powerwall_settings_cloud_handoff") == pending["id"]):
            return False
    return True


def async_get_clientsession(hass):
    """Return HA's shared session with the Tesla mutation boundary attached."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession as raw_session
    return controlled_session(hass, raw_session(hass))


def controlled_session(hass, raw_session, *, is_current=None, identity=None, before_send=None):
    """Preserve reads/other services while ordering Tesla energy mutations.

    Per-request ``_power_sync_is_current``, ``_power_sync_before_send``,
    ``_power_sync_deadline`` and ``_power_sync_on_rejected`` compose the
    original caller's final fences. They are never forwarded to aiohttp.
    """
    if isinstance(raw_session, _ControlledSession) and is_current is None and identity is None and before_send is None:
        return raw_session
    if isinstance(raw_session, _ControlledSession):
        result = _ControlledSession(hass, raw_session._raw, is_current, identity, before_send)
        result._guards = raw_session._guards + result._guards
        result._identities = raw_session._identities + result._identities
        result._fences = raw_session._fences + result._fences
        return result
    return _ControlledSession(hass, raw_session, is_current, identity, before_send)


class _ControlledSession:
    def __init__(self, hass, raw, is_current, identity, before_send):
        self._hass, self._raw = hass, raw
        self._guards = (is_current,) if is_current is not None else ()
        self._identities = (identity,) if identity is not None else ()
        self._fences = (before_send,) if before_send is not None else ()

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def post(self, url, *args, **kwargs):
        return self._mutation(url, lambda: self._raw.post(url, *args, **kwargs), kwargs)

    def request(self, method, url, *args, **kwargs):
        if str(method).upper() in {"GET", "HEAD", "OPTIONS"}:
            _markers(kwargs)
            return self._raw.request(method, url, *args, **kwargs)
        return self._mutation(url, lambda: self._raw.request(method, url, *args, **kwargs), kwargs)

    def put(self, url, *args, **kwargs):
        return self._mutation(url, lambda: self._raw.put(url, *args, **kwargs), kwargs)

    def patch(self, url, *args, **kwargs):
        return self._mutation(url, lambda: self._raw.patch(url, *args, **kwargs), kwargs)

    def delete(self, url, *args, **kwargs):
        return self._mutation(url, lambda: self._raw.delete(url, *args, **kwargs), kwargs)

    def _mutation(self, url, create_request, kwargs):
        guard, fence, on_rejected, deadline = _markers(kwargs)
        try:
            match = _ENERGY_POST.search(urlsplit(str(url)).path)
            if match is not None:
                site_id, suffix = match.groups()
                label = suffix if suffix in _LABELS else "energy_command"
                site = coordinator_for(self._hass, site_id=unquote(site_id))
        except BaseException:
            if on_rejected is not None:
                on_rejected()
            raise
        if match is None:
            return create_request()

        def current():
            if not _cloud_settings_allowed(self._hass, site.site_id):
                return False
            for check in (*self._guards, guard):
                if check is not None:
                    result = check()
                    if result is not True:
                        if inspect.iscoroutine(result):
                            result.close()
                        return False
            for before_send in (*self._fences, fence):
                if before_send is not None:
                    result = before_send()
                    if inspect.isawaitable(result):
                        if inspect.iscoroutine(result):
                            result.close()
                        raise ControlRequestStale("Final mutation fence must be synchronous")
            return True

        def identity():
            try:
                return site.site_id if all(canonical_site_id(check()) == site.site_id
                                           for check in self._identities) else None
            except (TypeError, ValueError):
                return None

        return _ControlledRequest(site, site.write_context(f"cloud_{label}", is_current=current,
                                                          identity=identity if self._identities else None,
                                                          deadline=deadline), create_request, on_rejected)


def _markers(kwargs):
    """Remove internal final fences before any aiohttp call is constructed."""
    return tuple(kwargs.pop(f"_power_sync_{name}", None)
                 for name in ("is_current", "before_send", "on_rejected", "deadline"))


class _ControlledRequest:
    """Delay creating the raw request until the final gate has been acquired."""
    def __init__(self, site, gate, create_request, on_rejected=None):
        self._site = site
        self._gate, self._create_request = gate, create_request
        self._request = None
        self._receipt = None
        self._on_rejected = on_rejected
        self._raw_created = False
        self._rejected = False

    def _reject(self):
        if self._receipt is not None:
            self._site.mark_effect_not_submitted(self._receipt)
        if not self._rejected and self._on_rejected is not None:
            self._rejected = True
            self._on_rejected()

    async def __aenter__(self):
        try:
            self._receipt = await self._gate.__aenter__()
        except BaseException:
            self._reject()
            raise
        try:
            fence = self._site.current_fence()
            async def open_request():
                fence()
                self._raw_created = True
                self._request = self._create_request()
                return await self._request.__aenter__()
            opening = asyncio.create_task(open_request())
            try:
                return await asyncio.shield(opening)
            except asyncio.CancelledError:
                cancelled = sys.exc_info()
                if not self._raw_created:
                    opening.cancel()
                # The raw driver has already been invoked. Drain its bounded
                # response instead of abandoning an in-flight write and letting
                # another site mutation race it. Status reads remain unlocked.
                try:
                    await _drain(opening)
                except BaseException:
                    pass
                else:
                    await _drain(asyncio.create_task(self._request.__aexit__(*cancelled)))
                raise
        except BaseException:
            if not self._raw_created:
                self._reject()
            await self._gate.__aexit__(*sys.exc_info())
            raise

    async def __aexit__(self, *exc):
        try:
            result = await self._request.__aexit__(*exc)
        except BaseException:
            await self._gate.__aexit__(*sys.exc_info())
            raise
        await self._gate.__aexit__(*exc)
        return result

    async def _await_response(self):
        try:
            async with self._gate as receipt:
                self._receipt = receipt
                fence = self._site.current_fence()
                async def open_request():
                    fence()
                    self._raw_created = True
                    return await self._create_request()
                opening = asyncio.create_task(open_request())
                try:
                    return await asyncio.shield(opening)
                except asyncio.CancelledError:
                    if not self._raw_created:
                        opening.cancel()
                    try:
                        response = await _drain(opening)
                    except BaseException:
                        pass
                    else:
                        response.close()
                    if not self._raw_created:
                        self._reject()
                    raise
                except BaseException:
                    if not self._raw_created:
                        self._reject()
                    raise
        except BaseException:
            if not self._raw_created:
                self._reject()
            raise

    def __await__(self):
        return self._await_response().__await__()


async def _drain(task):
    """Retain final mutation ownership even if the caller is cancelled twice."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    return task.result()
