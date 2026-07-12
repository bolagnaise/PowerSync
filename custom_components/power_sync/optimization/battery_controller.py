"""
Battery controller wrappers for optimization executor.

Provides unified interface for controlling different battery systems:
- Tesla: Uses TOU tariff trick (upload fake rates to incentivize charge/discharge)
- Sigenergy: Uses Modbus commands
- Sungrow: Uses Modbus commands
"""
from __future__ import annotations

import collections
import enum
import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant

from ..const import TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS

_LOGGER = logging.getLogger(__name__)

TESLA_SITE_INFO_MAX_AGE_SECONDS = 900
_BACKUP_RESERVE_WRITE_USER_KEY = "powerwall_local_backup_reserve_write_user_pct"


class ReserveTrust(enum.Enum):
    """Provenance tag for a backup-reserve reading."""

    LIVE = "live"
    CLOUD_FRESH = "cloud_fresh"
    CLOUD_STALE = "cloud_stale"
    ENTITY = "entity"
    NONE = "none"


ReserveReading = collections.namedtuple("ReserveReading", "percent trust source")

TRUSTED_FOR_PERSIST = frozenset({ReserveTrust.LIVE, ReserveTrust.CLOUD_FRESH})


def _coerce_reserve_percent(value: Any) -> int | None:
    try:
        reserve = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, reserve))


def _pending_powerwall_local_reserve_write(
    entry_data: dict[str, Any],
) -> int | None:
    """Return the user-facing target for an in-flight local reserve write."""
    if not isinstance(entry_data, dict):
        return None
    return _coerce_reserve_percent(entry_data.get(_BACKUP_RESERVE_WRITE_USER_KEY))


def _fresh_powerwall_local_snapshot(
    entry_data: dict[str, Any],
) -> Any | None:
    """Return fresh local Powerwall data when available, otherwise None."""
    coordinator = (
        (entry_data.get("powerwall_local") or {}).get("coordinator")
        if isinstance(entry_data, dict)
        else None
    )
    data = getattr(coordinator, "data", None)
    last_success_monotonic = getattr(coordinator, "last_success_monotonic", None)
    if data is None or last_success_monotonic is None:
        return None
    if time.monotonic() - last_success_monotonic > TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS:
        return None
    return data


class BatteryControllerWrapper:
    """
    Wrapper that provides force_charge/force_discharge/restore_normal interface.

    Delegates to the existing PowerSync service handlers via hass.services.async_call,
    which is the stable HA service API across all versions.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        battery_system: str,  # "tesla", "sigenergy", "sungrow", "foxess"
    ):
        """
        Initialize the battery controller wrapper.

        Args:
            hass: Home Assistant instance
            battery_system: Type of battery system
        """
        self.hass = hass
        self.battery_system = battery_system

    async def force_charge(self, duration_minutes: int = 60, power_w: float = 5000, _extend_hardware: bool = False) -> bool:
        """
        Command battery to charge.

        For Tesla: Uploads TOU tariff with $0/kWh buy rate to incentivize charging
        For Sigenergy/Sungrow: Uses Modbus to set charge mode

        Args:
            duration_minutes: How long to charge
            power_w: Target charge power (may not be controllable on all systems)

        Returns:
            True if command was sent successfully
        """
        try:
            _LOGGER.info(f"🔋 Optimizer: Force charge {duration_minutes}min at {power_w}W")

            service_data = {"duration": duration_minutes, "power_w": power_w, "source": "optimizer"}
            if _extend_hardware:
                service_data["_extend_hardware"] = True
            await self.hass.services.async_call(
                "power_sync", "force_charge",
                service_data,
                blocking=True,
            )
            return True

        except Exception as e:
            _LOGGER.error(f"Force charge failed: {e}", exc_info=True)
            return False

    async def force_discharge(
        self,
        duration_minutes: int = 60,
        power_w: float = 5000,
        _extend_hardware: bool = False,
        _tariff_duration: int | None = None,
    ) -> bool:
        """
        Command battery to discharge.

        For Tesla: Uploads TOU tariff with $20/kWh sell rate to incentivize discharge
        For Sigenergy/Sungrow: Uses Modbus to set discharge mode

        Args:
            duration_minutes: How long to discharge
            power_w: Target discharge power (may not be controllable on all systems)

        Returns:
            True if command was sent successfully
        """
        try:
            _LOGGER.info(f"🔋 Optimizer: Force discharge {duration_minutes}min at {power_w}W")

            service_data = {"duration": duration_minutes, "power_w": power_w, "source": "optimizer"}
            if _extend_hardware:
                service_data["_extend_hardware"] = True
            if _tariff_duration is not None:
                service_data["_tariff_duration"] = _tariff_duration
            await self.hass.services.async_call(
                "power_sync", "force_discharge",
                service_data,
                blocking=True,
            )
            return True

        except Exception as e:
            _LOGGER.error(f"Force discharge failed: {e}", exc_info=True)
            return False

    async def restore_normal(self) -> bool:
        """
        Restore battery to normal autonomous operation.

        For Tesla: Uploads original TOU tariff and sets self-consumption mode
        For Sigenergy/Sungrow: Restores normal operating mode via Modbus

        Returns:
            True if command was sent successfully
        """
        try:
            _LOGGER.info("🔋 Optimizer: Restoring normal operation")

            await self.hass.services.async_call(
                "power_sync", "restore_normal",
                {"source": "optimizer", "_allow_monitoring_restore": True},
                blocking=True,
            )
            return True

        except Exception as e:
            _LOGGER.error(f"Restore normal failed: {e}", exc_info=True)
            return False

    async def set_self_consumption_mode(self) -> bool:
        """
        Set battery to pure self-consumption mode (no TOU optimization).

        This is used for CONSUME action (battery→home) where we want the
        battery to naturally offset home load WITHOUT making autonomous
        charge/discharge decisions based on TOU rates.

        Unlike restore_normal, this:
        - Sets mode to self_consumption (not autonomous)
        - Does NOT restore TOU tariff
        - Does NOT send push notifications

        Returns:
            True if command was sent successfully
        """
        try:
            _LOGGER.info("🔋 Optimizer: Setting pure self-consumption mode (battery→home, no TOU)")

            await self.hass.services.async_call(
                "power_sync", "set_self_consumption",
                {"source": "optimizer"},
                blocking=True,
            )
            return True

        except Exception as e:
            _LOGGER.error(f"Set self-consumption mode failed: {e}", exc_info=True)
            return False

    async def set_autonomous_mode(self) -> bool:
        """
        Set battery to autonomous (TOU) mode.

        In autonomous mode, Tesla respects backup_reserve as a hard floor and
        makes charge/discharge decisions based on TOU rates. This is required
        for IDLE to work correctly — backup_reserve alone is not enough in
        self_consumption mode.

        Returns:
            True if command was sent successfully
        """
        try:
            _LOGGER.info("🔋 Optimizer: Setting autonomous (TOU) mode")

            await self.hass.services.async_call(
                "power_sync", "set_autonomous",
                {},
                blocking=True,
            )
            return True

        except Exception as e:
            _LOGGER.error(f"Set autonomous mode failed: {e}", exc_info=True)
            return False

    async def read_backup_reserve(self) -> ReserveReading:
        """
        Read current battery backup reserve percentage, tagged with provenance.

        Reads from the energy coordinator's underlying controller (Modbus/API),
        current Tesla HA entities, or the Tesla coordinator's cached site_info.
        Returns ReserveReading(None, ReserveTrust.NONE, ...) if not available.
        """
        try:
            from ..const import DOMAIN
            for entry_id, entry_data in self.hass.data.get(DOMAIN, {}).items():
                if not isinstance(entry_data, dict):
                    continue
                if self.battery_system == "tesla":
                    pending_reserve = _pending_powerwall_local_reserve_write(
                        entry_data
                    )
                    if pending_reserve is not None:
                        return ReserveReading(pending_reserve, ReserveTrust.LIVE, "pending_local_write")

                    local_snap = _fresh_powerwall_local_snapshot(entry_data)
                    local_reserve = getattr(
                        local_snap,
                        "backup_reserve_percent",
                        None,
                    )
                    if local_reserve is not None:
                        coerced = _coerce_reserve_percent(local_reserve)
                        if coerced is not None:
                            return ReserveReading(coerced, ReserveTrust.LIVE, "local_snapshot")

                # Tesla: prefer cached site_info over the HA number entity
                # because the entity can fall back to a stale persisted user
                # reserve during setup. Fresh local readback above wins.
                tesla_coord = entry_data.get("tesla_coordinator") or entry_data.get("coordinator")
                if tesla_coord and hasattr(tesla_coord, "_site_info_cache") and tesla_coord._site_info_cache:
                    reserve = tesla_coord._site_info_cache.get("backup_reserve_percent")
                    if reserve is not None:
                        last_fetch = getattr(tesla_coord, "_site_info_last_fetch", 0) or 0
                        age = time.monotonic() - last_fetch
                        trust = (
                            ReserveTrust.CLOUD_FRESH
                            if age <= TESLA_SITE_INFO_MAX_AGE_SECONDS
                            else ReserveTrust.CLOUD_STALE
                        )
                        return ReserveReading(int(reserve), trust, "site_info_cache")
                # Prefer the coordinator's latest data when available. This
                # also covers wrappers like DualSungrowCoordinator and entity-
                # based bridges whose underlying controller may not expose a
                # direct get_backup_reserve method.
                for coord_key in (
                    "sigenergy_coordinator",
                    "sungrow_coordinator",
                    "foxess_coordinator",
                    "goodwe_coordinator",
                    "esy_sunhome_coordinator",
                    "solax_coordinator",
                    "saj_h2_coordinator",
                    "fronius_reserva_coordinator",
                    "neovolt_coordinator",
                    "solaredge_coordinator",
                    "anker_solix_coordinator",
                ):
                    coord = entry_data.get(coord_key)
                    data = getattr(coord, "data", None) or {}
                    for data_key in ("backup_reserve", "min_soc"):
                        reserve = data.get(data_key)
                        if reserve is not None:
                            return ReserveReading(int(float(reserve)), ReserveTrust.LIVE, coord_key)

                # Modbus-based batteries: read from controller
                for coord_key in ("sigenergy_coordinator", "sungrow_coordinator", "foxess_coordinator", "goodwe_coordinator", "esy_sunhome_coordinator", "solax_coordinator", "saj_h2_coordinator", "fronius_reserva_coordinator", "neovolt_coordinator", "solaredge_coordinator", "anker_solix_coordinator"):
                    coord = entry_data.get(coord_key)
                    if coord and hasattr(coord, "_controller") and hasattr(coord._controller, "get_backup_reserve"):
                        reserve = await coord._controller.get_backup_reserve()
                        return ReserveReading(reserve, ReserveTrust.LIVE, coord_key)
            if self.battery_system == "tesla" and hasattr(self.hass, "states"):
                state = self.hass.states.get("number.power_sync_tesla_backup_reserve")
                if state and state.state not in (None, "unknown", "unavailable"):
                    return ReserveReading(int(float(state.state)), ReserveTrust.ENTITY, "ha_entity")
            return ReserveReading(None, ReserveTrust.NONE, "unavailable")
        except Exception as e:
            _LOGGER.debug(f"get_backup_reserve failed: {e}")
            return ReserveReading(None, ReserveTrust.NONE, "error")

    async def get_backup_reserve(self) -> int | None:
        """
        Read current battery backup reserve percentage.

        Thin wrapper over read_backup_reserve() for callers that only need
        the value, not its provenance.
        """
        return (await self.read_backup_reserve()).percent

    async def get_tesla_operation_mode(self) -> str | None:
        """
        Read the actual Tesla operation mode from HA state or site_info cache.

        Returns the live hardware mode string (e.g. "self_consumption",
        "autonomous") or None if not a Tesla / cache not populated.
        """
        try:
            from ..const import DOMAIN
            if self.battery_system == "tesla" and hasattr(self.hass, "states"):
                state = self.hass.states.get("select.power_sync_tesla_operation_mode")
                if state and state.state not in (None, "unknown", "unavailable"):
                    return str(state.state)
            for entry_id, entry_data in self.hass.data.get(DOMAIN, {}).items():
                if not isinstance(entry_data, dict):
                    continue
                tesla_coord = entry_data.get("tesla_coordinator") or entry_data.get("coordinator")
                if tesla_coord and hasattr(tesla_coord, "_site_info_cache") and tesla_coord._site_info_cache:
                    return tesla_coord._site_info_cache.get("default_real_mode")
            return None
        except Exception as e:
            _LOGGER.debug(f"get_tesla_operation_mode failed: {e}")
            return None

    async def set_backup_reserve(self, percent: int) -> bool:
        """
        Set battery backup reserve percentage.

        Used by the optimizer's IDLE action to hold SOC by setting backup
        reserve to the current SOC%. This prevents the battery from
        discharging while the home draws from the grid.

        Args:
            percent: Backup reserve percentage (0-100)

        Returns:
            True if command was sent successfully
        """
        try:
            await self.hass.services.async_call(
                "power_sync", "set_backup_reserve",
                {"percent": percent, "source": "optimizer"},
                blocking=True,
            )
            return True

        except Exception as e:
            _LOGGER.error(f"Set backup reserve failed: {e}", exc_info=True)
            return False
