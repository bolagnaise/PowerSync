"""Sungrow SH series (hybrid) inverter controller via Modbus TCP.

Supports Sungrow SH series hybrid inverters (SH5.0RT, SH10RT, etc.)
connected via internal LAN port or WiNet-S dongle.

Reference: https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant
"""
import asyncio
import logging
from typing import Optional

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from .base import InverterController, InverterState, InverterStatus

_LOGGER = logging.getLogger(__name__)


class SungrowSHController(InverterController):
    """Controller for Sungrow SH series hybrid inverters via Modbus TCP.

    Uses Modbus TCP to communicate with the inverter through
    the internal LAN port or WiNet-S WiFi/Ethernet dongle.

    SH series uses different register addresses than SG series:
    - System state control: Register 13000 (vs 5006 for SG)
    - Same values: 0xCE=stop, 0xCF=start
    """

    # Modbus register addresses (0-indexed for pymodbus)
    # Documentation register - 1 = pymodbus address
    REGISTER_SYSTEM_STATE = 12999      # 13000 - System state control

    # System state control values
    STATE_STOP = 0xCE   # 206 - Stop inverter
    STATE_START = 0xCF  # 207 - Start inverter

    # Running state register and values
    REGISTER_RUNNING_STATE = 12999     # 13000 - Running state
    RUNNING_STATE_STOP = 0x8000
    RUNNING_STATE_STANDBY = 0x1400
    RUNNING_STATE_RUNNING = 0x0002
    RUNNING_STATE_FAULT = 0x1300

    # ===== SH Series Register Addresses (0-indexed) =====
    # PV Generation
    REG_DAILY_PV = 13000               # 13001 - Daily PV generation (kWh * 0.1)
    REG_TOTAL_PV = 13001               # 13002-13003 - Total PV generation (kWh * 0.1, U32)

    # Power readings
    REG_LOAD_POWER = 13006             # 13007-13008 - Load power (W, I32)
    REG_EXPORT_POWER = 13008           # 13009-13010 - Export power (W, I32)
    REG_TOTAL_ACTIVE_POWER = 13032     # 13033-13034 - Total active power (W, I32)

    # Battery
    REG_BATTERY_VOLTAGE = 13018        # 13019 - Battery voltage (V * 0.1)
    REG_BATTERY_CURRENT = 13019        # 13020 - Battery current (A * 0.1, signed)
    REG_BATTERY_POWER = 13020          # 13021 - Battery power (W, signed)
    REG_BATTERY_LEVEL = 13021          # 13022 - Battery level (% * 0.1)
    REG_BATTERY_SOH = 13022            # 13023 - Battery state of health (% * 0.1)
    REG_BATTERY_TEMP = 13023           # 13024 - Battery temperature (°C * 0.1, signed)
    REG_DAILY_BATTERY_DISCHARGE = 13024  # 13025 - Daily battery discharge (kWh * 0.1)
    REG_DAILY_BATTERY_CHARGE = 13038   # 13039 - Daily battery charge (kWh * 0.1)

    # Energy accounting
    REG_DAILY_IMPORT = 13034           # 13035 - Daily imported energy (kWh * 0.1)
    REG_DAILY_EXPORT = 13043           # 13044 - Daily exported energy (kWh * 0.1)

    # Temperature
    REG_INVERTER_TEMP = 5006           # 5007 - Inverter temperature (°C * 0.1, signed)

    # Grid
    REG_GRID_FREQUENCY = 5035          # 5036 - Grid frequency (Hz * 0.1)
    REG_PHASE_A_VOLTAGE = 5018         # 5019 - Phase A voltage (V * 0.1)

    # Timeout for Modbus operations
    TIMEOUT_SECONDS = 10.0

    def __init__(
        self,
        host: str,
        port: int = 502,
        slave_id: int = 1,
        model: Optional[str] = None,
    ):
        """Initialize Sungrow SH controller.

        Args:
            host: IP address of inverter LAN port or WiNet-S dongle
            port: Modbus TCP port (default: 502)
            slave_id: Modbus slave ID (default: 1)
            model: Sungrow model (e.g., 'sh10rt')
        """
        super().__init__(host, port, slave_id, model)
        self._client: Optional[AsyncModbusTcpClient] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> bool:
        """Connect to the Sungrow SH inverter via Modbus TCP."""
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
                    _LOGGER.info(f"Connected to Sungrow SH inverter at {self.host}:{self.port}")
                else:
                    _LOGGER.error(f"Failed to connect to Sungrow SH inverter at {self.host}:{self.port}")

                return connected

            except Exception as e:
                _LOGGER.error(f"Error connecting to Sungrow SH inverter: {e}")
                self._connected = False
                return False

    async def disconnect(self) -> None:
        """Disconnect from the Sungrow SH inverter."""
        async with self._lock:
            if self._client:
                self._client.close()
                self._client = None
            self._connected = False
            _LOGGER.debug(f"Disconnected from Sungrow SH inverter at {self.host}")

    async def _write_register(self, address: int, value: int) -> bool:
        """Write a value to a Modbus register.

        Args:
            address: Register address (0-indexed)
            value: Value to write

        Returns:
            True if write successful, False otherwise
        """
        if not self._client or not self._client.connected:
            if not await self.connect():
                return False

        try:
            result = await self._client.write_register(
                address=address,
                value=value,
                slave=self.slave_id,
            )

            if result.isError():
                _LOGGER.error(f"Modbus write error at register {address}: {result}")
                return False

            _LOGGER.debug(f"Successfully wrote {value} (0x{value:02X}) to register {address}")
            return True

        except ModbusException as e:
            _LOGGER.error(f"Modbus exception writing to register {address}: {e}")
            return False
        except Exception as e:
            _LOGGER.error(f"Error writing to register {address}: {e}")
            return False

    async def _read_register(self, address: int, count: int = 1) -> Optional[list]:
        """Read values from Modbus registers.

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
                _LOGGER.debug(f"Modbus read error at register {address}: {result}")
                return None

            return result.registers

        except ModbusException as e:
            _LOGGER.debug(f"Modbus exception reading register {address}: {e}")
            return None
        except Exception as e:
            _LOGGER.debug(f"Error reading register {address}: {e}")
            return None

    def _to_signed16(self, value: int) -> int:
        """Convert unsigned 16-bit to signed."""
        if value >= 0x8000:
            return value - 0x10000
        return value

    def _to_signed32(self, high: int, low: int) -> int:
        """Convert two unsigned 16-bit registers to signed 32-bit."""
        value = (high << 16) | low
        if value >= 0x80000000:
            return value - 0x100000000
        return value

    def _to_unsigned32(self, high: int, low: int) -> int:
        """Convert two unsigned 16-bit registers to unsigned 32-bit."""
        return (high << 16) | low

    async def _read_all_registers(self) -> dict:
        """Read all SH series registers and return as attributes dict."""
        attrs = {}

        try:
            # Read battery registers (13019-13025) in one batch
            battery_regs = await self._read_register(self.REG_BATTERY_VOLTAGE, 7)
            if battery_regs and len(battery_regs) >= 7:
                attrs["battery_voltage"] = round(battery_regs[0] * 0.1, 1)
                attrs["battery_current"] = round(self._to_signed16(battery_regs[1]) * 0.1, 1)
                attrs["battery_power"] = self._to_signed16(battery_regs[2])
                attrs["battery_level"] = round(battery_regs[3] * 0.1, 1)
                attrs["battery_soh"] = round(battery_regs[4] * 0.1, 1)
                attrs["battery_temperature"] = round(self._to_signed16(battery_regs[5]) * 0.1, 1)
                attrs["daily_battery_discharge"] = round(battery_regs[6] * 0.1, 2)

            # Read daily PV generation
            daily_pv = await self._read_register(self.REG_DAILY_PV, 1)
            if daily_pv:
                attrs["daily_pv_generation"] = round(daily_pv[0] * 0.1, 2)

            # Read total PV generation (32-bit)
            total_pv = await self._read_register(self.REG_TOTAL_PV, 2)
            if total_pv and len(total_pv) >= 2:
                attrs["total_pv_generation"] = round(self._to_unsigned32(total_pv[0], total_pv[1]) * 0.1, 1)

            # Read load power (32-bit signed)
            load_power = await self._read_register(self.REG_LOAD_POWER, 2)
            if load_power and len(load_power) >= 2:
                attrs["load_power"] = self._to_signed32(load_power[0], load_power[1])

            # Read export power (32-bit signed)
            export_power = await self._read_register(self.REG_EXPORT_POWER, 2)
            if export_power and len(export_power) >= 2:
                attrs["export_power"] = self._to_signed32(export_power[0], export_power[1])

            # Read total active power (32-bit signed)
            active_power = await self._read_register(self.REG_TOTAL_ACTIVE_POWER, 2)
            if active_power and len(active_power) >= 2:
                attrs["active_power"] = self._to_signed32(active_power[0], active_power[1])

            # Read daily import/export
            daily_import = await self._read_register(self.REG_DAILY_IMPORT, 1)
            if daily_import:
                attrs["daily_import"] = round(daily_import[0] * 0.1, 2)

            daily_export = await self._read_register(self.REG_DAILY_EXPORT, 1)
            if daily_export:
                attrs["daily_export"] = round(daily_export[0] * 0.1, 2)

            # Read daily battery charge
            daily_charge = await self._read_register(self.REG_DAILY_BATTERY_CHARGE, 1)
            if daily_charge:
                attrs["daily_battery_charge"] = round(daily_charge[0] * 0.1, 2)

            # Read inverter temperature (from 5xxx range)
            inv_temp = await self._read_register(self.REG_INVERTER_TEMP, 1)
            if inv_temp:
                attrs["inverter_temperature"] = round(self._to_signed16(inv_temp[0]) * 0.1, 1)

            # Read grid frequency
            grid_freq = await self._read_register(self.REG_GRID_FREQUENCY, 1)
            if grid_freq:
                attrs["grid_frequency"] = round(grid_freq[0] * 0.1, 2)

            # Read phase A voltage
            voltage = await self._read_register(self.REG_PHASE_A_VOLTAGE, 1)
            if voltage:
                attrs["grid_voltage"] = round(voltage[0] * 0.1, 1)

        except Exception as e:
            _LOGGER.warning(f"Error reading some registers: {e}")

        return attrs

    async def curtail(self) -> bool:
        """Stop the Sungrow SH inverter to prevent solar export.

        Writes stop command (0xCE/206) to the system state register.

        Returns:
            True if curtailment successful
        """
        _LOGGER.info(f"Curtailing Sungrow SH inverter at {self.host} (stop mode)")

        try:
            # Ensure connected
            if not await self.connect():
                _LOGGER.error("Cannot curtail: failed to connect to inverter")
                return False

            # Write stop command to system state register
            success = await self._write_register(
                self.REGISTER_SYSTEM_STATE,
                self.STATE_STOP,
            )

            if success:
                _LOGGER.info(f"Successfully curtailed Sungrow SH inverter at {self.host}")
                # Brief delay for inverter to process
                await asyncio.sleep(1)
            else:
                _LOGGER.error(f"Failed to curtail Sungrow SH inverter at {self.host}")

            return success

        except Exception as e:
            _LOGGER.error(f"Error curtailing Sungrow SH inverter: {e}")
            return False

    async def restore(self) -> bool:
        """Restore normal operation of the Sungrow SH inverter.

        Writes start command (0xCF/207) to the system state register.

        Returns:
            True if restore successful
        """
        _LOGGER.info(f"Restoring Sungrow SH inverter at {self.host} to normal operation")

        try:
            # Ensure connected
            if not await self.connect():
                _LOGGER.error("Cannot restore: failed to connect to inverter")
                return False

            # Write start command to system state register
            success = await self._write_register(
                self.REGISTER_SYSTEM_STATE,
                self.STATE_START,
            )

            if success:
                _LOGGER.info(f"Successfully restored Sungrow SH inverter at {self.host}")
                # Brief delay for inverter to process
                await asyncio.sleep(1)
            else:
                _LOGGER.error(f"Failed to restore Sungrow SH inverter at {self.host}")

            return success

        except Exception as e:
            _LOGGER.error(f"Error restoring Sungrow SH inverter: {e}")
            return False

    async def get_status(self) -> InverterState:
        """Get current status of the Sungrow SH inverter.

        Returns:
            InverterState with current status and register attributes
        """
        try:
            # Ensure connected
            if not await self.connect():
                return InverterState(
                    status=InverterStatus.OFFLINE,
                    is_curtailed=False,
                    error_message="Failed to connect to inverter",
                )

            # Read all available registers
            attrs = await self._read_all_registers()

            # Determine status based on running state
            running_state = await self._read_register(self.REGISTER_RUNNING_STATE, 1)
            status = InverterStatus.ONLINE
            is_curtailed = False

            if running_state:
                state_value = running_state[0]
                if state_value == self.RUNNING_STATE_STOP:
                    status = InverterStatus.CURTAILED
                    is_curtailed = True
                elif state_value == self.RUNNING_STATE_FAULT:
                    status = InverterStatus.ERROR
                elif state_value == self.RUNNING_STATE_STANDBY:
                    status = InverterStatus.ONLINE
                    attrs["running_state"] = "standby"
                else:
                    attrs["running_state"] = "running"

            # Add model info
            attrs["model"] = self.model or "SH Series"
            attrs["host"] = self.host

            # Get power output from active_power if available
            power_output = attrs.get("active_power")

            self._last_state = InverterState(
                status=status,
                is_curtailed=is_curtailed,
                power_output_w=power_output,
                attributes=attrs,
            )

            return self._last_state

        except Exception as e:
            _LOGGER.error(f"Error getting Sungrow SH inverter status: {e}")
            return InverterState(
                status=InverterStatus.ERROR,
                is_curtailed=False,
                error_message=str(e),
            )

    async def __aenter__(self):
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.disconnect()
