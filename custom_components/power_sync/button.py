"""Button platform — exposes Powerwall pairing controls inside HA itself.

Two buttons, both attached to the Battery device card:

- ``button.power_sync_pair_powerwall_gateway`` kicks off the same pairing flow
  the PowerSync mobile app uses (RSA-4096 keypair generation → Fleet API
  ``add_authorized_client_request`` → 120s polling for VERIFIED). Persistent
  notifications guide the user through the physical DC isolator toggle and
  surface success / timeout / failure outcomes.

- ``button.power_sync_unpair_powerwall_gateway`` clears stored key material
  and the paired flag, falling all command paths back to the Fleet API.

Letting users start pairing from inside HA — without the mobile app — closes
the gap for users who never installed the app or have lost access to it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_POWERWALL_LOCAL_DIN,
    CONF_POWERWALL_LOCAL_ENERGY_SITE_ID,
    CONF_POWERWALL_LOCAL_PAIRED,
    CONF_POWERWALL_LOCAL_PAIRED_AT,
    CONF_POWERWALL_LOCAL_PRIVATE_KEY,
    CONF_POWERWALL_LOCAL_PUBLIC_KEY,
    CONF_TESLA_ENERGY_SITE_ID,
    DOMAIN,
    POWERWALL_PAIRING_WINDOW_SECONDS,
    SENSOR_FAMILY_BATTERY,
    family_device_info,
)

_LOGGER = logging.getLogger(__name__)

NOTIF_ID_PAIR_PROGRESS = "power_sync_pair_progress"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Powerwall pairing buttons (Tesla entries only)."""
    tesla_site_id = entry.options.get(
        CONF_TESLA_ENERGY_SITE_ID,
        entry.data.get(CONF_TESLA_ENERGY_SITE_ID, ""),
    )
    if not tesla_site_id:
        return

    async_add_entities(
        [
            PowerwallPairButton(hass, entry),
            PowerwallUnpairButton(hass, entry),
        ]
    )


class _PowerwallPairButtonBase(ButtonEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._attr_device_info = family_device_info(
            entry.entry_id, SENSOR_FAMILY_BATTERY
        )


class PowerwallPairButton(_PowerwallPairButtonBase):
    """Kick off Powerwall RSA key registration over Fleet API."""

    _attr_name = "Pair Powerwall Gateway"
    _attr_icon = "mdi:key-plus"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry)
        self._attr_unique_id = f"{entry.entry_id}_pair_powerwall"

    @property
    def available(self) -> bool:
        # Block the button while a pairing attempt is already in flight so
        # users don't kick off a second registration mid-toggle. The
        # PairingManager raises if start() is called twice anyway, but
        # disabling the button is a clearer signal.
        runtime = (
            self._hass.data.get(DOMAIN, {})
            .get(self._entry.entry_id, {})
            .get("powerwall_local", {})
        )
        mgr = runtime.get("pairing_manager")
        if mgr is not None and mgr.is_running:
            return False
        return True

    async def async_press(self) -> None:
        await _start_pairing_with_notifications(self._hass, self._entry)


class PowerwallUnpairButton(_PowerwallPairButtonBase):
    """Clear stored RSA key + local paired flag; commands revert to cloud.

    Two-press confirmation: the first press posts a "press again within 30s
    to confirm" notification and returns without changing state. A second
    press within the window does the actual unpair. Outside the window the
    counter resets.

    Wiping the key is recoverable (the user can re-pair via the Pair
    Powerwall Gateway button), but re-pairing requires physical access to
    the DC isolator. So a single accidental press shouldn't be enough.
    """

    _attr_name = "Unpair Powerwall Gateway"
    _attr_icon = "mdi:key-minus"

    _CONFIRM_WINDOW_SECONDS = 30

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry)
        self._attr_unique_id = f"{entry.entry_id}_unpair_powerwall"
        self._first_press_ts: float | None = None

    @property
    def available(self) -> bool:
        return bool(self._entry.data.get(CONF_POWERWALL_LOCAL_PAIRED))

    async def async_press(self) -> None:
        now = time.time()
        if (
            self._first_press_ts is None
            or (now - self._first_press_ts) > self._CONFIRM_WINDOW_SECONDS
        ):
            self._first_press_ts = now
            await _notify(
                self._hass,
                "⚠ Confirm Unpair Powerwall",
                (
                    f"Press **Unpair Powerwall Gateway** again within "
                    f"{self._CONFIRM_WINDOW_SECONDS}s to wipe the RSA key and "
                    f"revert all commands to the Tesla cloud path.\n\n"
                    f"Re-pairing requires physical access to the DC isolator "
                    f"(toggle off/on), so don't unpair unless you have it."
                ),
            )
            return
        # Second press inside the confirmation window → execute.
        self._first_press_ts = None
        await _unpair_powerwall(self._hass, self._entry)


# ---------------------------------------------------------------------------
# Helpers — duplicate the logic in powerwall_local/views.py PairStart/Unpair
# but drive persistent_notification updates instead of returning JSON to the
# mobile app. Kept private here because the views and buttons have slightly
# different feedback channels and consolidating prematurely would couple them.
# ---------------------------------------------------------------------------


async def _start_pairing_with_notifications(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Kick off ``PowerwallPairingManager`` + walk the user through the toggle."""
    # Lazy imports — these modules are heavy and only needed when the user
    # actually presses the button.
    from .powerwall_local.pairing import (
        PairingState,
        PowerwallPairingError,
        PowerwallPairingManager,
    )
    from .powerwall_local.views import (
        _get_fleet_api_context,
        _runtime,
        ensure_coordinator,
    )

    token, base, site_id = _get_fleet_api_context(hass, entry)
    if not token or not base:
        await _notify(
            hass,
            "Powerwall Pairing",
            "❌ Tesla API not configured. Finish PowerSync setup first.",
        )
        return

    runtime = _runtime(hass, entry)
    old_mgr: PowerwallPairingManager | None = runtime.get("pairing_manager")
    if old_mgr is not None and old_mgr.is_running:
        await old_mgr.cancel()

    session = async_get_clientsession(hass)

    async def _on_success(result: Any) -> None:
        updated = {
            **entry.data,
            CONF_POWERWALL_LOCAL_PAIRED: True,
            CONF_POWERWALL_LOCAL_PAIRED_AT: time.time(),
            CONF_POWERWALL_LOCAL_PRIVATE_KEY: result.private_key_pem.decode(),
            CONF_POWERWALL_LOCAL_PUBLIC_KEY: result.public_key_der.hex(),
            CONF_POWERWALL_LOCAL_DIN: result.din,
            CONF_POWERWALL_LOCAL_ENERGY_SITE_ID: result.energy_site_id,
        }
        hass.config_entries.async_update_entry(entry, data=updated)
        await ensure_coordinator(hass, entry)

    mgr = PowerwallPairingManager(
        session,
        base,
        token,
        energy_site_id=site_id,
        window_seconds=POWERWALL_PAIRING_WINDOW_SECONDS,
        on_success=_on_success,
    )
    runtime["pairing_manager"] = mgr

    try:
        await mgr.start()
    except PowerwallPairingError as err:
        _LOGGER.exception("Pairing start failed")
        await _notify(hass, "❌ Pairing failed to start", str(err))
        return

    await _notify(
        hass,
        "Powerwall Pairing",
        "Generating RSA key and registering with Tesla Fleet API…",
    )

    hass.async_create_task(_watch_pairing(hass, mgr))


async def _watch_pairing(hass: HomeAssistant, mgr: Any) -> None:
    """Poll the manager and post a notification at each state transition."""
    from .powerwall_local.pairing import PairingState

    last_state = None
    while mgr.is_running:
        status = mgr.status()
        if status.state != last_state:
            last_state = status.state
            if status.state == PairingState.WAITING_FOR_TOGGLE:
                remaining = max(0, int((status.expires_at or 0) - time.time()))
                await _notify(
                    hass,
                    "Powerwall Pairing — toggle the DC isolator now",
                    (
                        f"⚡ Toggle your Powerwall DC isolator **OFF**, wait 10 seconds, "
                        f"then **ON** again.\n\n"
                        f"You have {remaining}s to complete the toggle. This proves "
                        f"physical possession of the gateway and authorizes the RSA "
                        f"key for direct LAN control."
                    ),
                )
        await asyncio.sleep(2)

    final = mgr.status()
    if final.state == PairingState.VERIFIED:
        await _notify(
            hass,
            "✓ Powerwall Paired",
            (
                f"Local LAN control enabled for gateway `{final.din}`.\n"
                f"Backup reserve, operation mode, and grid export rule now write "
                f"directly to the gateway. No cloud round-trip."
            ),
        )
    elif final.state == PairingState.TIMEOUT:
        await _notify(
            hass,
            "⏱ Pairing Window Expired",
            (
                "The 2-minute pairing window expired before the DC isolator was "
                "toggled. Press **Pair Powerwall Gateway** to try again."
            ),
        )
    elif final.state == PairingState.FAILED:
        await _notify(
            hass,
            "❌ Pairing Failed",
            f"Tesla rejected the pairing request: {final.error or 'unknown error'}.",
        )
    elif final.state == PairingState.CANCELLED:
        await _notify(hass, "Pairing Cancelled", "Pairing was cancelled.")


async def _unpair_powerwall(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clear key material and paired flag — mirrors PowerwallPairUnpairView."""
    from .powerwall_local.views import _runtime

    runtime = _runtime(hass, entry)
    mgr = runtime.get("pairing_manager")
    if mgr is not None and mgr.is_running:
        await mgr.cancel()

    new_data = {**entry.data}
    for key in (
        CONF_POWERWALL_LOCAL_PAIRED,
        CONF_POWERWALL_LOCAL_PRIVATE_KEY,
        CONF_POWERWALL_LOCAL_PUBLIC_KEY,
        CONF_POWERWALL_LOCAL_DIN,
        CONF_POWERWALL_LOCAL_ENERGY_SITE_ID,
        CONF_POWERWALL_LOCAL_PAIRED_AT,
    ):
        new_data.pop(key, None)
    hass.config_entries.async_update_entry(entry, data=new_data)

    runtime["client"] = None
    coordinator = runtime.get("coordinator")
    if coordinator is not None:
        coordinator.update_interval = None
    runtime["coordinator"] = None
    runtime["pairing_manager"] = None

    await _notify(
        hass,
        "Powerwall Unpaired",
        "Local LAN control disabled. Commands will route via Tesla cloud.",
    )


async def _notify(hass: HomeAssistant, title: str, message: str) -> None:
    """Update the single pair-progress persistent notification."""
    await hass.services.async_call(
        "persistent_notification",
        "create",
        {
            "notification_id": NOTIF_ID_PAIR_PROGRESS,
            "title": title,
            "message": message,
        },
        blocking=False,
    )
