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
from ..powerwall_host import normalize_powerwall_gateway_host
from .exceptions import (
    PowerwallAuthError,
    PowerwallLocalError,
    PowerwallSignatureError,
    PowerwallUnreachableError,
    PowerwallWriteRejected,
)
from .transport import TEDAPIv1rTransport

_LOGGER = logging.getLogger(__name__)

_RUNTIME_KEY = "powerwall_local"

# Mirrors powerwall_local/coordinator.py's _CLOUD_FALLBACK_PENDING_KEY. Set
# here (the write side) when a local attempt failed but the cloud fallback
# succeeded; consumed there (the poll side) to skip re-stamping the next
# local snapshot as fresh, since that poll re-fetches the gateway's
# still-stale (unwritten) local state (PW-4).
_CLOUD_FALLBACK_PENDING_KEY = "powerwall_local_cloud_fallback_pending"

LocalCall = Callable[[TEDAPIv1rTransport], Awaitable[Any]]
CloudCall = Callable[[], Awaitable[Any]]


class _SettingsPolicyDenied(Exception):
    """The current owner lost permission; this is not caller cancellation."""


async def write_config_confirmed(
    transport, din: str, updates: dict[str, Any], *,
    is_current: Callable[[], bool], deadline: float,
    acknowledged: Callable[[], None] | None = None, before_submit=None,
    completed=None, submissions=None, is_allowed=None,
) -> bool:
    """Submit once and confirm exact fields; retain an in-flight submission."""
    def check_current():
        if not is_current():
            raise asyncio.CancelledError("Powerwall settings request superseded")
        if is_allowed is not None and not is_allowed():
            raise _SettingsPolicyDenied
        if time.monotonic() >= deadline:
            raise asyncio.TimeoutError

    did_submit = False
    def before_write():
        nonlocal did_submit
        check_current()
        did_submit = True

    async def stage(expires_at):
        check_current()
        if before_submit is not None:
            await before_submit(expires_at)

    async def write():
        try:
            result = await transport.write_config(
                din, updates, before_write=before_write, before_submit=stage,
            )
        except BaseException as err:
            if completed is not None:
                await completed(False, did_submit, err)
            raise
        if completed is not None:
            await completed(result, did_submit, None)
        return result

    if time.monotonic() >= deadline:
        return False
    check_current()
    if submissions is None:
        result = await write()
    else:
        # The request's local slice may end before the HTTP response arrives.
        # Keep the actual RPC owned/tracked, so unload can drain it and its
        # exact update response can retire the pending submission safely.
        task = asyncio.create_task(write())
        submissions.add(task)
        def finished(done):
            submissions.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)
        result = await asyncio.shield(task)
    if not result:
        return False
    if acknowledged is not None:
        acknowledged()
    check_current()
    accepted_at = time.monotonic()
    for offset in (0.0, 0.5, 1.5):
        delay = min(accepted_at + offset, deadline) - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        if time.monotonic() >= deadline:
            return False
        check_current()
        config = await transport.read_config(din)
        check_current()
        for dotted, expected in updates.items():
            observed = config
            for key in dotted.split("."):
                observed = observed.get(key) if isinstance(observed, dict) else None
            if type(observed) is not type(expected) or observed != expected:
                break
        else:
            return True
    return False


async def dispatch_powerwall_setting(
    hass, entry, *, site_id: str, updates: dict[str, Any],
    cloud_call: CloudCall, is_current: Callable[[], bool], deadline: float,
    acknowledged=None, before_submit=None, prefer_local=True,
    guard_write=None, operation_id=None, is_allowed=None, allow_stopping=False, label: str,
    cloud_observation_confirmed=True, skip_matching=False,
) -> bool:
    """One bounded optional settings transaction, separate from watt control."""
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    lock = runtime.setdefault("powerwall_settings_lock", asyncio.Lock())
    tasks = runtime.setdefault("powerwall_settings_tasks", set())
    submissions = runtime.setdefault("powerwall_settings_submissions", set())
    task = asyncio.current_task()
    tasks.add(task)
    acquired = False
    epoch = runtime.get("powerwall_settings_epoch", 0)

    def current():
        pending = runtime.get("powerwall_settings_pending")
        return (hass.data.get(DOMAIN, {}).get(entry.entry_id) is runtime
                and runtime.get("powerwall_settings_lock") is lock
                and runtime.get("powerwall_settings_epoch", 0) == epoch
                and not runtime.get("powerwall_settings_recovering")
                and (not pending or pending.get("resume_cloud") is not True
                     or runtime.get("powerwall_settings_cloud_handoff") == pending.get("id"))
                and (allow_stopping or not runtime.get("powerwall_local_dispatch_stopping")) and is_current())
    def check_current():
        if not current():
            raise asyncio.CancelledError("Powerwall settings request superseded")
    def allowed():
        return is_allowed is None or is_allowed()
    def remaining():
        return max(0.0, deadline - time.monotonic())

    try:
        check_current()
        if not allowed() or not remaining():
            return False
        try:
            await asyncio.wait_for(lock.acquire(), remaining())
            acquired = True
            check_current()
            active = tuple(t for t in submissions if not t.done())
            pending = runtime.get("powerwall_settings_pending")
            affected = not pending or pending.get("site_id") == str(site_id)
            if active and affected and not (pending and pending.get("resume_cloud") is True):
                await asyncio.wait_for(asyncio.gather(
                    *(asyncio.shield(t) for t in active), return_exceptions=True), remaining())
        except asyncio.TimeoutError:
            return False
        check_current()
        pending = runtime.get("powerwall_settings_pending")
        if pending and pending.get("site_id") == str(site_id) and pending.get("resume_cloud") is not True:
            # Expiry is an acceptance deadline, not a FileStore completion
            # guarantee. No contrary/new transaction can retire this record.
            _LOGGER.warning("%s blocked: previous signed settings update remains unresolved", label)
            return False
        if not allowed() or not remaining():
            return False
        din = entry.data.get("powerwall_local_din")
        transport = (get_local_transport(hass, entry)
                     if not pending and prefer_local and site_id is not None
                     and str(site_id) == str(entry.data.get("tesla_energy_site_id"))
                     and (operation_id is None or runtime.get("powerwall_settings_local_failed") != operation_id) else None)
        record = None
        async def staged(expires_at):
            nonlocal record
            import uuid
            record = {"id": uuid.uuid4().hex, "expires_at": expires_at,
                      "site_id": str(site_id), "updates": dict(updates)}
            if before_submit is not None:
                await before_submit(record)
            check_current()
            runtime["powerwall_settings_pending"] = record

        async def completed(ok, submitted, error):
            import aiohttp
            definitely_finished = (ok or not submitted
                or isinstance(error, (PowerwallSignatureError, PowerwallAuthError, PowerwallWriteRejected))
                or isinstance(getattr(error, "__cause__", None), aiohttp.ClientConnectorError))
            if record is not None:
                if definitely_finished:
                    saved = None
                    if before_submit is not None:
                        saved = await before_submit(None, previous=record)
                    active_runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
                    latest = active_runtime.get("powerwall_settings_pending")
                    if latest and latest.get("id") == record["id"]:
                        active_runtime["powerwall_settings_pending"] = saved
                else:
                    _LOGGER.warning("%s signed update outcome unresolved; recovery retained", label)

        confirmed = False
        primary_site = str(site_id) == str(entry.data.get("tesla_energy_site_id"))
        if primary_site:
            _record_setting_observation(runtime, updates, "unknown")
        if transport is not None and din:
            try:
                local_deadline = min(deadline, time.monotonic() + 5.0)
                async def local():
                    if skip_matching:
                        check_current()
                        if not allowed():
                            return False
                        config = await transport.read_config(din)
                        check_current()
                        if not allowed() or time.monotonic() >= local_deadline:
                            return False
                        for dotted, expected in updates.items():
                            observed = config
                            for key in dotted.split("."):
                                observed = observed.get(key) if isinstance(observed, dict) else None
                            if type(observed) is not type(expected) or observed != expected:
                                break
                        else:
                            return True
                    return await write_config_confirmed(
                        transport, din, updates, is_current=current, is_allowed=allowed,
                        deadline=local_deadline, acknowledged=acknowledged,
                        before_submit=staged, completed=completed, submissions=submissions,
                    )
                confirmed = await asyncio.wait_for(
                    guard_write(local) if guard_write else local(),
                    max(0.0, local_deadline - time.monotonic()),
                )
            except _SettingsPolicyDenied:
                return False
            except _LOCAL_FALLBACK_EXC as err:
                if operation_id is not None:
                    runtime["powerwall_settings_local_failed"] = operation_id
                _LOGGER.debug("%s local settings unconfirmed: %s", label, err)
        check_current()
        source = "local" if confirmed else "cloud"
        if not confirmed:
            if not allowed() or not remaining():
                return False
            # The same owner may use its same-target cloud fallback. Any
            # ambiguous local RPC remains a separate recovery obligation.
            try:
                confirmed = bool(await asyncio.wait_for(cloud_call(), remaining()))
            except asyncio.TimeoutError:
                confirmed = False
            check_current()
        if not allowed():
            return False
        if confirmed and primary_site:
            _record_setting_observation(
                runtime, updates,
                source if source == "local" or cloud_observation_confirmed else "unknown",
            )
        return confirmed
    finally:
        if acquired:
            lock.release()
        tasks.discard(task)


def _record_setting_observation(runtime, updates, source):
    """Fence old local polls using per-setting evidence, not meter timestamps."""
    fields = {"default_real_mode": "operation_mode",
              "site_info.disallow_charge_from_grid_with_solar_installed": "grid_charging_enabled",
              "site_info.customer_preferred_export_rule": "grid_export_rule"}
    records = runtime.setdefault("powerwall_setting_observations", {})
    coordinator = (runtime.get("powerwall_local") or {}).get("coordinator")
    snapshot = getattr(coordinator, "data", None)
    for dotted, value in updates.items():
        attribute = fields[dotted]
        if attribute == "grid_charging_enabled":
            value = not value
        records[attribute] = {"value": value, "source": source,
                              "at": time.monotonic()}
        if snapshot is not None:
            setattr(snapshot, attribute, value if source == "local" else None)

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
    try:
        return bool(
            normalize_powerwall_gateway_host(
                entry.data.get(CONF_POWERWALL_LOCAL_IP)
            )
        )
    except ValueError:
        return False


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
    if transport is not None and result:
        entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if isinstance(entry_data, dict):
            entry_data[_CLOUD_FALLBACK_PENDING_KEY] = True
    return result
