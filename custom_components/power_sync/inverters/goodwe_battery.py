"""GoodWe battery controller using the goodwe PyPI library.

Supports ET/EH/BT/BH and ES/EM/BP series hybrid inverters.
Uses the goodwe library for auto-detection, protocol handling, and battery control.

Separate from inverters/goodwe.py which handles AC-coupled curtailment via pymodbus.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)

_GOODWE_UDP_PORT = 8899
_UDP_FAILURE_CACHE_SECONDS = 300  # re-try UDP no more than once every 5 min
_GOODWE_EXPORT_LIMIT_MAX_W = 65535

# Curtailment methods for the direct hybrid controller.
CURTAILMENT_METHOD_EXPORT_LIMIT = "export_limit"
CURTAILMENT_METHOD_EMS_LOAD_FOLLOWING = "ems_load_following"
CURTAILMENT_METHODS = (
    CURTAILMENT_METHOD_EXPORT_LIMIT,
    CURTAILMENT_METHOD_EMS_LOAD_FOLLOWING,
)

# GoodWe Third-Party EMS Protocol MODBUS V1.1.1, section 9 and Table 1-4.
# 47511 EMS Power Mode and 47512 EMS Power Set (U16, W) are listed for ESA,
# ET and EH families. BuyPower (0x0009) holds grid purchase at the set point
# using the site meter: "When the PV power is too large, the MPPT power will
# be limited. When the load is too large, the battery will discharge." With a
# 0 W set point that is native load following: PV keeps supplying the house
# and battery while export is curtailed.
_EMS_MODE_REGISTER = 47511
_EMS_POWER_REGISTER = 47512
_EMS_FALLBACK_REGISTER = 47505
_EMS_MODE_AUTO = 0x0001
_EMS_MODE_BUY_POWER = 0x0009
_EMS_MODE_STOPPED = 0x00FF
_EMS_DOCUMENTED_MODES = frozenset(range(0x0001, 0x000D)) | {_EMS_MODE_STOPPED}
# 47505: 10 (default) keeps the last EMS mode when no instruction arrives for
# 30 s. 2 shuts the inverter down and 3 returns to self-use, so neither can
# hold a PowerSync curtailment between its periodic checks.
_EMS_FALLBACK_KEEP_LAST_MODE = 10


class GoodWeBatteryController:
    """Battery controller for GoodWe hybrid inverters."""

    def __init__(
        self,
        host: str,
        port: int = 8899,
        comm_addr: int = 0,
        curtailment_method: str = CURTAILMENT_METHOD_EXPORT_LIMIT,
    ):
        self.host = host
        self.port = port
        self.comm_addr = comm_addr
        self.curtailment_method = (
            curtailment_method
            if curtailment_method in CURTAILMENT_METHODS
            else CURTAILMENT_METHOD_EXPORT_LIMIT
        )
        # Which method owns the saved pre-curtailment baseline. Restore always
        # follows the owner, even if the configured method changed meanwhile.
        self._saved_method: str | None = None
        self._saved_ems_mode: int | None = None
        self._saved_ems_power_limit: int | None = None
        self._inverter = None  # goodwe.Inverter instance (data / TCP)
        self._lock: asyncio.Lock | None = None
        self._udp_unavailable_until: float = 0.0  # epoch time; 0 = never tried
        self._saved_grid_export_enabled: int | None = None
        self._saved_grid_export_limit: int | None = None
        self._grid_export_state_saved = False

    async def connect(self) -> bool:
        """Connect to inverter and auto-detect model family."""
        import goodwe

        if self._lock is None:
            self._lock = asyncio.Lock()

        async with self._lock:
            self._inverter = await goodwe.connect(
                host=self.host, port=self.port, comm_addr=self.comm_addr
            )
            _LOGGER.info(
                "Connected to GoodWe %s (SN: %s, rated: %sW)",
                self._inverter.model_name,
                self._inverter.serial_number,
                self._inverter.rated_power,
            )
            return True

    async def _apply_operation_mode(self, mode, **kwargs) -> bool:
        """Write an operation mode and verify it was accepted.

        GoodWe mode changes (work_mode, eco_mode registers) are only reliably
        applied via the proprietary UDP protocol on port 8899. When the data
        connection uses TCP/Modbus (port 502), the inverter ACKs writes but
        silently ignores them. We therefore open a temporary UDP connection
        specifically for control operations when the data port is not UDP.
        """
        import goodwe

        if self.port == _GOODWE_UDP_PORT:
            # Already UDP — write and verify on the same connection.
            await self._inverter.set_operation_mode(mode, **kwargs)
            return await self._verify_mode(self._inverter, mode)

        # TCP/Modbus data connection — open a fresh UDP connection for the write.
        # Skip the attempt if UDP has recently proven unreachable (avoids a 10s
        # timeout stall on every force-charge call when port 8899 is blocked).
        now = time.monotonic()
        if now < self._udp_unavailable_until:
            remaining = int(self._udp_unavailable_until - now)
            _LOGGER.warning(
                "GoodWe UDP control skipped (port %d unreachable, retry in %ds) — "
                "TCP write attempted but will not take effect. Ensure UDP port %d "
                "is reachable from Home Assistant at %s, or configure GoodWe EMS "
                "entity mode for LAN/Kit-20 Modbus TCP setups.",
                _GOODWE_UDP_PORT, remaining, _GOODWE_UDP_PORT, self.host,
            )
            await self._inverter.set_operation_mode(mode, **kwargs)
            return await self._verify_mode(self._inverter, mode)

        try:
            udp_inv = await asyncio.wait_for(
                goodwe.connect(
                    host=self.host,
                    port=_GOODWE_UDP_PORT,
                    comm_addr=self.comm_addr,
                ),
                timeout=10.0,
            )
            # Reset failure cache on successful connection.
            self._udp_unavailable_until = 0.0
        except Exception as exc:
            self._udp_unavailable_until = time.monotonic() + _UDP_FAILURE_CACHE_SECONDS
            _LOGGER.warning(
                "GoodWe UDP control connection to %s:%d failed: %s — "
                "trying TCP write (will not take effect). "
                "If using a Modbus TCP gateway, the inverter's direct network IP "
                "may differ from the gateway IP; UDP port %d must be reachable "
                "from Home Assistant for direct force charge/discharge. For LAN/Kit-20 "
                "setups that only support Modbus TCP, configure GoodWe EMS entity mode "
                "using the HA GoodWe integration's EMS entities instead.",
                self.host, _GOODWE_UDP_PORT, exc, _GOODWE_UDP_PORT,
            )
            # Fall back: write via TCP and verify via TCP.
            await self._inverter.set_operation_mode(mode, **kwargs)
            return await self._verify_mode(self._inverter, mode)

        try:
            await udp_inv.set_operation_mode(mode, **kwargs)
            _LOGGER.debug(
                "GoodWe control: mode %s written via UDP to %s:%d",
                mode, self.host, _GOODWE_UDP_PORT,
            )
        except Exception as exc:
            _LOGGER.error("GoodWe UDP mode write failed: %s", exc)
            return False
        finally:
            try:
                await udp_inv.disconnect()
            except Exception:
                pass

        # Verify via TCP read-back. This is best-effort — some inverter firmware
        # may have a brief delay before the register reflects the change.
        await asyncio.sleep(0.5)
        return await self._verify_mode(self._inverter, mode)

    async def _verify_mode(self, inverter, mode) -> bool:
        """Read work_mode back and confirm it matches the expected value."""
        import goodwe

        # ECO_CHARGE (98) and ECO_DISCHARGE (99) both set work_mode to ECO (3).
        if int(mode) in (int(goodwe.OperationMode.ECO_CHARGE), int(goodwe.OperationMode.ECO_DISCHARGE)):
            expected = 3
        else:
            expected = int(mode)

        try:
            actual = await inverter.read_setting("work_mode")
            if actual != expected:
                _LOGGER.error(
                    "GoodWe mode write did not take effect: expected work_mode=%d "
                    "but read back %s. Ensure the inverter's UDP port %d is reachable "
                    "from Home Assistant, or configure GoodWe EMS entity mode for "
                    "LAN/Kit-20 Modbus TCP setups. Force charge/discharge will not "
                    "work until one of these control paths is available.",
                    expected, actual, _GOODWE_UDP_PORT,
                )
                return False
            _LOGGER.debug("GoodWe mode verified: work_mode=%d", actual)
        except Exception as exc:
            # Read-back failed — log a warning but treat as success to avoid
            # blocking force-mode state when the write likely worked.
            _LOGGER.warning(
                "GoodWe work_mode read-back failed (non-fatal): %s", exc
            )

        return True

    async def get_runtime_data(self) -> dict[str, Any]:
        """Read runtime data from inverter.

        Returns dict with PowerSync standard fields:
        - solar_power (kW), grid_power (kW), battery_power (kW),
          load_power (kW), battery_level (%), battery_temperature (C),
          model_name, serial_number, rated_power
        """
        data = await self._inverter.read_runtime_data()

        # Map to PowerSync standard format with correct sign conventions:
        # GoodWe active_power: positive=export, negative=import
        # PowerSync grid_power: positive=import, negative=export
        grid_w = data.get("active_power", 0) or 0
        grid_kw = -(grid_w / 1000.0)  # Negate

        # GoodWe pbattery1: positive=discharge, negative=charge (matches PowerSync directly)
        # PowerSync battery_power: positive=discharge, negative=charge
        bat_w = data.get("pbattery1", 0) or 0
        battery_kw = bat_w / 1000.0

        solar_w = data.get("ppv", 0) or 0
        solar_kw = solar_w / 1000.0

        # Load: use house_consumption (calculated by library) or load_ptotal
        load_w = data.get("house_consumption", 0) or data.get("load_ptotal", 0) or 0
        load_kw = load_w / 1000.0

        soc = data.get("battery_soc", 0) or 0

        return {
            "solar_power": max(0, solar_kw),
            "grid_power": grid_kw,
            "battery_power": battery_kw,
            "load_power": max(0, load_kw),
            "battery_level": soc,
            "battery_temperature": data.get("battery_temperature"),
            "battery_soh": data.get("battery_soh"),
            "model_name": self._inverter.model_name,
            "serial_number": self._inverter.serial_number,
            "rated_power_w": self._inverter.rated_power,
        }

    async def force_charge(self, power_pct: int = 100, soc_target: int = 100) -> bool:
        """Force charge from grid using ECO_CHARGE mode."""
        import goodwe

        # A load-following BuyPower hold (EMS mode 9) would override the
        # work-mode command; hand the EMS mode back first.
        if not await self.release_ems_hold():
            return False

        ok = await self._apply_operation_mode(
            goodwe.OperationMode.ECO_CHARGE,
            eco_mode_power=power_pct,
            eco_mode_soc=soc_target,
        )
        if ok:
            _LOGGER.info("GoodWe force charge: power=%d%%, target_soc=%d%%", power_pct, soc_target)
        return ok

    async def force_discharge(self, power_pct: int = 100, soc_floor: int = 10) -> bool:
        """Force discharge to grid using ECO_DISCHARGE mode."""
        import goodwe

        # A load-following BuyPower hold (EMS mode 9) would override the
        # work-mode command; hand the EMS mode back first.
        if not await self.release_ems_hold():
            return False

        ok = await self._apply_operation_mode(
            goodwe.OperationMode.ECO_DISCHARGE,
            eco_mode_power=power_pct,
            eco_mode_soc=soc_floor,
        )
        if ok:
            _LOGGER.info("GoodWe force discharge: power=%d%%, floor_soc=%d%%", power_pct, soc_floor)
        return ok

    async def restore_normal(self) -> bool:
        """Restore to normal (GENERAL) operation mode."""
        import goodwe

        ok = await self._apply_operation_mode(goodwe.OperationMode.GENERAL)
        if ok:
            _LOGGER.info("GoodWe restored to GENERAL mode")
        return ok

    async def set_backup_reserve(self, percent: int) -> bool:
        """Set backup reserve (minimum SOC).

        GoodWe uses DOD (depth of discharge) which is inverse:
        DOD = 100 - reserve_percent
        """
        dod = max(0, min(100, 100 - percent))  # GoodWe on-grid DOD max is 100%
        await self._inverter.set_ongrid_battery_dod(dod)
        _LOGGER.info("GoodWe backup reserve set to %d%% (DOD=%d%%)", percent, dod)
        return True

    async def get_backup_reserve(self) -> int:
        """Get current backup reserve (minimum SOC) percentage."""
        dod = await self._inverter.get_ongrid_battery_dod()
        return 100 - dod

    async def set_grid_export_limit(self, watts: int) -> bool:
        """Set grid export limit in watts."""
        limit_w = max(0, min(_GOODWE_EXPORT_LIMIT_MAX_W, int(watts)))
        await self._inverter.set_grid_export_limit(limit_w)
        _LOGGER.info("GoodWe export limit set to %dW", limit_w)
        return True

    async def _read_grid_export_setting(self, setting_id: str) -> int | None:
        """Read a GoodWe export setting if the model exposes it."""
        try:
            value = await self._inverter.read_setting(setting_id)
            return int(value)
        except Exception as exc:
            _LOGGER.debug("GoodWe %s read unavailable: %s", setting_id, exc)
            return None

    async def _write_grid_export_enabled(self, enabled: int) -> bool:
        """Enable or disable the GoodWe grid export limiter when supported."""
        try:
            await self._inverter.write_setting("grid_export", int(enabled))
            _LOGGER.info("GoodWe export limit enable set to %d", int(enabled))
            return True
        except Exception as exc:
            _LOGGER.warning(
                "GoodWe export limit enable write failed; writing limit only: %s",
                exc,
            )
            return False

    async def _save_grid_export_state(self) -> None:
        """Capture the user's current export limit settings before curtailing."""
        if self._grid_export_state_saved:
            return
        self._saved_grid_export_enabled = await self._read_grid_export_setting(
            "grid_export"
        )
        self._saved_grid_export_limit = await self._read_grid_export_setting(
            "grid_export_limit"
        )
        self._saved_method = CURTAILMENT_METHOD_EXPORT_LIMIT
        self._grid_export_state_saved = True

    def get_grid_export_restore_state(self) -> dict[str, int | None] | None:
        """Return the export settings that must survive a controller restart."""
        if not self._grid_export_state_saved:
            return None
        if self._saved_method == CURTAILMENT_METHOD_EMS_LOAD_FOLLOWING:
            return {
                "method": CURTAILMENT_METHOD_EMS_LOAD_FOLLOWING,
                "ems_mode": self._saved_ems_mode,
                "ems_power_limit": self._saved_ems_power_limit,
            }
        return {
            "grid_export": self._saved_grid_export_enabled,
            "grid_export_limit": self._saved_grid_export_limit,
        }

    def restore_grid_export_restore_state(self, state: Any) -> bool:
        """Restore a previously persisted pre-curtail export-setting snapshot."""
        if not isinstance(state, dict):
            return False
        if state.get("method") == CURTAILMENT_METHOD_EMS_LOAD_FOLLOWING:
            try:
                mode = state.get("ems_mode")
                power = state.get("ems_power_limit")
                self._saved_ems_mode = int(mode) if mode is not None else None
                self._saved_ems_power_limit = int(power) if power is not None else None
            except (TypeError, ValueError, OverflowError):
                return False
            self._saved_method = CURTAILMENT_METHOD_EMS_LOAD_FOLLOWING
            self._grid_export_state_saved = True
            return True
        try:
            enabled = state.get("grid_export")
            limit = state.get("grid_export_limit")
            self._saved_grid_export_enabled = int(enabled) if enabled is not None else None
            self._saved_grid_export_limit = int(limit) if limit is not None else None
        except (TypeError, ValueError, OverflowError):
            return False
        self._saved_method = CURTAILMENT_METHOD_EXPORT_LIMIT
        self._grid_export_state_saved = True
        return True

    async def _verify_grid_export_settings(
        self, expected_enabled: int, expected_limit: int
    ) -> bool:
        """Confirm the effective GoodWe export-limit state after a write."""
        actual_enabled = await self._read_grid_export_setting("grid_export")
        actual_limit = await self._read_grid_export_setting("grid_export_limit")
        # GoodWe devices may normalize the inactive numeric limit to 0W when
        # the limiter is disabled. The enable flag is the effective control in
        # that state, while an enabled limiter must still echo its exact limit.
        if actual_enabled == expected_enabled and (
            expected_enabled == 0 or actual_limit == expected_limit
        ):
            return True
        _LOGGER.warning(
            "GoodWe export-limit write is not confirmed: expected enabled=%d, "
            "limit=%dW; read enabled=%s, limit=%sW",
            expected_enabled,
            expected_limit,
            actual_enabled,
            actual_limit,
        )
        return False

    @property
    def uses_ems_load_following(self) -> bool:
        """True when curtailment owns (or will own) the GoodWe EMS mode."""
        if self._grid_export_state_saved and self._saved_method is not None:
            return self._saved_method == CURTAILMENT_METHOD_EMS_LOAD_FOLLOWING
        return self.curtailment_method == CURTAILMENT_METHOD_EMS_LOAD_FOLLOWING

    async def _read_register(self, register: int) -> int | None:
        """Read one raw holding register through the goodwe library."""
        try:
            return int(await self._inverter.read_setting(f"modbus_{register}"))
        except Exception as exc:
            _LOGGER.debug("GoodWe register %d read unavailable: %s", register, exc)
            return None

    async def _read_ems_setting(self, setting_id: str) -> int | None:
        try:
            value = await self._inverter.read_setting(setting_id)
        except Exception as exc:
            _LOGGER.debug("GoodWe %s read unavailable: %s", setting_id, exc)
            return None
        try:
            # 47511/47512 are unsigned; some library versions decode them as
            # signed 16-bit integers.
            return int(value) & 0xFFFF
        except (TypeError, ValueError, OverflowError):
            return None

    async def _write_ems(self, mode: int, power_w: int) -> None:
        """Write EMS mode and power set point.

        The protocol documents function 0x10 for writes. Write both adjacent
        registers in one 0x10 frame when the library exposes it; otherwise
        write the set point first so the mode never runs on a stale target.
        """
        power_w = max(0, min(0xFFFF, int(power_w)))
        multi = getattr(self._inverter, "_write_multi_command", None)
        send = getattr(self._inverter, "_read_from_socket", None)
        if callable(multi) and callable(send):
            payload = int(mode).to_bytes(2, "big") + power_w.to_bytes(2, "big")
            await send(multi(_EMS_MODE_REGISTER, payload))
            return
        await self._inverter.write_setting("ems_power_limit", power_w)
        await self._inverter.write_setting("ems_mode", int(mode))

    async def _verify_ems(self, expected_mode: int, expected_power: int) -> bool:
        actual_mode = await self._read_ems_setting("ems_mode")
        actual_power = await self._read_ems_setting("ems_power_limit")
        if actual_mode == expected_mode and actual_power == expected_power:
            return True
        _LOGGER.warning(
            "GoodWe EMS write is not confirmed: expected mode=%d, set=%dW; "
            "read mode=%s, set=%sW",
            expected_mode,
            expected_power,
            actual_mode,
            actual_power,
        )
        return False

    async def _curtail_ems_load_following(self) -> bool:
        """Hold grid exchange at 0 W with native EMS BuyPower mode."""
        if not (
            self._grid_export_state_saved
            and self._saved_method == CURTAILMENT_METHOD_EMS_LOAD_FOLLOWING
        ):
            fallback = await self._read_register(_EMS_FALLBACK_REGISTER)
            if fallback != _EMS_FALLBACK_KEEP_LAST_MODE:
                _LOGGER.error(
                    "GoodWe load-following curtailment refused: EMS fallback "
                    "(47505) reads %s, expected %d (keep last mode). Another "
                    "fallback would stop the inverter or drop the curtailment "
                    "between checks; PowerSync will not change it",
                    fallback,
                    _EMS_FALLBACK_KEEP_LAST_MODE,
                )
                return False
            mode = await self._read_ems_setting("ems_mode")
            power = await self._read_ems_setting("ems_power_limit")
            if mode is None or power is None:
                _LOGGER.error(
                    "GoodWe load-following curtailment refused: EMS mode/set "
                    "point (47511/47512) could not be read"
                )
                return False
            if mode == _EMS_MODE_STOPPED or mode not in _EMS_DOCUMENTED_MODES:
                # 0 means the EMS mode was never set; stopped/unknown values
                # are not a running baseline. Self-use is the normal state.
                mode = _EMS_MODE_AUTO
            elif mode == _EMS_MODE_BUY_POWER and power == 0:
                # Indistinguishable from a hold left by an interrupted run.
                # Never make it the "original" setting, or restore would
                # keep curtailing forever.
                _LOGGER.warning(
                    "GoodWe EMS was already in BuyPower 0 W before curtailment; "
                    "it will be restored to self-use"
                )
                mode = _EMS_MODE_AUTO
            self._saved_ems_mode = mode
            self._saved_ems_power_limit = power
            self._saved_method = CURTAILMENT_METHOD_EMS_LOAD_FOLLOWING
            self._grid_export_state_saved = True

        await self._write_ems(_EMS_MODE_BUY_POWER, 0)
        if not await self._verify_ems(_EMS_MODE_BUY_POWER, 0):
            return False
        _LOGGER.info(
            "GoodWe curtailed: EMS BuyPower load following at 0 W grid exchange"
        )
        return True

    async def _restore_ems_load_following(self) -> bool:
        """Return EMS mode and set point to the saved baseline."""
        restore_mode = self._saved_ems_mode or _EMS_MODE_AUTO
        restore_power = self._saved_ems_power_limit
        current_mode = await self._read_ems_setting("ems_mode")
        current_power = await self._read_ems_setting("ems_power_limit")
        if current_mode is None:
            _LOGGER.warning("GoodWe EMS restore deferred: EMS mode unreadable")
            return False
        if not (current_mode == _EMS_MODE_BUY_POWER and current_power == 0):
            # Something else (SEMS, an EMS entity command or a force mode)
            # has already replaced PowerSync's hold. Do not overwrite it.
            _LOGGER.info(
                "GoodWe EMS restore: hold already replaced (mode=%s, set=%sW); "
                "leaving the current EMS setting in place",
                current_mode,
                current_power,
            )
        else:
            if restore_power is None:
                restore_power = 0
            await self._write_ems(restore_mode, restore_power)
            if not await self._verify_ems(restore_mode, restore_power):
                return False
            _LOGGER.info(
                "GoodWe restore: EMS mode restored to %d (set=%dW)",
                restore_mode,
                restore_power,
            )
        self._saved_ems_mode = None
        self._saved_ems_power_limit = None
        self._saved_method = None
        self._grid_export_state_saved = False
        return True

    async def release_ems_hold(self) -> bool:
        """Release a load-following EMS hold before another mode command."""
        if not (
            self._grid_export_state_saved
            and self._saved_method == CURTAILMENT_METHOD_EMS_LOAD_FOLLOWING
        ):
            return True
        try:
            return await self._restore_ems_load_following()
        except Exception as exc:
            _LOGGER.error("GoodWe EMS hold release failed: %s", exc)
            return False

    async def curtail(self) -> bool:
        """Curtail grid export with the configured native GoodWe method."""
        try:
            if not await self.connect():
                return False
            if self.uses_ems_load_following:
                return await self._curtail_ems_load_following()
            await self._save_grid_export_state()
            if not await self._write_grid_export_enabled(1):
                return False
            await self._inverter.set_grid_export_limit(0)
            if not await self._verify_grid_export_settings(1, 0):
                return False
            _LOGGER.info("GoodWe curtailed: export limit enabled and set to 0W")
            return True
        except Exception as e:
            _LOGGER.error("GoodWe curtail() failed: %s", e)
            return False

    async def restore(self, *, allow_zero_export_limit: bool = True) -> bool:
        """Remove export limit to restore normal grid export."""
        try:
            if not await self.connect():
                return False
            if self.uses_ems_load_following:
                # BuyPower never touched the export limiter; leave it as set.
                return await self._restore_ems_load_following()
            restore_limit = (
                self._saved_grid_export_limit
                if self._saved_grid_export_limit is not None
                else _GOODWE_EXPORT_LIMIT_MAX_W
            )
            restore_enabled = (
                self._saved_grid_export_enabled
                if self._saved_grid_export_enabled is not None
                else 0
            )
            if (
                not allow_zero_export_limit
                and restore_enabled
                and restore_limit <= 0
            ):
                _LOGGER.info(
                    "GoodWe restore: saved export limit is 0W; disabling limiter "
                    "for active export command"
                )
                restore_limit = _GOODWE_EXPORT_LIMIT_MAX_W
                restore_enabled = 0
            await self._inverter.set_grid_export_limit(restore_limit)
            if not await self._write_grid_export_enabled(restore_enabled):
                return False
            if not await self._verify_grid_export_settings(
                restore_enabled, restore_limit
            ):
                return False
            self._saved_grid_export_enabled = None
            self._saved_grid_export_limit = None
            self._saved_method = None
            self._grid_export_state_saved = False
            _LOGGER.info(
                "GoodWe restore: export limit restored to %dW (enabled=%d)",
                restore_limit,
                restore_enabled,
            )
            return True
        except Exception as e:
            _LOGGER.error("GoodWe restore() failed: %s", e)
            return False

    async def disconnect(self) -> None:
        """No persistent connection to close (UDP is stateless)."""
        self._inverter = None
