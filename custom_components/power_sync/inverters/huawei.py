"""Huawei SUN2000 inverter controller via Modbus TCP.

Supports Huawei SUN2000 series inverters (L1, M0, M1, M2).
Uses export power limiting for load following curtailment.

Reference: https://github.com/wlcrs/huawei-solar-lib
"""
import asyncio
import logging
from typing import Optional

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException
import pymodbus

from .base import InverterController, InverterState, InverterStatus

_LOGGER = logging.getLogger(__name__)

# pymodbus 3.9+ changed 'slave' parameter to 'device_id'
try:
    _pymodbus_version = tuple(int(x) for x in pymodbus.__version__.split(".")[:2])
    _SLAVE_PARAM = "device_id" if _pymodbus_version >= (3, 9) else "slave"
except Exception:
    _SLAVE_PARAM = "slave"  # Fallback to older parameter name


class HuaweiController(InverterController):
    """Controller for Huawei SUN2000 series inverters via Modbus TCP.

    Uses Modbus TCP to communicate with the inverter through
    the Smart Dongle (WiFi or LAN) on port 502.

    Supports load following curtailment via active power control.
    """

    # Read registers (Input/Holding registers)
    # PV string registers
    REG_PV1_VOLTAGE = 32016        # PV1 voltage (V * 10)
    REG_PV1_CURRENT = 32017        # PV1 current (A * 100)
    REG_PV2_VOLTAGE = 32018        # PV2 voltage (V * 10)
    REG_PV2_CURRENT = 32019        # PV2 current (A * 100)

    # Power registers
    REG_INPUT_POWER = 32064        # DC input power (kW * 1000, 32-bit)
    REG_ACTIVE_POWER = 32080       # AC active power (kW * 1000, 32-bit signed)

    # Temperature
    REG_INVERTER_TEMP = 32087      # Inverter temperature (°C * 10)

    # Energy registers
    REG_DAILY_YIELD = 32114        # Daily energy yield (kWh * 100, 32-bit)
    REG_TOTAL_YIELD = 32106        # Total energy yield (kWh * 100, 32-bit)

    # Battery registers (for hybrid models with LUNA battery)
    REG_BATTERY_SOC = 37004        # Battery state of charge (% * 10)
    REG_BATTERY_POWER = 37001      # Battery charge/discharge power (W, 32-bit signed)

    # Grid/Meter registers (requires Huawei Smart Power Sensor)
    REG_GRID_POWER = 37113         # Grid active power (W, 32-bit signed, +export/-import)

    # Device status
    REG_DEVICE_STATUS = 32089      # Device status code

    # Active power control registers (for export limiting)
    REG_ACTIVE_POWER_CONTROL_MODE = 47415   # Control mode (U16)
    REG_MAX_FEED_GRID_POWER_KW = 47416      # Max feed-in power kW (I32, gain 1000)
    REG_MAX_FEED_GRID_POWER_PCT = 47418     # Max feed-in power % (I16, gain 10)

    # Active power control mode values
    MODE_UNLIMITED = 0              # No power limiting (default)
    MODE_DI_SCHEDULING = 1          # DI active scheduling
    MODE_ZERO_EXPORT = 5            # Zero power grid connection
    MODE_LIMIT_KW = 6               # Power-limited grid connection (kW)
    MODE_LIMIT_PERCENT = 7          # Power-limited grid connection (%)

    # Device status values
    STATUS_STANDBY = 0x0000
    STATUS_GRID_CONNECTED = 0x0001
    STATUS_GRID_CONNECTED_LIMIT = 0x0002
    STATUS_GRID_CONNECTED_EXPORT_LIMIT = 0x0003
    STATUS_STOP = 0x0100
    STATUS_SHUTDOWN = 0x0200
    STATUS_FAULT = 0x0300
    STATUS_STANDBY_NO_GRID = 0x0400

    # Timeout for Modbus operations
    TIMEOUT_SECONDS = 10.0

    def __init__(
        self,
        host: str,
        port: int = 502,
        slave_id: int = 1,
        model: Optional[str] = None,
    ):
        """Initialize Huawei controller.

        Args:
            host: IP address of Huawei Smart Dongle
            port: Modbus TCP port (default: 502, some firmware uses 6607)
            slave_id: Modbus slave ID (default: 1)
            model: Huawei model (e.g., 'l1', 'm1', 'm2')
        """
        super().__init__(host, port, slave_id, model)
        self._client: Optional[AsyncModbusTcpClient] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> bool:
        """Connect to the Huawei inverter via Modbus TCP."""
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
                    _LOGGER.info(f"Connected to Huawei inverter at {self.host}:{self.port}")
                else:
                    _LOGGER.error(f"Failed to connect to Huawei inverter at {self.host}:{self.port}")

                return connected

            except Exception as e:
                _LOGGER.error(f"Error connecting to Huawei inverter: {e}")
                self._connected = False
                return False

    async def disconnect(self) -> None:
        """Disconnect from the Huawei inverter."""
        async with self._lock:
            if self._client:
                self._client.close()
                self._client = None
            self._connected = False
            _LOGGER.debug(f"Disconnected from Huawei inverter at {self.host}")

    async def _write_register(self, address: int, value: int) -> bool:
        """Write a value to a Modbus holding register."""
        if not self._client or not self._client.connected:
            if not await self.connect():
                return False

        try:
            result = await self._client.write_register(
                address=address,
                value=value,
                **{_SLAVE_PARAM: self.slave_id},
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

    async def _write_registers(self, address: int, values: list) -> bool:
        """Write multiple values to consecutive Modbus holding registers."""
        if not self._client or not self._client.connected:
            if not await self.connect():
                return False

        try:
            result = await self._client.write_registers(
                address=address,
                values=values,
                **{_SLAVE_PARAM: self.slave_id},
            )

            if result.isError():
                _LOGGER.error(f"Modbus write error at register {address}: {result}")
                return False

            _LOGGER.debug(f"Successfully wrote {values} to registers starting at {address}")
            return True

        except ModbusException as e:
            _LOGGER.error(f"Modbus exception writing to registers {address}: {e}")
            return False
        except Exception as e:
            _LOGGER.error(f"Error writing to registers {address}: {e}")
            return False

    async def _read_register(self, address: int, count: int = 1) -> Optional[list]:
        """Read values from Modbus holding registers."""
        if not self._client or not self._client.connected:
            if not await self.connect():
                return None

        try:
            result = await self._client.read_holding_registers(
                address=address,
                count=count,
                **{_SLAVE_PARAM: self.slave_id},
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

    def _i32_to_registers(self, value: int) -> list:
        """Convert signed 32-bit integer to two 16-bit register values."""
        if value < 0:
            value = value + 0x100000000
        high = (value >> 16) & 0xFFFF
        low = value & 0xFFFF
        return [high, low]

    async def curtail(
        self,
        home_load_w: Optional[float] = None,
        rated_capacity_w: Optional[float] = None,
    ) -> bool:
        """Enable load following curtailment on the Huawei inverter.

        If home_load_w is provided, uses kW limit mode to limit export to home load.
        Otherwise sets active power control mode to zero export.

        Returns:
            True if curtailment successful
        """
        try:
            if not await self.connect():
                _LOGGER.error("Cannot curtail: failed to connect to inverter")
                return False

            if home_load_w is not None and home_load_w > 0:
                # Load-following: use kW limit mode
                limit_kw_scaled = int(home_load_w)  # gain 1000 → W value maps to kW×1000
                _LOGGER.info(f"Curtailing Huawei inverter at {self.host} (load-following: {home_load_w:.0f}W)")
                success = await self._write_register(
                    self.REG_ACTIVE_POWER_CONTROL_MODE,
                    self.MODE_LIMIT_KW
                )
                if success:
                    success = await self._write_registers(
                        self.REG_MAX_FEED_GRID_POWER_KW,
                        self._i32_to_registers(limit_kw_scaled)
                    )
            else:
                # Zero export mode
                _LOGGER.info(f"Curtailing Huawei inverter at {self.host} (zero export mode)")
                success = await self._write_register(
                    self.REG_ACTIVE_POWER_CONTROL_MODE,
                    self.MODE_ZERO_EXPORT
                )
                if not success:
                    _LOGGER.warning("Zero export mode failed, trying kW limit mode with 0")
                    success = await self._write_register(
                        self.REG_ACTIVE_POWER_CONTROL_MODE,
                        self.MODE_LIMIT_KW
                    )
                    if success:
                        success = await self._write_registers(
                            self.REG_MAX_FEED_GRID_POWER_KW,
                            self._i32_to_registers(0)
                        )

            if not success:
                _LOGGER.error("Failed to enable export limiting")
                return False

            _LOGGER.info(f"Successfully curtailed Huawei inverter at {self.host}")
            await asyncio.sleep(1)
            return True

        except Exception as e:
            _LOGGER.error(f"Error curtailing Huawei inverter: {e}")
            return False

    async def restore(self) -> bool:
        """Restore normal operation of the Huawei inverter.

        Sets active power control mode back to unlimited.

        Returns:
            True if restore successful
        """
        _LOGGER.info(f"Restoring Huawei inverter at {self.host} to normal operation")

        try:
            if not await self.connect():
                _LOGGER.error("Cannot restore: failed to connect to inverter")
                return False

            # Set active power control mode to unlimited
            success = await self._write_register(
                self.REG_ACTIVE_POWER_CONTROL_MODE,
                self.MODE_UNLIMITED
            )
            if not success:
                _LOGGER.error("Failed to disable export limiting")
                return False

            _LOGGER.info(f"Successfully restored Huawei inverter at {self.host}")
            await asyncio.sleep(1)
            return True

        except Exception as e:
            _LOGGER.error(f"Error restoring Huawei inverter: {e}")
            return False

    async def _read_all_registers(self) -> dict:
        """Read all available registers and return as attributes dict."""
        attrs = {}

        try:
            # Read PV1 data
            pv1_voltage = await self._read_register(self.REG_PV1_VOLTAGE, 1)
            if pv1_voltage:
                attrs["pv1_voltage"] = round(pv1_voltage[0] / 10.0, 1)

            pv1_current = await self._read_register(self.REG_PV1_CURRENT, 1)
            if pv1_current:
                attrs["pv1_current"] = round(pv1_current[0] / 100.0, 2)

            # Read PV2 data
            pv2_voltage = await self._read_register(self.REG_PV2_VOLTAGE, 1)
            if pv2_voltage:
                attrs["pv2_voltage"] = round(pv2_voltage[0] / 10.0, 1)

            pv2_current = await self._read_register(self.REG_PV2_CURRENT, 1)
            if pv2_current:
                attrs["pv2_current"] = round(pv2_current[0] / 100.0, 2)

            # Read input power (DC)
            input_power = await self._read_register(self.REG_INPUT_POWER, 2)
            if input_power and len(input_power) >= 2:
                # kW * 1000 -> W
                power_kw = self._to_signed32(input_power[0], input_power[1]) / 1000.0
                attrs["input_power"] = round(power_kw * 1000)  # Convert to W

            # Read active power (AC)
            active_power = await self._read_register(self.REG_ACTIVE_POWER, 2)
            if active_power and len(active_power) >= 2:
                power_kw = self._to_signed32(active_power[0], active_power[1]) / 1000.0
                attrs["active_power"] = round(power_kw * 1000)  # Convert to W

            # Read inverter temperature
            temp = await self._read_register(self.REG_INVERTER_TEMP, 1)
            if temp:
                attrs["inverter_temperature"] = round(self._to_signed16(temp[0]) / 10.0, 1)

            # Read daily yield
            daily_yield = await self._read_register(self.REG_DAILY_YIELD, 2)
            if daily_yield and len(daily_yield) >= 2:
                yield_kwh = self._to_unsigned32(daily_yield[0], daily_yield[1]) / 100.0
                attrs["daily_pv_generation"] = round(yield_kwh, 2)

            # Read battery data (may not be present on non-hybrid models)
            battery_soc = await self._read_register(self.REG_BATTERY_SOC, 1)
            if battery_soc and battery_soc[0] != 0xFFFF:
                attrs["battery_level"] = round(battery_soc[0] / 10.0, 1)

            battery_power = await self._read_register(self.REG_BATTERY_POWER, 2)
            if battery_power and len(battery_power) >= 2:
                power_w = self._to_signed32(battery_power[0], battery_power[1])
                if power_w != 0x7FFFFFFF:  # Check for invalid value
                    attrs["battery_power"] = power_w

            # Read grid power (requires Smart Power Sensor)
            grid_power = await self._read_register(self.REG_GRID_POWER, 2)
            if grid_power and len(grid_power) >= 2:
                power_w = self._to_signed32(grid_power[0], grid_power[1])
                if power_w != 0x7FFFFFFF:  # Check for invalid value
                    attrs["grid_power"] = power_w

            # Read device status
            device_status = await self._read_register(self.REG_DEVICE_STATUS, 1)
            if device_status:
                attrs["device_status_code"] = device_status[0]

            # Read active power control mode
            control_mode = await self._read_register(self.REG_ACTIVE_POWER_CONTROL_MODE, 1)
            if control_mode:
                mode_value = control_mode[0]
                attrs["active_power_control_mode"] = mode_value
                mode_names = {
                    0: "unlimited",
                    1: "di_scheduling",
                    5: "zero_export",
                    6: "limit_kw",
                    7: "limit_percent",
                }
                attrs["active_power_control_mode_name"] = mode_names.get(mode_value, f"mode_{mode_value}")

        except Exception as e:
            _LOGGER.warning(f"Error reading some registers: {e}")

        return attrs

    async def get_status(self) -> InverterState:
        """Get current status of the Huawei inverter.

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

            # If we couldn't read ANY registers, the inverter is likely sleeping/offline
            if not attrs or len(attrs) == 0:
                _LOGGER.debug("Huawei: No register data - inverter likely sleeping")
                return InverterState(
                    status=InverterStatus.OFFLINE,
                    is_curtailed=False,
                    error_message="No register data (inverter sleeping)",
                    attributes={"host": self.host, "model": self.model or "SUN2000"},
                )

            # Determine status from device status code
            status = InverterStatus.ONLINE
            is_curtailed = False

            device_status = attrs.get("device_status_code", 0)
            if device_status == self.STATUS_FAULT:
                status = InverterStatus.ERROR
                attrs["running_state"] = "fault"
            elif device_status == self.STATUS_SHUTDOWN:
                status = InverterStatus.OFFLINE
                attrs["running_state"] = "shutdown"
            elif device_status == self.STATUS_STOP:
                status = InverterStatus.OFFLINE
                attrs["running_state"] = "stopped"
            elif device_status == self.STATUS_STANDBY:
                status = InverterStatus.ONLINE
                attrs["running_state"] = "standby"
            elif device_status == self.STATUS_STANDBY_NO_GRID:
                status = InverterStatus.ONLINE
                attrs["running_state"] = "standby_no_grid"
            elif device_status in (self.STATUS_GRID_CONNECTED, self.STATUS_GRID_CONNECTED_LIMIT, self.STATUS_GRID_CONNECTED_EXPORT_LIMIT):
                status = InverterStatus.ONLINE
                attrs["running_state"] = "grid_connected"
            else:
                attrs["running_state"] = f"status_{device_status}"

            # Check if export limiting is active
            control_mode = attrs.get("active_power_control_mode", 0)
            if control_mode in (self.MODE_ZERO_EXPORT, self.MODE_LIMIT_KW, self.MODE_LIMIT_PERCENT):
                is_curtailed = True
                attrs["running_state"] = "export_limited"
                if status == InverterStatus.ONLINE:
                    status = InverterStatus.CURTAILED

            # Add model info
            attrs["model"] = self.model or "SUN2000"
            attrs["host"] = self.host

            # Get power output
            power_output = attrs.get("active_power") or attrs.get("input_power")

            self._last_state = InverterState(
                status=status,
                is_curtailed=is_curtailed,
                power_output_w=float(power_output) if power_output else None,
                attributes=attrs,
            )

            return self._last_state

        except Exception as e:
            _LOGGER.error(f"Error getting Huawei inverter status: {e}")
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
