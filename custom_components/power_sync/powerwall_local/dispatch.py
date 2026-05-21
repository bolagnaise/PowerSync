"""Local-first / cloud-fallback dispatcher for paired Powerwall sites.

Service handlers in ``__init__.py`` call ``dispatch_powerwall_write`` instead
of issuing the Tesla Fleet API POST directly. When the entry is paired and the
signed transport is reachable, the local V1R path runs first; on any failure
(timeout, signature reject, network), the cloud path runs as fallback.

Unpaired entries short-circuit to cloud — the dispatcher is a no-op overhead
of one dict lookup per call.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from ..const import CONF_POWERWALL_LOCAL_IP, CONF_POWERWALL_LOCAL_PAIRED, DOMAIN
from .exceptions import (
    PowerwallAuthError,
    PowerwallLocalError,
    PowerwallSignatureError,
    PowerwallUnreachableError,
)
from .transport import TEDAPIv1rTransport

_LOGGER = logging.getLogger(__name__)

_RUNTIME_KEY = "powerwall_local"

LocalCall = Callable[[TEDAPIv1rTransport], Awaitable[Any]]
CloudCall = Callable[[], Awaitable[Any]]

# Exceptions we treat as "local failed, try cloud" rather than re-raising.
_LOCAL_FALLBACK_EXC = (
    PowerwallUnreachableError,
    PowerwallSignatureError,
    PowerwallAuthError,
    PowerwallLocalError,
    asyncio.TimeoutError,
)


def is_local_preferred(entry: ConfigEntry) -> bool:
    """True when the entry is paired — local is the preferred path."""
    return entry.data.get(CONF_POWERWALL_LOCAL_PAIRED) is True


def has_local_gateway_ip(entry: ConfigEntry) -> bool:
    """True when the entry has a non-empty gateway LAN address configured."""
    return bool(str(entry.data.get(CONF_POWERWALL_LOCAL_IP) or "").strip())


def get_local_transport(
    hass: HomeAssistant, entry: ConfigEntry
) -> TEDAPIv1rTransport | None:
    """Return the live signed transport for a paired entry, or None.

    None means: not paired, no gateway LAN IP, transport not yet built
    (coordinator still starting up), or PW2 (no signed transport). Callers
    should treat None as "fall through to cloud".
    """
    if not is_local_preferred(entry) or not has_local_gateway_ip(entry):
        return None
    bucket = (
        hass.data.get(DOMAIN, {})
        .get(entry.entry_id, {})
        .get(_RUNTIME_KEY, {})
    )
    client = bucket.get("client")
    if client is None:
        return None
    transport = getattr(client, "_transport", None)
    if not isinstance(transport, TEDAPIv1rTransport):
        return None
    client_local_enabled = getattr(client, "local_access_enabled", True)
    if client_local_enabled is False:
        return None
    return transport


async def dispatch_powerwall_write(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    local_call: LocalCall,
    cloud_call: CloudCall,
    label: str,
    timeout: float = 5.0,
    retry_local_once: bool = True,
) -> Any:
    """Run ``local_call`` if paired & reachable; else (or on failure) ``cloud_call``.

    ``local_call(transport)`` must be an async callable that returns a truthy
    value on success. A falsy return (e.g. ``write_config`` returning False on
    optimistic-lock hash mismatch) is treated as a recoverable miss and either
    retried once or fed into the cloud fallback.

    ``cloud_call()`` is the existing Tesla Fleet API path — it's untouched and
    runs as the safety net.
    """
    transport = get_local_transport(hass, entry)
    if transport is not None:
        for attempt in (1, 2) if retry_local_once else (1,):
            t0 = time.monotonic()
            try:
                result = await asyncio.wait_for(local_call(transport), timeout=timeout)
            except _LOCAL_FALLBACK_EXC as err:
                _LOGGER.warning(
                    "[powerwall_cmd] %s local failed attempt %d (%s) in %.0fms — %s",
                    label,
                    attempt,
                    err.__class__.__name__,
                    (time.monotonic() - t0) * 1000.0,
                    "retrying" if attempt == 1 and retry_local_once else "falling back to cloud",
                )
                continue
            except Exception:  # pragma: no cover — unexpected
                _LOGGER.exception(
                    "[powerwall_cmd] %s local raised unexpected error — falling back to cloud",
                    label,
                )
                break

            if result:
                _LOGGER.info(
                    "[powerwall_cmd] %s via local in %.0fms (attempt %d)",
                    label,
                    (time.monotonic() - t0) * 1000.0,
                    attempt,
                )
                return result

            _LOGGER.warning(
                "[powerwall_cmd] %s local returned falsy on attempt %d in %.0fms — %s",
                label,
                attempt,
                (time.monotonic() - t0) * 1000.0,
                "retrying" if attempt == 1 and retry_local_once else "falling back to cloud",
            )

    t0 = time.monotonic()
    result = await cloud_call()
    _LOGGER.info(
        "[powerwall_cmd] %s via cloud in %.0fms",
        label,
        (time.monotonic() - t0) * 1000.0,
    )
    return result
