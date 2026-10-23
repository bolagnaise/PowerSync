"""Home Assistant services wrapping local Powerwall control.

Services:
    power_sync.powerwall_go_off_grid   - disconnect from grid (islanding)
    power_sync.powerwall_reconnect_grid - reconnect to grid

Both services look up the PowerSync config entry, check the paired state,
enforce the SOC safety floor, and dispatch to ``PowerwallLocalClient``.
Callable from automations, the dashboard, and the LP optimizer.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError

from ..const import (
    CONF_POWERWALL_LOCAL_PAIRED,
    CONF_MONITORING_MODE,
    CONF_POWERWALL_OFF_GRID_MIN_SOC,
    DEFAULT_POWERWALL_OFF_GRID_MIN_SOC,
    DOMAIN,
)
from .views import _get_entry, ensure_client, ensure_coordinator
from ..battery_backend.profiles import resolve_connection_profile

if TYPE_CHECKING:
    pass

_LOGGER = logging.getLogger(__name__)

SERVICE_GO_OFF_GRID = "powerwall_go_off_grid"
SERVICE_RECONNECT_GRID = "powerwall_reconnect_grid"
SERVICE_VERIFY_PAIRING = "powerwall_verify_pairing"
SERVICE_ON_GRID_SOLAR_CURTAILMENT = "powerwall_set_on_grid_solar_curtailment"

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


async def _handle_on_grid_solar_curtailment(hass: HomeAssistant, call: ServiceCall) -> None:
    """Change the native permission without changing export policy or mode."""
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
        if resolve_connection_profile(current.data, current.options).monitoring_only:
            raise HomeAssistantError("This battery connection permits monitoring only")
        monitoring = current.options.get(CONF_MONITORING_MODE, current.data.get(CONF_MONITORING_MODE, False))
        if monitoring and not getattr(call.context, "user_id", None):
            raise HomeAssistantError("Monitoring Mode blocks automated on-grid solar curtailment changes")
        current_runtime = hass.data.get(DOMAIN, {}).get(current.entry_id)
        if current_runtime is not runtime or runtime.get("powerwall_local_dispatch_stopping"):
            raise HomeAssistantError("PowerSync is stopping; retry after setup completes")

    guard()
    coordinator = await ensure_coordinator(hass, entry)
    client = coordinator.client if coordinator is not None else await ensure_client(hass, entry)
    if client is None or not getattr(client, "local_access_enabled", False):
        raise HomeAssistantError("Powerwall LAN control is unavailable; this setting has no Fleet fallback")
    guard()
    if not await client.set_on_grid_solar_curtailment(call.data["enabled"], before_write=guard):
        raise HomeAssistantError("Gateway readback did not confirm the on-grid solar curtailment setting")
    if coordinator is not None:
        await coordinator.async_request_refresh()


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
