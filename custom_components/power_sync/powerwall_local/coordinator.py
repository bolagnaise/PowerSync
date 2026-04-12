"""DataUpdateCoordinator for Powerwall local monitoring.

Runs alongside the existing ``TeslaEnergyCoordinator`` (cloud) and provides a
low-latency local snapshot every ``POWERWALL_LOCAL_POLL_INTERVAL`` seconds
when the gateway is paired. When unreachable the coordinator does not raise —
it leaves ``snapshot`` at ``None`` so consumers know to fall back to cloud data.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from ..const import (
    CONF_POWERWALL_LOCAL_PAIRED,
    POWERWALL_LOCAL_POLL_INTERVAL,
)
from .client import PowerwallLocalClient, PowerwallSnapshot
from .exceptions import (
    PowerwallLocalError,
    PowerwallSignatureError,
    PowerwallUnreachableError,
)

_LOGGER = logging.getLogger(__name__)


class PowerwallLocalCoordinator(DataUpdateCoordinator[PowerwallSnapshot | None]):
    """Polls the Powerwall gateway directly for live telemetry."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: PowerwallLocalClient,
        *,
        entry: ConfigEntry,
    ) -> None:
        # Modern HA DataUpdateCoordinator requires config_entry in __init__
        # otherwise async_config_entry_first_refresh() raises
        # "only supported for coordinators with a config entry". Passing
        # it as a keyword works on HA 2024.11+ without breaking older
        # releases (they ignore unknown kwargs).
        try:
            super().__init__(
                hass,
                _LOGGER,
                name=f"powerwall_local_{entry.entry_id}",
                update_interval=timedelta(seconds=POWERWALL_LOCAL_POLL_INTERVAL),
                config_entry=entry,
            )
        except TypeError:
            # HA < 2024.11 — no config_entry kwarg on the base class.
            super().__init__(
                hass,
                _LOGGER,
                name=f"powerwall_local_{entry.entry_id}",
                update_interval=timedelta(seconds=POWERWALL_LOCAL_POLL_INTERVAL),
            )
        self._client = client
        self._entry_id = entry.entry_id
        self._consecutive_failures = 0
        self._last_success_ts: float | None = None
        # Flips True when the gateway rejects our RSA signature — ie the
        # user revoked the key from the Tesla app or did a factory reset.
        # Surfaces via the app banner and a re-pair push notification.
        self._needs_repair = False

    @property
    def needs_repair(self) -> bool:
        return self._needs_repair

    @property
    def client(self) -> PowerwallLocalClient:
        return self._client

    @property
    def last_success_ts(self) -> float | None:
        return self._last_success_ts

    @property
    def reachable(self) -> bool:
        return self._consecutive_failures == 0

    def replace_client(self, client: PowerwallLocalClient) -> None:
        """Swap in a new client (eg after re-pair) without resetting the coordinator."""
        self._client = client
        self._consecutive_failures = 0

    async def _async_update_data(self) -> PowerwallSnapshot | None:
        try:
            snap = await self._client.get_snapshot()
        except PowerwallSignatureError as err:
            # Gateway no longer recognises our RSA key — usually means the
            # user revoked it from the Tesla app, or the gateway was
            # factory-reset. Flip the paired flag off so the re-pair banner
            # shows in the mobile app, fire a push notification once, and
            # stop trying to poll.
            if not self._needs_repair:
                self._needs_repair = True
                await self._handle_key_rejected(err)
            raise UpdateFailed(f"Powerwall key rejected: {err}") from err
        except PowerwallUnreachableError as err:
            self._consecutive_failures += 1
            if self._consecutive_failures <= 3:
                _LOGGER.debug(
                    "Powerwall local unreachable (attempt %s): %s",
                    self._consecutive_failures,
                    err,
                )
            raise UpdateFailed(f"Powerwall unreachable: {err}") from err
        except PowerwallLocalError as err:
            self._consecutive_failures += 1
            raise UpdateFailed(f"Powerwall local error: {err}") from err

        import time as _time

        self._last_success_ts = _time.time()
        self._consecutive_failures = 0
        return snap

    async def _handle_key_rejected(self, err: Exception) -> None:
        """Mark the entry as unpaired and prompt the user to re-pair.

        Runs once per rejection event. Updates ``entry.data`` so the
        binary_sensor.powerwall_local_paired flips off, which in turn hides
        the Local Control dashboard card and surfaces the re-pair banner
        in the Battery Setup screen. Fires a push notification so the
        user knows why their local control just stopped working.
        """
        _LOGGER.warning(
            "Powerwall gateway rejected our RSA key — marking entry as needs-repair: %s",
            err,
        )
        # Walk the config entries to find the one we belong to. We cached
        # entry_id at construction so this lookup is O(1).
        for entry in self.hass.config_entries.async_entries():
            if entry.entry_id != self._entry_id:
                continue
            new_data = {**entry.data}
            new_data[CONF_POWERWALL_LOCAL_PAIRED] = False
            self.hass.config_entries.async_update_entry(entry, data=new_data)
            break
        try:
            from ..automations.actions import _send_expo_push

            await _send_expo_push(
                self.hass,
                "🔒 Powerwall Re-pair Required",
                "Your Powerwall gateway no longer recognises PowerSync's "
                "local control key. Open Battery Setup and tap Pair Gateway "
                "to restore direct LAN control.",
            )
        except Exception as push_err:
            _LOGGER.debug("Re-pair push notification failed: %s", push_err)

    def snapshot_as_api(self) -> dict[str, Any]:
        """Shape the snapshot into an app-friendly dict."""
        snap = self.data
        if snap is None:
            return {
                "available": False,
                "reachable": self.reachable,
                "last_success_ts": self._last_success_ts,
                "needs_repair": self._needs_repair,
            }
        return {
            "available": True,
            "reachable": True,
            "last_success_ts": self._last_success_ts,
            "needs_repair": self._needs_repair,
            "soc_percent": snap.soc,
            "solar_w": snap.solar_w,
            "battery_w": snap.battery_w,
            "grid_w": snap.grid_w,
            "load_w": snap.load_w,
            "grid_status": snap.grid_status,
            "operation_mode": snap.operation_mode,
            "backup_reserve_percent": snap.backup_reserve_percent,
            "gateway_host": self._client.host,
            "gateway_din": self._client.din,
            "version": self._client.version.value,
        }
