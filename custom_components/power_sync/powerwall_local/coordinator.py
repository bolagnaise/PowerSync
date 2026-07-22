"""DataUpdateCoordinator for Powerwall local monitoring.

Runs alongside the existing ``TeslaEnergyCoordinator`` (cloud) and provides a
low-latency local snapshot every ``POWERWALL_LOCAL_POLL_INTERVAL`` seconds
when the gateway is paired. When unreachable the coordinator does not raise —
it leaves ``snapshot`` at ``None`` so consumers know to fall back to cloud data.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from ..const import (
    CONF_POWERWALL_LOCAL_PAIRED,
    DOMAIN,
    POWERWALL_LOCAL_POLL_INTERVAL,
)
from .client import PowerwallLocalClient, PowerwallSnapshot
from .exceptions import (
    PowerwallLocalError,
    PowerwallSignatureError,
    PowerwallUnreachableError,
)
from .normalization import (
    detect_local_backup_reserve_offset,
    normalize_local_backup_reserve_percent,
)

_LOGGER = logging.getLogger(__name__)

# Hard ceiling on a single snapshot fetch. The transport's per-request
# ClientTimeout is 8s, but that relies on event-loop timer callbacks — when the
# loop is briefly blocked (weak hardware / startup load) those timers fire late
# and a dead-gateway connect can run to the OS TCP timeout (~100s). That long
# block during the first refresh exceeds HA's bootstrap window and crash-loops
# setup, so wrap every fetch in an explicit ceiling well under the per-request
# budget's worst case but far below the runaway.
POWERWALL_LOCAL_SNAPSHOT_TIMEOUT = 15.0
POWERWALL_LOCAL_DIAGNOSTICS_INTERVAL = 300.0
POWERWALL_LOCAL_DIAGNOSTICS_TIMEOUT = 15.0
_BACKUP_RESERVE_WRITE_LOCAL_KEY = "powerwall_local_backup_reserve_write_local_pct"
_BACKUP_RESERVE_WRITE_USER_KEY = "powerwall_local_backup_reserve_write_user_pct"
_CLOUD_FALLBACK_PENDING_KEY = "powerwall_local_cloud_fallback_pending"


def _reserve_matches(left: Any, right: Any) -> bool:
    try:
        return abs(float(left) - float(right)) <= 0.5
    except (TypeError, ValueError):
        return False


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
        self._last_success_monotonic: float | None = None
        # Flips True when the gateway rejects our RSA signature — ie the
        # user revoked the key from the Tesla app or did a factory reset.
        # Surfaces via the app banner and a re-pair push notification.
        self._needs_repair = False
        self._v1r_diagnostics: dict[str, Any] = {
            "available": False,
            "last_attempt_ts": None,
            "last_success_ts": None,
            "error": None,
            "system_info": None,
            "networking": None,
            "internet": None,
        }
        self._v1r_diagnostics_last_attempt_monotonic: float | None = None
        self._v1r_diagnostics_task: asyncio.Task[None] | None = None

        # DataUpdateCoordinator pauses its periodic schedule when it has zero
        # listeners (HA 2023.x+ optimisation). Entity listeners attach in
        # ``async_added_to_hass`` which races with our coordinator setup —
        # if sensors win the race, ``_local_coordinator()`` returns None and
        # the listener is never added, leaving the coordinator silent forever
        # after its one-shot first refresh. Anchor a keep-alive no-op listener
        # at construction so the schedule stays armed regardless of who else
        # subscribes downstream.
        self._keepalive_unsub = self.async_add_listener(self._keepalive_noop)

    @staticmethod
    def _keepalive_noop() -> None:
        """No-op listener that exists solely to keep the periodic poll armed."""
        return

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
    def last_success_monotonic(self) -> float | None:
        return self._last_success_monotonic

    @property
    def reachable(self) -> bool:
        return self._consecutive_failures == 0

    def replace_client(self, client: PowerwallLocalClient) -> None:
        """Swap in a new client (eg after re-pair) without resetting the coordinator."""
        self._client = client
        self._consecutive_failures = 0

    async def _async_update_data(self) -> PowerwallSnapshot | None:
        if not self._client.local_access_enabled:
            return None

        try:
            snap = await asyncio.wait_for(
                self._client.get_snapshot(),
                timeout=POWERWALL_LOCAL_SNAPSHOT_TIMEOUT,
            )
        except (asyncio.TimeoutError, TimeoutError) as err:
            self._consecutive_failures += 1
            if self._consecutive_failures <= 3:
                _LOGGER.debug(
                    "Powerwall local snapshot timed out after %.0fs (attempt %s)",
                    POWERWALL_LOCAL_SNAPSHOT_TIMEOUT,
                    self._consecutive_failures,
                )
            raise UpdateFailed(
                "Powerwall unreachable: snapshot timed out after "
                f"{POWERWALL_LOCAL_SNAPSHOT_TIMEOUT:.0f}s"
            ) from err
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

        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry_id, {})
        cloud_fallback_pending = isinstance(entry_data, dict) and entry_data.pop(
            _CLOUD_FALLBACK_PENDING_KEY, False
        )
        if not cloud_fallback_pending:
            self._last_success_ts = time.time()
            self._last_success_monotonic = time.monotonic()
        self._consecutive_failures = 0
        self._update_backup_reserve_offset(snap)
        self._schedule_v1r_diagnostics_if_due()
        return snap

    def _schedule_v1r_diagnostics_if_due(self) -> None:
        """Refresh slow Common API diagnostics without delaying live telemetry."""
        if not callable(getattr(self._client, "get_v1r_diagnostics", None)):
            return
        now = time.monotonic()
        last_attempt = getattr(
            self, "_v1r_diagnostics_last_attempt_monotonic", None
        )
        task = getattr(self, "_v1r_diagnostics_task", None)
        if task is not None and not task.done():
            return
        if (
            last_attempt is not None
            and now - last_attempt < POWERWALL_LOCAL_DIAGNOSTICS_INTERVAL
        ):
            return
        self._v1r_diagnostics_last_attempt_monotonic = now
        self._v1r_diagnostics_task = self.hass.async_create_task(
            self._async_refresh_v1r_diagnostics(),
            f"powerwall_v1r_diagnostics_{self._entry_id}",
        )

    async def _async_refresh_v1r_diagnostics(self) -> None:
        """Fetch and publish the credential-free Common API diagnostic set."""
        attempted_at = time.time()
        previous = getattr(self, "_v1r_diagnostics", {})
        try:
            diagnostics = await asyncio.wait_for(
                self._client.get_v1r_diagnostics(),
                timeout=POWERWALL_LOCAL_DIAGNOSTICS_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except Exception as err:
            self._v1r_diagnostics = {
                **previous,
                "available": False,
                "last_attempt_ts": attempted_at,
                "error": str(err),
            }
            _LOGGER.debug("Powerwall v1r diagnostics unavailable: %s", err)
        else:
            available = any(diagnostics.get(key) is not None for key in (
                "system_info",
                "networking",
                "internet",
            ))
            self._v1r_diagnostics = {
                **diagnostics,
                "available": available,
                "last_attempt_ts": attempted_at,
                "last_success_ts": attempted_at if available else previous.get(
                    "last_success_ts"
                ),
                "error": None if available else "No supported Common API response",
            }
        self.async_update_listeners()

    async def async_shutdown(self) -> None:
        """Cancel any in-flight background diagnostics during entry unload."""
        task = getattr(self, "_v1r_diagnostics_task", None)
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def _update_backup_reserve_offset(self, snap: PowerwallSnapshot) -> None:
        """Detect the local reserve offset by comparing local and cloud readbacks."""
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry_id, {})
        if not isinstance(entry_data, dict):
            return

        config = (snap.raw or {}).get("config") or {}
        site_info = config.get("site_info") or {}
        local_reserve = site_info.get("backup_reserve_percent")
        tesla_coord = entry_data.get("tesla_coordinator")
        cloud_site_info = getattr(tesla_coord, "_site_info_cache", None)
        cloud_reserve = (
            cloud_site_info.get("backup_reserve_percent")
            if isinstance(cloud_site_info, dict)
            else None
        )

        pending_local_write = entry_data.get(_BACKUP_RESERVE_WRITE_LOCAL_KEY)
        pending_user_reserve = entry_data.get(_BACKUP_RESERVE_WRITE_USER_KEY)
        pending_offset = detect_local_backup_reserve_offset(
            pending_local_write,
            pending_user_reserve,
        )
        if (
            pending_offset is not None
            and _reserve_matches(local_reserve, pending_local_write)
        ):
            previous = entry_data.get("powerwall_local_low_soe_reserve_pct")
            entry_data["powerwall_local_low_soe_reserve_pct"] = pending_offset
            normalized = normalize_local_backup_reserve_percent(
                local_reserve,
                pending_offset,
            )
            if normalized is not None:
                snap.backup_reserve_percent = normalized
            if _reserve_matches(cloud_reserve, pending_user_reserve):
                entry_data.pop(_BACKUP_RESERVE_WRITE_LOCAL_KEY, None)
                entry_data.pop(_BACKUP_RESERVE_WRITE_USER_KEY, None)
            if previous != pending_offset:
                _LOGGER.info(
                    "Using Powerwall local backup reserve write offset: %.1f%% "
                    "(local=%s%%, requested=%s%%, Tesla site_info=%s%%)",
                    pending_offset,
                    local_reserve,
                    pending_user_reserve,
                    cloud_reserve,
                )
            return
        if pending_local_write is not None or pending_user_reserve is not None:
            entry_data.pop(_BACKUP_RESERVE_WRITE_LOCAL_KEY, None)
            entry_data.pop(_BACKUP_RESERVE_WRITE_USER_KEY, None)

        detected = detect_local_backup_reserve_offset(local_reserve, cloud_reserve)
        if detected is None:
            persisted = entry_data.get("powerwall_local_low_soe_reserve_pct")
            if persisted is not None:
                normalized = normalize_local_backup_reserve_percent(
                    local_reserve,
                    persisted,
                )
                if normalized is not None:
                    snap.backup_reserve_percent = normalized
            return

        previous = entry_data.get("powerwall_local_low_soe_reserve_pct")
        entry_data["powerwall_local_low_soe_reserve_pct"] = detected
        normalized = normalize_local_backup_reserve_percent(
            local_reserve,
            detected,
        )
        if normalized is not None:
            snap.backup_reserve_percent = normalized
        if previous != detected:
            _LOGGER.info(
                "Detected Powerwall local backup reserve offset: %.1f%% "
                "(local=%s%%, Tesla site_info=%s%%)",
                detected,
                local_reserve,
                cloud_reserve,
            )

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
                "snapshot_available": False,
                "last_success_ts": self._last_success_ts,
                "needs_repair": self._needs_repair,
                "v1r_diagnostics": getattr(self, "_v1r_diagnostics", None),
            }
        ev_power_w = self._observed_ev_power_w()
        load_w = snap.load_w
        if load_w is not None:
            load_w = max(0.0, load_w - ev_power_w)

        local_reachable = self.reachable
        return {
            "available": local_reachable,
            "reachable": local_reachable,
            "snapshot_available": True,
            "last_success_ts": self._last_success_ts,
            "needs_repair": self._needs_repair,
            "soc_percent": snap.soc,
            "solar_w": snap.solar_w,
            "battery_w": snap.battery_w,
            "grid_w": snap.grid_w,
            "load_w": load_w,
            "raw_load_w": snap.load_w,
            "ev_power_w": ev_power_w,
            "grid_status": snap.grid_status,
            "operation_mode": snap.operation_mode,
            "backup_reserve_percent": snap.backup_reserve_percent,
            "grid_charging_enabled": snap.grid_charging_enabled,
            "grid_export_rule": snap.grid_export_rule,
            "gateway_host": self._client.host,
            "gateway_din": self._client.din,
            "version": self._client.version.value,
            "v1r_diagnostics": getattr(self, "_v1r_diagnostics", None),
        }

    def _observed_ev_power_w(self) -> float:
        """Return observed EV charging power from the site coordinator in watts."""
        try:
            entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry_id, {})
            for coord_key in (
                "tesla_coordinator",
                "sigenergy_coordinator",
                "sungrow_coordinator",
                "foxess_coordinator",
            ):
                data = getattr(entry_data.get(coord_key), "data", None)
                if not data:
                    continue
                ev_power_kw = data.get("ev_power")
                if ev_power_kw is None:
                    ev_power_kw = data.get("ev_power_kw")
                if ev_power_kw is not None:
                    return max(0.0, float(ev_power_kw or 0.0) * 1000.0)
        except (TypeError, ValueError, AttributeError):
            return 0.0
        return 0.0
