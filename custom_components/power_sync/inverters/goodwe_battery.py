"""GoodWe battery controller using the goodwe PyPI library.

Supports ET/EH/BT/BH and ES/EM/BP series hybrid inverters.
Uses the goodwe library for auto-detection, protocol handling, and battery control.

Separate from inverters/goodwe.py which handles AC-coupled curtailment via pymodbus.
"""
import asyncio
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)


class GoodWeBatteryController:
    """Battery controller for GoodWe hybrid inverters."""

    def __init__(self, host: str, port: int = 8899, comm_addr: int = 0):
        self.host = host
        self.port = port
        self.comm_addr = comm_addr
        self._inverter = None  # goodwe.Inverter instance
        self._lock = asyncio.Lock()

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

        # GoodWe pbattery1: positive=charge, negative=discharge
        # PowerSync battery_power: positive=discharge, negative=charge
        bat_w = data.get("pbattery1", 0) or 0
        battery_kw = -(bat_w / 1000.0)  # Negate

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

        await self._inverter.set_operation_mode(
            goodwe.OperationMode.ECO_CHARGE,
            eco_mode_power=power_pct,
            eco_mode_soc=soc_target,
        )
        _LOGGER.info("GoodWe force charge: power=%d%%, target_soc=%d%%", power_pct, soc_target)
        return True

    async def force_discharge(self, power_pct: int = 100, soc_floor: int = 10) -> bool:
        """Force discharge to grid using ECO_DISCHARGE mode."""
        import goodwe

        await self._inverter.set_operation_mode(
            goodwe.OperationMode.ECO_DISCHARGE,
            eco_mode_power=power_pct,
            eco_mode_soc=soc_floor,
        )
        _LOGGER.info("GoodWe force discharge: power=%d%%, floor_soc=%d%%", power_pct, soc_floor)
        return True

    async def restore_normal(self) -> bool:
        """Restore to normal (GENERAL) operation mode."""
        import goodwe

        await self._inverter.set_operation_mode(goodwe.OperationMode.GENERAL)
        _LOGGER.info("GoodWe restored to GENERAL mode")
        return True

    async def set_backup_reserve(self, percent: int) -> bool:
        """Set backup reserve (minimum SOC).

        GoodWe uses DOD (depth of discharge) which is inverse:
        DOD = 100 - reserve_percent
        """
        dod = max(0, min(89, 100 - percent))  # ET max DOD is 89%
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

    async def disconnect(self) -> None:
        """No persistent connection to close (UDP is stateless)."""
        self._inverter = None
