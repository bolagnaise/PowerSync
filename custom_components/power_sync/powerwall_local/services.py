"""Home Assistant services wrapping local Powerwall control.

Services:
    power_sync.powerwall_go_off_grid   - disconnect from grid (islanding)
    power_sync.powerwall_reconnect_grid - reconnect to grid

Both services look up the PowerSync config entry, check the paired state,
enforce the SOC safety floor, and dispatch to ``PowerwallLocalClient``.
Callable from automations, the dashboard, and the LP optimizer.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import time
import uuid
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from ..const import (
    CONF_POWERWALL_LOCAL_PAIRED,
    CONF_MONITORING_MODE,
    CONF_POWERWALL_OFF_GRID_MIN_SOC,
    DEFAULT_POWERWALL_OFF_GRID_MIN_SOC,
    DOMAIN,
)
from .views import _get_entry, ensure_client, ensure_coordinator
from ..battery_backend.profiles import resolve_connection_profile
from .calibration_delay import validate_delay_hours
from .exceptions import PowerwallLocalError

if TYPE_CHECKING:
    pass

_LOGGER = logging.getLogger(__name__)

# This lock deliberately lives outside HA's per-entry runtime dictionary.  A
# config-entry reload replaces that dictionary while the old request may still
# be in flight; using the runtime object would permit two allowance-consuming
# requests to overlap.
_CALIBRATION_DELAY_LOCKS: dict[str, asyncio.Lock] = {}
_CALIBRATION_DELAY_ACCEPTANCE_WINDOW_SECONDS = 60
_CALIBRATION_DELAY_SIGNATURE_SAFETY_SECONDS = 13
_CALIBRATION_DELAY_JOURNAL_LIMIT = 64
_CALIBRATION_DELAY_BARRIER_KEY = "__acceptance_barrier_until"


async def _durable_calibration_save(storage: Store, requests: dict) -> None:
    """Finish an executor-backed Store save before propagating cancellation."""
    task = asyncio.create_task(storage.async_save(copy.deepcopy(requests)))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Consume cancellation while the executor write finishes.  A
            # second cancellation must not release the lock with the intent
            # save still running.
            cancelled = True
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
    task.result()
    if cancelled:
        raise asyncio.CancelledError


def _prune_calibration_journal(requests: dict) -> bool:
    """Prune terminal records only; return whether another record can fit."""
    record_keys = [key for key in requests if key != _CALIBRATION_DELAY_BARRIER_KEY]
    while len(record_keys) > _CALIBRATION_DELAY_JOURNAL_LIMIT:
        removable = next(
            (key for key in record_keys if requests.get(key, {}).get("terminal")),
            None,
        )
        if removable is None:
            return False
        requests.pop(removable, None)
        record_keys.remove(removable)
    return True

SERVICE_GO_OFF_GRID = "powerwall_go_off_grid"
SERVICE_RECONNECT_GRID = "powerwall_reconnect_grid"
SERVICE_VERIFY_PAIRING = "powerwall_verify_pairing"
SERVICE_ON_GRID_SOLAR_CURTAILMENT = "powerwall_set_on_grid_solar_curtailment"
SERVICE_CALIBRATION_DELAY_STATUS = "powerwall_get_battery_calibration_delay_status"
SERVICE_DELAY_CALIBRATION = "powerwall_delay_battery_calibration"

GO_OFF_GRID_SCHEMA = vol.Schema(
    {
        vol.Optional("bypass_soc_check", default=False): bool,
        vol.Optional("mode"): vol.All(int, vol.Range(min=1, max=10)),
    }
)

RECONNECT_SCHEMA = vol.Schema({})
ON_GRID_SOLAR_CURTAILMENT_SCHEMA = vol.Schema({
    vol.Required("enabled"): bool,
    vol.Optional("entry_id"): str,
})


def _service_delay_hours(value):
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        value = int(value)
    return validate_delay_hours(value)


CALIBRATION_DELAY_SCHEMA = vol.Schema({
    vol.Required("delay_hours"): _service_delay_hours,
    vol.Optional("entry_id"): str,
    vol.Optional("request_id"): str,
})


async def _paired_client_for_call(hass: HomeAssistant, call: ServiceCall, *, control: bool = True):
    """Acquire a client and an authority guard valid through the network write."""
    entry_id = call.data.get("entry_id")
    if entry_id:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is not None and entry.domain != DOMAIN:
            entry = None
    else:
        entries = hass.config_entries.async_entries(DOMAIN)
        if len(entries) != 1:
            raise HomeAssistantError("Specify entry_id when more than one PowerSync entry is configured")
        entry = entries[0]
    if entry is None or not entry.data.get(CONF_POWERWALL_LOCAL_PAIRED):
        raise HomeAssistantError("A paired Powerwall entry is required")
    identity_keys = ("powerwall_local_paired", "powerwall_local_ip", "powerwall_local_din",
        "powerwall_local_private_key_pem", "powerwall_local_public_key_der")
    identity = tuple(entry.data.get(key) for key in identity_keys)
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not isinstance(runtime, dict):
        raise HomeAssistantError("PowerSync entry is not loaded")

    def guard() -> None:
        current = hass.config_entries.async_get_entry(entry.entry_id)
        if current is not entry or tuple(current.data.get(key) for key in identity_keys) != identity:
            raise HomeAssistantError("Powerwall pairing or entry changed; retry with the current configuration")
        if control and resolve_connection_profile(current.data, current.options).monitoring_only:
            raise HomeAssistantError("This battery connection permits monitoring only")
        monitoring = current.options.get(CONF_MONITORING_MODE, current.data.get(CONF_MONITORING_MODE, False))
        if control and monitoring and not getattr(call.context, "user_id", None):
            raise HomeAssistantError("Monitoring Mode blocks automated Powerwall setting changes")
        current_runtime = hass.data.get(DOMAIN, {}).get(current.entry_id)
        if current_runtime is not runtime or runtime.get("powerwall_local_dispatch_stopping"):
            raise HomeAssistantError("PowerSync is stopping; retry after setup completes")

    guard()
    coordinator = await ensure_coordinator(hass, entry)
    client = coordinator.client if coordinator is not None else await ensure_client(hass, entry)
    if client is None or not getattr(client, "local_access_enabled", False):
        raise HomeAssistantError("Powerwall LAN control is unavailable; this setting has no Fleet fallback")
    guard()
    return entry, coordinator, client, guard


async def _handle_on_grid_solar_curtailment(hass: HomeAssistant, call: ServiceCall) -> None:
    """Change the native permission without changing export policy or mode."""
    entry, coordinator, client, guard = await _paired_client_for_call(hass, call)
    if not await client.set_on_grid_solar_curtailment(call.data["enabled"], before_write=guard):
        raise HomeAssistantError("Gateway readback did not confirm the on-grid solar curtailment setting")
    if coordinator is not None:
        await coordinator.async_request_refresh()


async def _handle_calibration_delay_status(hass: HomeAssistant, call: ServiceCall) -> dict:
    _, _, client, _ = await _paired_client_for_call(hass, call, control=False)
    return await client.get_battery_calibration_delay_status()


async def _handle_delay_calibration(hass: HomeAssistant, call: ServiceCall) -> dict:
    hours = _service_delay_hours(call.data["delay_hours"])
    entry, _, client, guard = await _paired_client_for_call(hass, call)
    lock = _CALIBRATION_DELAY_LOCKS.setdefault(entry.entry_id, asyncio.Lock())
    if lock.locked():
        raise HomeAssistantError("A calibration delay request is already in progress")
    async with lock:
        guard()
        request_id = call.data["request_id"] if "request_id" in call.data else getattr(call.context, "id", None) or uuid.uuid4().hex
        if (not isinstance(request_id, str) or request_id == _CALIBRATION_DELAY_BARRIER_KEY
                or not 1 <= len(request_id) <= 80
                or not all(c.isascii() and (c.isalnum() or c in "-_") for c in request_id)):
            raise HomeAssistantError("Invalid calibration delay request_id")
        storage = Store(hass, 1, f"power_sync_calibration_delay_requests_{entry.entry_id}")
        loaded_requests = await storage.async_load()
        if loaded_requests is None:
            requests = {}
        elif not isinstance(loaded_requests, dict):
            raise HomeAssistantError("Calibration request journal is invalid; refusing to replay")
        else:
            requests = loaded_requests
        pairing = hashlib.sha256(str((entry.data.get("powerwall_local_din"), entry.data.get("powerwall_local_public_key_der"))).encode()).hexdigest()
        previous = requests.get(request_id)
        if previous:
            if previous.get("hours") != hours or previous.get("pairing") != pairing:
                raise HomeAssistantError("Calibration request ID was already used for a different request")
            if previous.get("result") is not None:
                return {**previous["result"], "replayed": True}
            raise HomeAssistantError("Previous calibration request is unconfirmed; refresh status instead of replaying it")
        barrier_until = requests.get(_CALIBRATION_DELAY_BARRIER_KEY, 0)
        if isinstance(barrier_until, (int, float)) and barrier_until > time.time():
            raise HomeAssistantError("A previous calibration delay write is still awaiting gateway acceptance")

        requests[request_id] = {"hours": hours, "pairing": pairing}
        if not _prune_calibration_journal(requests):
            requests.pop(request_id, None)
            raise HomeAssistantError("Calibration request journal is full of unconfirmed requests")

        acceptance_until: float | None = None

        def before_send_guard() -> None:
            guard()
            if acceptance_until is not None and time.time() + _CALIBRATION_DELAY_SIGNATURE_SAFETY_SECONDS > acceptance_until:
                raise PowerwallLocalError("Calibration delay acceptance window expired before send")

        async def persist_intent():
            nonlocal acceptance_until
            guard()
            # Persist the acceptance fence before signing/session setup.  If
            # cancellation lands after the gateway POST starts, a new request
            # cannot race the original signed message during its TTL.
            acceptance_until = time.time() + _CALIBRATION_DELAY_ACCEPTANCE_WINDOW_SECONDS
            requests[_CALIBRATION_DELAY_BARRIER_KEY] = acceptance_until
            await _durable_calibration_save(storage, requests)
            guard()

        try:
            result = await client.delay_battery_calibration(hours, before_write=before_send_guard, before_command=persist_intent)
        except PowerwallLocalError as error:
            if "no calibration deferrals remaining" in str(error).lower():
                requests[request_id]["terminal"] = "rejected_full"
                await _durable_calibration_save(storage, requests)
            raise HomeAssistantError(str(error)) from error
        requests[request_id]["result"] = result
        if result.get("command_acknowledged"):
            requests[request_id]["terminal"] = "completed"
            requests.pop(_CALIBRATION_DELAY_BARRIER_KEY, None)
        else:
            # A signed request can reach the gateway after the local transport
            # times out.  Keep the tombstone and block a fresh write during the
            # signature acceptance window; never infer retry safety.
            requests[_CALIBRATION_DELAY_BARRIER_KEY] = time.time() + _CALIBRATION_DELAY_ACCEPTANCE_WINDOW_SECONDS
        await _durable_calibration_save(storage, requests)
    # The result intentionally distinguishes typed acceptance from status.
    # Never clear PowerSync's calibration alert/guard or replay an uncertain request.
    return result


async def _handle_go_off_grid(hass: HomeAssistant, call: ServiceCall) -> None:
    entry = _get_entry(hass)
    if entry is None:
        raise HomeAssistantError("PowerSync not configured")
    if not entry.data.get(CONF_POWERWALL_LOCAL_PAIRED):
        raise HomeAssistantError("Powerwall not paired for local control")

    coordinator = await ensure_coordinator(hass, entry)
    client = (
        coordinator.client if coordinator is not None else await ensure_client(hass, entry)
    )
    if client is None:
        raise HomeAssistantError("Powerwall local client unavailable")

    bypass = bool(call.data.get("bypass_soc_check"))
    if not bypass:
        min_soc = int(
            entry.data.get(
                CONF_POWERWALL_OFF_GRID_MIN_SOC,
                DEFAULT_POWERWALL_OFF_GRID_MIN_SOC,
            )
        )
        snap = coordinator.data if coordinator is not None else None
        if snap is not None and snap.soc is not None and snap.soc < min_soc:
            raise HomeAssistantError(
                f"Refusing off-grid: SOC {snap.soc:.0f}% < floor {min_soc}%"
            )

    mode_override = call.data.get("mode")
    ok = await client.go_off_grid(mode_override=mode_override)
    if coordinator is not None:
        await coordinator.async_request_refresh()
    if not ok:
        raise HomeAssistantError(
            "Gateway rejected islanding command — check logs for details"
        )


async def _handle_reconnect_grid(hass: HomeAssistant, call: ServiceCall) -> None:
    entry = _get_entry(hass)
    if entry is None:
        raise HomeAssistantError("PowerSync not configured")
    if not entry.data.get(CONF_POWERWALL_LOCAL_PAIRED):
        raise HomeAssistantError("Powerwall not paired for local control")
    coordinator = await ensure_coordinator(hass, entry)
    client = (
        coordinator.client if coordinator is not None else await ensure_client(hass, entry)
    )
    if client is None:
        raise HomeAssistantError("Powerwall local client unavailable")
    ok = await client.reconnect_grid()
    if coordinator is not None:
        await coordinator.async_request_refresh()
    if not ok:
        raise HomeAssistantError("Gateway rejected reconnect command")


async def _handle_verify_pairing(hass: HomeAssistant, call: ServiceCall) -> None:
    entry = _get_entry(hass)
    if entry is None:
        raise HomeAssistantError("PowerSync not configured")

    coordinator = await ensure_coordinator(hass, entry)
    if coordinator is None or coordinator.client is None:
        raise HomeAssistantError("Powerwall local client unavailable")

    state = await coordinator.client.verify_pairing()
    state_names = {1: "pending", 2: "pending_verification", 3: "verified"}
    if state is None:
        raise HomeAssistantError("Could not determine pairing state")
    if state != 3:
        raise HomeAssistantError(
            f"Pairing key state={state} ({state_names.get(state, 'unknown')}). "
            "Toggle the DC isolator to complete verification."
        )
    _LOGGER.info("verify_pairing: key is verified (state=3)")


@callback
def register_services(hass: HomeAssistant) -> None:
    """Register the off-grid / reconnect / verify services (idempotent)."""

    async def go_off_grid(call: ServiceCall) -> None:
        await _handle_go_off_grid(hass, call)

    async def reconnect_grid(call: ServiceCall) -> None:
        await _handle_reconnect_grid(hass, call)

    async def verify_pairing(call: ServiceCall) -> None:
        await _handle_verify_pairing(hass, call)

    async def on_grid_solar_curtailment(call: ServiceCall) -> None:
        await _handle_on_grid_solar_curtailment(hass, call)

    async def calibration_delay_status(call: ServiceCall) -> dict:
        return await _handle_calibration_delay_status(hass, call)

    async def delay_calibration(call: ServiceCall) -> dict:
        return await _handle_delay_calibration(hass, call)

    if not hass.services.has_service(DOMAIN, SERVICE_CALIBRATION_DELAY_STATUS):
        hass.services.async_register(DOMAIN, SERVICE_CALIBRATION_DELAY_STATUS,
            calibration_delay_status, schema=vol.Schema({vol.Optional("entry_id"): str}),
            supports_response=SupportsResponse.ONLY)
    if not hass.services.has_service(DOMAIN, SERVICE_DELAY_CALIBRATION):
        hass.services.async_register(DOMAIN, SERVICE_DELAY_CALIBRATION, delay_calibration,
            schema=CALIBRATION_DELAY_SCHEMA, supports_response=SupportsResponse.OPTIONAL)

    if not hass.services.has_service(DOMAIN, SERVICE_ON_GRID_SOLAR_CURTAILMENT):
        hass.services.async_register(
            DOMAIN, SERVICE_ON_GRID_SOLAR_CURTAILMENT, on_grid_solar_curtailment,
            schema=ON_GRID_SOLAR_CURTAILMENT_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_GO_OFF_GRID):
        hass.services.async_register(
            DOMAIN, SERVICE_GO_OFF_GRID, go_off_grid, schema=GO_OFF_GRID_SCHEMA
        )
    if not hass.services.has_service(DOMAIN, SERVICE_RECONNECT_GRID):
        hass.services.async_register(
            DOMAIN,
            SERVICE_RECONNECT_GRID,
            reconnect_grid,
            schema=RECONNECT_SCHEMA,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_VERIFY_PAIRING):
        hass.services.async_register(
            DOMAIN,
            SERVICE_VERIFY_PAIRING,
            verify_pairing,
            schema=vol.Schema({}),
        )
