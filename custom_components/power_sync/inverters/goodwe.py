"""GoodWe inverter controller via Modbus TCP.

Supports GoodWe ET/EH/BT/BH series hybrid inverters.
Uses export power limiting for load following curtailment.

Reference: https://github.com/marcelblijleven/goodwe
"""
import asyncio
import logging
from typing import Optional

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from .base import InverterController, InverterState, InverterStatus

_LOGGER = logging.getLogger(__name__)


class GoodWeController(InverterController):
    """Controller for GoodWe ET/EH/BT/BH series inverters via Modbus TCP.

    Uses Modbus TCP to communicate with the inverter through
    the built-in WiFi/LAN module on port 502.

    Supports load following curtailment via export power limiting.
    """

    # Modbus register addresses (hex -> decimal)
    # Export limiting registers
    REG_EXPORT_LIMIT_ENABLED = 47549   # 0xB9AD - Export limit enabled (1=on, 0=off)
    REG_EXPORT_LIMIT = 47550           # 0xB9AE - Export limit (W)

    # Power registers
    REG_PV1_VOLTAGE = 35103            # 0x891F - PV1 voltage (V * 0.1)
    REG_PV1_CURRENT = 35104            # 0x8920 - PV1 current (A * 0.1)
    REG_PV1_POWER = 35105              # 0x8921 - PV1 power (W, 32-bit)
    REG_PV2_VOLTAGE = 35107            # 0x8923 - PV2 voltage (V * 0.1)
    REG_PV2_CURRENT = 35108            # 0x8924 - PV2 current (A * 0.1)
    REG_PV2_POWER = 35109              # 0x8925 - PV2 power (W, 32-bit)

    REG_GRID_POWER = 35125             # 0x8935 - On-grid L1 power (W, signed)
    REG_TOTAL_POWER = 35138            # 0x8942 - Total inverter power (W)

    # Battery registers
    REG_BATTERY_VOLTAGE = 35180        # 0x9094 - Battery voltage (V * 0.1)
    REG_BATTERY_CURRENT = 35181        # 0x9095 - Battery current (A * 0.1, signed)
    REG_BATTERY_POWER = 35182          # 0x9096 - Battery power (W, 32-bit signed)
    REG_BATTERY_SOC = 37007            # 0x9D7F - Battery state of charge (%)

    # Temperature registers
    REG_TEMP_AIR = 35174               # 0x8A5E - Inverter temp air (°C * 0.1)
    REG_TEMP_MODULE = 35175            # 0x8A5F - Inverter temp module (°C * 0.1)

    # Energy registers
    REG_DAILY_PV = 35116               # 0x892C - Daily PV generation (kWh * 0.1)
    REG_TOTAL_PV = 35118               # 0x892E - Total PV generation (kWh * 0.1, 32-bit)
    REG_DAILY_EXPORT = 35142           # 0x8946 - Daily export (kWh * 0.1)
    REG_DAILY_IMPORT = 35144           # 0x8948 - Daily import (kWh * 0.1)

    # Status register
    REG_WORK_MODE = 35200              # 0x8980 - Work mode

    # Work mode values
    MODE_WAIT = 0
    MODE_NORMAL = 1
    MODE_FAULT = 2
    MODE_CHECK = 4

    # Timeout for Modbus operations
    TIMEOUT_SECONDS = 10.0

    def __init__(
        self,
        host: str,
        port: int = 502,
        slave_id: int = 247,  # GoodWe default is 247
        model: Optional[str] = None,
    ):
        """Initialize GoodWe controller.

        Args:
            host: IP address of GoodWe inverter
            port: Modbus TCP port (default: 502)
            slave_id: Modbus slave ID (default: 247 for GoodWe)
            model: GoodWe model (e.g., 'et', 'eh', 'bt', 'bh')
        """
        super().__init__(host, port, slave_id, model)
        self._client: Optional[AsyncModbusTcpClient] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> bool:
        """Connect to the GoodWe inverter via Modbus TCP."""
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
                    _LOGGER.info(f"Connected to GoodWe inverter at {self.host}:{self.port}")
                else:
                    _LOGGER.error(f"Failed to connect to GoodWe inverter at {self.host}:{self.port}")

                return connected

            except Exception as e:
                _LOGGER.error(f"Error connecting to GoodWe inverter: {e}")
                self._connected = False
                return False

    async def disconnect(self) -> None:
        """Disconnect from the GoodWe inverter."""
        async with self._lock:
            if self._client:
                self._client.close()
                self._client = None
            self._connected = False
            _LOGGER.debug(f"Disconnected from GoodWe inverter at {self.host}")

    async def _write_register(self, address: int, value: int) -> bool:
        """Write a value to a Modbus register."""
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

            _LOGGER.debug(f"Successfully wrote {value} to register {address}")
            return True

        except ModbusException as e:
            _LOGGER.error(f"Modbus exception writing to register {address}: {e}")
            return False
        except Exception as e:
            _LOGGER.error(f"Error writing to register {address}: {e}")
            return False

    async def _read_register(self, address: int, count: int = 1) -> Optional[list]:
        """Read values from Modbus registers."""
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

    async def curtail(
        self,
        home_load_w: Optional[float] = None,
        rated_capacity_w: Optional[float] = None,
    ) -> bool:
        """Enable load following curtailment on the GoodWe inverter.

        Sets export limit to 0W to enable load following mode,
        which allows self-consumption while preventing grid export.

        Returns:
            True if curtailment successful
        """
        _LOGGER.info(f"Curtailing GoodWe inverter at {self.host} (load following mode)")

        try:
            if not await self.connect():
                _LOGGER.error("Cannot curtail: failed to connect to inverter")
                return False

            # Step 1: Set export limit to 0W
            success = await self._write_register(self.REG_EXPORT_LIMIT, 0)
            if not success:
                _LOGGER.error("Failed to set export limit to 0W")
                return False

            # Step 2: Enable export limiting
            success = await self._write_register(self.REG_EXPORT_LIMIT_ENABLED, 1)
            if not success:
                _LOGGER.error("Failed to enable export limiting")
                return False

            _LOGGER.info(f"Successfully curtailed GoodWe inverter at {self.host} (0W export limit)")
            await asyncio.sleep(1)
            return True

        except Exception as e:
            _LOGGER.error(f"Error curtailing GoodWe inverter: {e}")
            return False

    async def restore(self) -> bool:
        """Restore normal operation of the GoodWe inverter.

        Disables export power limiting to return to normal export behavior.

        Returns:
            True if restore successful
        """
        _LOGGER.info(f"Restoring GoodWe inverter at {self.host} to normal operation")

        try:
            if not await self.connect():
                _LOGGER.error("Cannot restore: failed to connect to inverter")
                return False

            # Disable export limiting
            success = await self._write_register(self.REG_EXPORT_LIMIT_ENABLED, 0)
            if not success:
                _LOGGER.error("Failed to disable export limiting")
                return False

            _LOGGER.info(f"Successfully restored GoodWe inverter at {self.host}")
            await asyncio.sleep(1)
            return True

        except Exception as e:
            _LOGGER.error(f"Error restoring GoodWe inverter: {e}")
            return False

    async def _read_all_registers(self) -> dict:
        """Read all available registers and return as attributes dict."""
        attrs = {}

        try:
            # Read PV1 data
            pv1_voltage = await self._read_register(self.REG_PV1_VOLTAGE, 1)
            if pv1_voltage:
                attrs["pv1_voltage"] = round(pv1_voltage[0] * 0.1, 1)

            pv1_current = await self._read_register(self.REG_PV1_CURRENT, 1)
            if pv1_current:
                attrs["pv1_current"] = round(pv1_current[0] * 0.1, 1)

            pv1_power = await self._read_register(self.REG_PV1_POWER, 2)
            if pv1_power and len(pv1_power) >= 2:
                attrs["pv1_power"] = self._to_unsigned32(pv1_power[0], pv1_power[1])

            # Read PV2 data
            pv2_voltage = await self._read_register(self.REG_PV2_VOLTAGE, 1)
            if pv2_voltage:
                attrs["pv2_voltage"] = round(pv2_voltage[0] * 0.1, 1)

            pv2_current = await self._read_register(self.REG_PV2_CURRENT, 1)
            if pv2_current:
                attrs["pv2_current"] = round(pv2_current[0] * 0.1, 1)

            pv2_power = await self._read_register(self.REG_PV2_POWER, 2)
            if pv2_power and len(pv2_power) >= 2:
                attrs["pv2_power"] = self._to_unsigned32(pv2_power[0], pv2_power[1])

            # Read battery data
            battery_voltage = await self._read_register(self.REG_BATTERY_VOLTAGE, 1)
            if battery_voltage:
                attrs["battery_voltage"] = round(battery_voltage[0] * 0.1, 1)

            battery_current = await self._read_register(self.REG_BATTERY_CURRENT, 1)
            if battery_current:
                attrs["battery_current"] = round(self._to_signed16(battery_current[0]) * 0.1, 1)

            battery_power = await self._read_register(self.REG_BATTERY_POWER, 2)
            if battery_power and len(battery_power) >= 2:
                attrs["battery_power"] = self._to_signed32(battery_power[0], battery_power[1])

            battery_soc = await self._read_register(self.REG_BATTERY_SOC, 1)
            if battery_soc:
                attrs["battery_level"] = battery_soc[0]

            # Read grid power
            grid_power = await self._read_register(self.REG_GRID_POWER, 1)
            if grid_power:
                attrs["grid_power"] = self._to_signed16(grid_power[0])

            # Read temperatures
            temp_air = await self._read_register(self.REG_TEMP_AIR, 1)
            if temp_air:
                attrs["inverter_temperature"] = round(self._to_signed16(temp_air[0]) * 0.1, 1)

            # Read daily energy
            daily_pv = await self._read_register(self.REG_DAILY_PV, 1)
            if daily_pv:
                attrs["daily_pv_generation"] = round(daily_pv[0] * 0.1, 2)

            daily_export = await self._read_register(self.REG_DAILY_EXPORT, 1)
            if daily_export:
                attrs["daily_export"] = round(daily_export[0] * 0.1, 2)

            daily_import = await self._read_register(self.REG_DAILY_IMPORT, 1)
            if daily_import:
                attrs["daily_import"] = round(daily_import[0] * 0.1, 2)

            # Read export limit status
            export_enabled = await self._read_register(self.REG_EXPORT_LIMIT_ENABLED, 1)
            if export_enabled:
                attrs["export_limit_enabled"] = export_enabled[0] == 1

            export_limit = await self._read_register(self.REG_EXPORT_LIMIT, 1)
            if export_limit:
                attrs["export_limit_w"] = export_limit[0]

        except Exception as e:
            _LOGGER.warning(f"Error reading some registers: {e}")

        return attrs

    async def get_status(self) -> InverterState:
        """Get current status of the GoodWe inverter.

        Returns:
            InverterState with current status and register attributes
        """
        try:
            if not await self.connect():
                return InverterState(
                    status=InverterStatus.OFFLINE,
                    is_curtailed=False,
                    error_message="Failed to connect to inverter",
                )

            # Read all available registers
            attrs = await self._read_all_registers()

            # Read work mode
            work_mode = await self._read_register(self.REG_WORK_MODE, 1)
            status = InverterStatus.ONLINE
            is_curtailed = False

            if work_mode:
                mode_value = work_mode[0]
                if mode_value == self.MODE_NORMAL:
                    status = InverterStatus.ONLINE
                    attrs["running_state"] = "normal"
                elif mode_value == self.MODE_FAULT:
                    status = InverterStatus.ERROR
                    attrs["running_state"] = "fault"
                elif mode_value == self.MODE_WAIT:
                    status = InverterStatus.ONLINE
                    attrs["running_state"] = "waiting"
                else:
                    attrs["running_state"] = f"mode_{mode_value}"

            # Check if export limiting is active (load following mode)
            if attrs.get("export_limit_enabled") and attrs.get("export_limit_w", 10000) == 0:
                is_curtailed = True
                attrs["running_state"] = "load_following"
                if status == InverterStatus.ONLINE:
                    status = InverterStatus.CURTAILED

            # Add model info
            attrs["model"] = self.model or "ET/EH Series"
            attrs["host"] = self.host

            # Calculate total power
            pv1 = attrs.get("pv1_power", 0)
            pv2 = attrs.get("pv2_power", 0)
            power_output = pv1 + pv2 if pv1 or pv2 else None

            self._last_state = InverterState(
                status=status,
                is_curtailed=is_curtailed,
                power_output_w=float(power_output) if power_output else None,
                attributes=attrs,
            )

            return self._last_state

        except Exception as e:
            _LOGGER.error(f"Error getting GoodWe inverter status: {e}")
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
