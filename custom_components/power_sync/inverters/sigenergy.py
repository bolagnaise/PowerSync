"""Sigenergy inverter controller via Modbus TCP.

Supports Sigenergy hybrid inverter systems for DC solar curtailment.
Uses the plant-level PV power limit and active power percentage registers.

Reference: https://github.com/TypQxQ/Sigenergy-Local-Modbus
"""
import asyncio
import logging
from typing import Optional

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from .base import InverterController, InverterState, InverterStatus

_LOGGER = logging.getLogger(__name__)


class SigenergyController(InverterController):
    """Controller for Sigenergy hybrid inverter systems via Modbus TCP.

    Uses Modbus TCP to communicate directly with the Sigenergy system
    for DC solar curtailment control.
    """

    # Modbus register addresses (documentation addresses - 40001 for pymodbus)
    # Holding registers (read/write) - base 40001
    # Register 40036 → pymodbus address 35
    REG_PV_MAX_POWER_LIMIT = 35           # 40036 - PV max power limit (U32, gain 1000, kW)
    REG_ACTIVE_POWER_PCT_TARGET = 4       # 40005 - Active power % target (S16, gain 100)
    REG_ACTIVE_POWER_FIXED_TARGET = 0     # 40001 - Active power fixed target (S32, gain 1000, kW)
    REG_GRID_EXPORT_LIMIT = 37            # 40038 - Grid export limit (U32, gain 1000)
    REG_PCS_EXPORT_LIMIT = 41             # 40042 - PCS export limit (U32, gain 1000)
    REG_ESS_MAX_CHARGE_LIMIT = 31         # 40032 - ESS max charging (U32, gain 1000, kW)
    REG_ESS_MAX_DISCHARGE_LIMIT = 33      # 40034 - ESS max discharging (U32, gain 1000, kW)

    # Input registers (read-only) - base 30001
    # Register 30035 → pymodbus address 34
    REG_PV_POWER = 34                     # 30035 - PV power (S32, gain 1000, kW)
    REG_ACTIVE_POWER = 30                 # 30031 - Active power (S32, gain 1000, kW)
    REG_ESS_SOC = 13                      # 30014 - Battery SOC (U16, gain 10, %)
    REG_RUNNING_STATE = 50                # 30051 - Plant running state (U16)
    REG_GRID_SENSOR_POWER = 4             # 30005 - Grid sensor active power (S32, gain 1000, kW)
    REG_EMS_WORK_MODE = 2                 # 30003 - EMS work mode (U16)

    # Constants
    GAIN_POWER = 1000  # kW → scaled value (multiply to write, divide to read)
    GAIN_PERCENT = 100  # % → scaled value
    GAIN_SOC = 10      # % → scaled value

    # Curtailment values
    # Use export limit (load-following) rather than full PV shutdown
    # This allows solar to continue powering house and charging battery
    EXPORT_LIMIT_ZERO = 0         # Zero export (load-following mode)
    EXPORT_LIMIT_UNLIMITED = 0xFFFFFFFE  # Unlimited export (normal operation)
    PV_POWER_LIMIT_ZERO = 0       # Set PV limit to 0 kW (full shutdown - not used)
    ACTIVE_POWER_PCT_ZERO = 0     # 0% active power

    # Default Modbus settings
    DEFAULT_PORT = 502
    DEFAULT_SLAVE_ID = 1
    TIMEOUT_SECONDS = 10.0

    def __init__(
        self,
        host: str,
        port: int = 502,
        slave_id: int = 1,
        model: Optional[str] = None,
    ):
        """Initialize Sigenergy controller.

        Args:
            host: IP address of Sigenergy system
            port: Modbus TCP port (default: 502)
            slave_id: Modbus slave ID (default: 1)
            model: Sigenergy model (optional)
        """
        super().__init__(host, port, slave_id, model)
        self._client: Optional[AsyncModbusTcpClient] = None
        self._lock = asyncio.Lock()
        self._original_pv_limit: Optional[int] = None  # Store original limit for restore

    async def connect(self) -> bool:
        """Connect to the Sigenergy system via Modbus TCP."""
        async with self._lock:
            try:
                if self._client and self._client.connected:
                    return True

                self._client = AsyncModbusTcpClient(
                    host=self.host,
                    port=self.port,
                    timeout=self.TIMEOUT_SECONDS,
                )

                connected = await self._client.connect()
                if connected:
                    self._connected = True
                    _LOGGER.info(f"Connected to Sigenergy system at {self.host}:{self.port}")
                else:
                    _LOGGER.error(f"Failed to connect to Sigenergy at {self.host}:{self.port}")

                return connected

            except Exception as e:
                _LOGGER.error(f"Error connecting to Sigenergy: {e}")
                self._connected = False
                return False

    async def disconnect(self) -> None:
        """Disconnect from the Sigenergy system."""
        async with self._lock:
            if self._client:
                self._client.close()
                self._client = None
            self._connected = False
            _LOGGER.debug(f"Disconnected from Sigenergy at {self.host}")

    async def _write_holding_registers(self, address: int, values: list[int]) -> bool:
        """Write values to holding registers.

        Args:
            address: Starting register address (0-indexed)
            values: List of values to write

        Returns:
            True if write successful
        """
        if not self._client or not self._client.connected:
            if not await self.connect():
                return False

        try:
            result = await self._client.write_registers(
                address=address,
                values=values,
                slave=self.slave_id,
            )

            if result.isError():
                _LOGGER.error(f"Modbus write error at register {address}: {result}")
                return False

            _LOGGER.debug(f"Successfully wrote {values} to register {address}")
            return True

        except ModbusException as e:
            _LOGGER.error(f"Modbus exception writing to register {address}: {e}")
            return False
        except Exception as e:
            _LOGGER.error(f"Error writing to register {address}: {e}")
            return False

    async def _read_holding_registers(self, address: int, count: int = 1) -> Optional[list]:
        """Read values from holding registers.

        Args:
            address: Starting register address (0-indexed)
            count: Number of registers to read

        Returns:
            List of register values or None on error
        """
        if not self._client or not self._client.connected:
            if not await self.connect():
                return None

        try:
            result = await self._client.read_holding_registers(
                address=address,
                count=count,
                slave=self.slave_id,
            )

            if result.isError():
                _LOGGER.debug(f"Modbus read error at holding register {address}: {result}")
                return None

            return result.registers

        except ModbusException as e:
            _LOGGER.debug(f"Modbus exception reading holding register {address}: {e}")
            return None
        except Exception as e:
            _LOGGER.debug(f"Error reading holding register {address}: {e}")
            return None

    async def _read_input_registers(self, address: int, count: int = 1) -> Optional[list]:
        """Read values from input registers.

        Args:
            address: Starting register address (0-indexed)
            count: Number of registers to read

        Returns:
            List of register values or None on error
        """
        if not self._client or not self._client.connected:
            if not await self.connect():
                return None

        try:
            result = await self._client.read_input_registers(
                address=address,
                count=count,
                slave=self.slave_id,
            )

            if result.isError():
                _LOGGER.debug(f"Modbus read error at input register {address}: {result}")
                return None

            return result.registers

        except ModbusException as e:
            _LOGGER.debug(f"Modbus exception reading input register {address}: {e}")
            return None
        except Exception as e:
            _LOGGER.debug(f"Error reading input register {address}: {e}")
            return None

    def _to_signed32(self, high: int, low: int) -> int:
        """Convert two unsigned 16-bit registers to signed 32-bit."""
        value = (high << 16) | low
        if value >= 0x80000000:
            value -= 0x100000000
        return value

    def _to_unsigned32(self, high: int, low: int) -> int:
        """Convert two unsigned 16-bit registers to unsigned 32-bit."""
        return (high << 16) | low

    def _from_unsigned32(self, value: int) -> list[int]:
        """Convert unsigned 32-bit to two 16-bit registers [high, low]."""
        high = (value >> 16) & 0xFFFF
        low = value & 0xFFFF
        return [high, low]

    async def _get_current_pv_limit(self) -> Optional[int]:
        """Read current PV power limit."""
        regs = await self._read_holding_registers(self.REG_PV_MAX_POWER_LIMIT, 2)
        if regs and len(regs) >= 2:
            return self._to_unsigned32(regs[0], regs[1])
        return None

    async def _get_current_export_limit(self) -> Optional[int]:
        """Read current grid export limit."""
        regs = await self._read_holding_registers(self.REG_GRID_EXPORT_LIMIT, 2)
        if regs and len(regs) >= 2:
            return self._to_unsigned32(regs[0], regs[1])
        return None

    async def curtail(
        self,
        home_load_w: Optional[float] = None,
        rated_capacity_w: Optional[float] = None,
    ) -> bool:
        """Curtail solar export using load-following mode.

        If home_load_w is provided, sets export limit to match home load.
        Otherwise sets export limit to 0 kW (zero export mode).

        Both modes allow solar to power the house and charge the battery,
        which is better than full PV shutdown.

        Args:
            home_load_w: Optional home load in watts for load-following mode
            rated_capacity_w: Inverter rated capacity (unused, for interface compat)

        Returns:
            True if curtailment successful
        """
        try:
            if not await self.connect():
                _LOGGER.error("Cannot curtail: failed to connect to Sigenergy")
                return False

            # Store original export limit if not already stored
            if self._original_pv_limit is None:
                self._original_pv_limit = await self._get_current_export_limit()
                if self._original_pv_limit is not None:
                    limit_str = f"{self._original_pv_limit / self.GAIN_POWER} kW" if self._original_pv_limit < self.EXPORT_LIMIT_UNLIMITED else "unlimited"
                    _LOGGER.info(f"Stored original export limit: {limit_str}")

            # Determine export limit
            if home_load_w is not None and home_load_w > 0:
                # Load-following mode: limit export to home load
                export_limit_kw = max(0.1, home_load_w / 1000)  # Minimum 0.1 kW
                _LOGGER.info(f"Curtailing Sigenergy at {self.host} (load-following: {export_limit_kw:.1f}kW = {home_load_w}W home load)")
            else:
                # Zero export mode
                export_limit_kw = 0
                _LOGGER.info(f"Curtailing Sigenergy at {self.host} (zero export mode)")

            # Set the export limit
            scaled_value = int(export_limit_kw * self.GAIN_POWER)
            values = self._from_unsigned32(scaled_value)
            success = await self._write_holding_registers(self.REG_GRID_EXPORT_LIMIT, values)

            if success:
                if export_limit_kw > 0:
                    _LOGGER.info(f"Successfully set load-following mode ({export_limit_kw:.1f}kW) on Sigenergy")
                else:
                    _LOGGER.info(f"Successfully set zero export mode on Sigenergy")
                # Brief delay then verify
                await asyncio.sleep(1)
                state = await self.get_status()
                if state.is_curtailed:
                    _LOGGER.info("Curtailment verified - load-following active")
                else:
                    _LOGGER.warning("Curtailment command sent but verification pending")
            else:
                _LOGGER.error(f"Failed to curtail Sigenergy at {self.host}")

            return success

        except Exception as e:
            _LOGGER.error(f"Error curtailing Sigenergy: {e}")
            return False

    async def restore(self) -> bool:
        """Restore normal export operation.

        Restores grid export limit to the original value or unlimited.

        Returns:
            True if restore successful
        """
        _LOGGER.info(f"Restoring Sigenergy export at {self.host}")

        try:
            if not await self.connect():
                _LOGGER.error("Cannot restore: failed to connect to Sigenergy")
                return False

            # Use stored original limit or set to unlimited
            restore_value = self._original_pv_limit if self._original_pv_limit else self.EXPORT_LIMIT_UNLIMITED
            limit_str = f"{restore_value / self.GAIN_POWER} kW" if restore_value < self.EXPORT_LIMIT_UNLIMITED else "unlimited"
            _LOGGER.info(f"Restoring export limit to: {limit_str}")

            values = self._from_unsigned32(restore_value)
            success = await self._write_holding_registers(self.REG_GRID_EXPORT_LIMIT, values)

            if success:
                _LOGGER.info(f"Successfully restored Sigenergy export at {self.host}")
                # Clear stored limit after successful restore
                self._original_pv_limit = None
                # Brief delay then verify
                await asyncio.sleep(1)
                state = await self.get_status()
                if not state.is_curtailed:
                    _LOGGER.info("Restore verified - normal export resumed")
                else:
                    _LOGGER.warning("Restore command sent but may take time to resume")
            else:
                _LOGGER.error(f"Failed to restore Sigenergy at {self.host}")

            return success

        except Exception as e:
            _LOGGER.error(f"Error restoring Sigenergy: {e}")
            return False

    async def get_status(self) -> InverterState:
        """Get current status of the Sigenergy system.

        Returns:
            InverterState with current status and power readings
        """
        try:
            if not await self.connect():
                return InverterState(
                    status=InverterStatus.OFFLINE,
                    is_curtailed=False,
                    error_message="Failed to connect to Sigenergy",
                )

            attrs = {}

            # Read PV power (S32, 2 registers)
            pv_power_regs = await self._read_input_registers(self.REG_PV_POWER, 2)
            pv_power_w = None
            if pv_power_regs and len(pv_power_regs) >= 2:
                pv_power_kw = self._to_signed32(pv_power_regs[0], pv_power_regs[1]) / self.GAIN_POWER
                pv_power_w = pv_power_kw * 1000
                attrs["pv_power_kw"] = round(pv_power_kw, 2)

            # Read active power (S32, 2 registers)
            active_power_regs = await self._read_input_registers(self.REG_ACTIVE_POWER, 2)
            if active_power_regs and len(active_power_regs) >= 2:
                active_power_kw = self._to_signed32(active_power_regs[0], active_power_regs[1]) / self.GAIN_POWER
                attrs["active_power_kw"] = round(active_power_kw, 2)

            # Read grid sensor power (S32, 2 registers)
            grid_power_regs = await self._read_input_registers(self.REG_GRID_SENSOR_POWER, 2)
            if grid_power_regs and len(grid_power_regs) >= 2:
                grid_power_kw = self._to_signed32(grid_power_regs[0], grid_power_regs[1]) / self.GAIN_POWER
                attrs["grid_power_kw"] = round(grid_power_kw, 2)

            # Read battery SOC (U16)
            soc_regs = await self._read_input_registers(self.REG_ESS_SOC, 1)
            if soc_regs:
                attrs["battery_soc"] = round(soc_regs[0] / self.GAIN_SOC, 1)

            # Read EMS work mode (U16)
            ems_regs = await self._read_input_registers(self.REG_EMS_WORK_MODE, 1)
            if ems_regs:
                attrs["ems_work_mode"] = ems_regs[0]

            # Read running state (U16)
            state_regs = await self._read_input_registers(self.REG_RUNNING_STATE, 1)
            if state_regs:
                attrs["running_state"] = state_regs[0]

            # Read current export limit to determine curtailment status (load-following mode)
            export_limit_regs = await self._read_holding_registers(self.REG_GRID_EXPORT_LIMIT, 2)
            export_limit = None
            is_curtailed = False
            if export_limit_regs and len(export_limit_regs) >= 2:
                export_limit = self._to_unsigned32(export_limit_regs[0], export_limit_regs[1])
                # Curtailed (load-following) if export limit is 0 or very low
                is_curtailed = export_limit < 100  # Less than 0.1 kW threshold
                if export_limit < self.EXPORT_LIMIT_UNLIMITED:
                    attrs["export_limit_kw"] = round(export_limit / self.GAIN_POWER, 2)
                else:
                    attrs["export_limit_kw"] = "unlimited"

            # Also read PV limit for reference
            pv_limit_regs = await self._read_holding_registers(self.REG_PV_MAX_POWER_LIMIT, 2)
            if pv_limit_regs and len(pv_limit_regs) >= 2:
                pv_limit = self._to_unsigned32(pv_limit_regs[0], pv_limit_regs[1])
                if pv_limit < self.EXPORT_LIMIT_UNLIMITED:
                    attrs["pv_power_limit_kw"] = round(pv_limit / self.GAIN_POWER, 2)
                else:
                    attrs["pv_power_limit_kw"] = "unlimited"

            # Determine overall status
            if is_curtailed:
                status = InverterStatus.CURTAILED
                attrs["curtailment_mode"] = "load_following"
            elif pv_power_w is not None and pv_power_w > 0:
                status = InverterStatus.ONLINE
            else:
                status = InverterStatus.ONLINE  # Connected but no PV production

            # Add model info
            attrs["model"] = self.model or "Sigenergy"
            attrs["host"] = self.host

            # In load-following mode, PV is not limited - only export is
            # So power_limit_percent is always 100 (export limit doesn't reduce PV)
            self._last_state = InverterState(
                status=status,
                is_curtailed=is_curtailed,
                power_output_w=pv_power_w,
                power_limit_percent=100,  # Load-following doesn't limit PV power
                attributes=attrs,
            )

            return self._last_state

        except Exception as e:
            _LOGGER.error(f"Error getting Sigenergy status: {e}")
            return InverterState(
                status=InverterStatus.ERROR,
                is_curtailed=False,
                error_message=str(e),
            )

    async def set_pv_power_limit(self, limit_kw: float) -> bool:
        """Set a specific PV power limit.

        Args:
            limit_kw: Power limit in kW (0 = curtail, very high = no limit)

        Returns:
            True if successful
        """
        try:
            if not await self.connect():
                return False

            # Convert kW to scaled value (multiply by gain)
            scaled_value = int(limit_kw * self.GAIN_POWER)
            if scaled_value < 0:
                scaled_value = 0
            if scaled_value > 0xFFFFFFFE:
                scaled_value = 0xFFFFFFFE  # Max valid value

            _LOGGER.info(f"Setting Sigenergy PV limit to {limit_kw} kW")
            values = self._from_unsigned32(scaled_value)
            return await self._write_holding_registers(self.REG_PV_MAX_POWER_LIMIT, values)

        except Exception as e:
            _LOGGER.error(f"Error setting PV power limit: {e}")
            return False

    async def set_export_limit(self, limit_kw: float) -> bool:
        """Set a specific grid export limit.

        Args:
            limit_kw: Export limit in kW (0 = no export)

        Returns:
            True if successful
        """
        try:
            if not await self.connect():
                return False

            scaled_value = int(limit_kw * self.GAIN_POWER)
            if scaled_value < 0:
                scaled_value = 0
            if scaled_value > 0xFFFFFFFE:
                scaled_value = 0xFFFFFFFE

            _LOGGER.info(f"Setting Sigenergy export limit to {limit_kw} kW")
            values = self._from_unsigned32(scaled_value)
            return await self._write_holding_registers(self.REG_GRID_EXPORT_LIMIT, values)

        except Exception as e:
            _LOGGER.error(f"Error setting export limit: {e}")
            return False

    async def __aenter__(self):
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.disconnect()
