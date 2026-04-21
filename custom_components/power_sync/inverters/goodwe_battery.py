"""GoodWe battery controller using the goodwe PyPI library.

Supports ET/EH/BT/BH and ES/EM/BP series hybrid inverters.
Uses the goodwe library for auto-detection, protocol handling, and battery control.

Separate from inverters/goodwe.py which handles AC-coupled curtailment via pymodbus.
"""
import asyncio
import logging
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)

_GOODWE_UDP_PORT = 8899
_UDP_FAILURE_CACHE_SECONDS = 300  # re-try UDP no more than once every 5 min


class GoodWeBatteryController:
    """Battery controller for GoodWe hybrid inverters."""

    def __init__(self, host: str, port: int = 8899, comm_addr: int = 0):
        self.host = host
        self.port = port
        self.comm_addr = comm_addr
        self._inverter = None  # goodwe.Inverter instance (data / TCP)
        self._lock = asyncio.Lock()
        self._udp_unavailable_until: float = 0.0  # epoch time; 0 = never tried

    async def connect(self) -> bool:
        """Connect to inverter and auto-detect model family."""
        import goodwe

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
                "is reachable from Home Assistant at %s.",
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
                "from Home Assistant for force charge/discharge to work.",
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
                    "from Home Assistant. Force charge/discharge will not work until "
                    "this is resolved.",
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
        await self._inverter.set_grid_export_limit(watts)
        _LOGGER.info("GoodWe export limit set to %dW", watts)
        return True

    async def curtail(self) -> bool:
        """Zero-export curtailment: block all grid export via export limit register."""
        try:
            if not await self.connect():
                return False
            await self._inverter.set_grid_export_limit(0)
            _LOGGER.info("GoodWe curtailed: export limit set to 0W")
            return True
        except Exception as e:
            _LOGGER.error("GoodWe curtail() failed: %s", e)
            return False

    async def restore(self) -> bool:
        """Remove export limit to restore normal grid export."""
        try:
            if not await self.connect():
                return False
            # 99999W effectively removes the limit for any consumer-grade inverter
            await self._inverter.set_grid_export_limit(99999)
            _LOGGER.info("GoodWe restore: export limit removed")
            return True
        except Exception as e:
            _LOGGER.error("GoodWe restore() failed: %s", e)
            return False

    async def disconnect(self) -> None:
        """No persistent connection to close (UDP is stateless)."""
        self._inverter = None
