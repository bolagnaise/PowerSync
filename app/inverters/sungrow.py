"""Sungrow inverter controller via Modbus TCP.

Supports Sungrow SG series inverters (SG5.0RS, SG10RS, etc.)
connected via WiNet-S dongle.

Reference: https://github.com/Artic0din/sungrow-sg5-price-curtailment
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
# Detect which parameter name to use based on version
_pymodbus_version = tuple(int(x) for x in pymodbus.__version__.split(".")[:2])
_SLAVE_PARAM = "device_id" if _pymodbus_version >= (3, 9) else "slave"
_LOGGER.debug(f"pymodbus version {pymodbus.__version__}, using '{_SLAVE_PARAM}' parameter")


# Register maps for different Sungrow model families
# Each map contains: (address, count, scale_factor)
# count=1 for 16-bit, count=2 for 32-bit little-endian
REGISTER_MAPS = {
    # SG10RS and newer models (tested on SG10RS)
    "sg10rs": {
        "daily_yield": (5002, 1, 0.1),       # kWh
        "total_yield": (5003, 2, 0.1),       # kWh, 32-bit LE
        "dc_power": (5016, 2, 1),            # W, 32-bit LE
        "mppt1_voltage": (5010, 1, 0.1),     # V
        "mppt1_current": (5011, 1, 0.1),     # A
        "mppt2_voltage": (5012, 1, 0.1),     # V
        "mppt2_current": (5013, 1, 0.1),     # A
        "temperature": (5007, 1, 0.1),       # °C (signed)
        "grid_voltage": (5018, 1, 0.1),      # V
        "grid_frequency": (5035, 1, 0.1),    # Hz
        "running_state": (5037, 1, 1),       # State code
        "power_limit_toggle": (5006, 1, 1),  # Holding register
        "power_limit_percent": (5007, 1, 0.1),  # Holding register, /10
    },
    # SG.05RS and older models (tested on SG.05RS)
    "sg05rs": {
        "daily_yield": (5003, 1, 0.1),       # kWh
        "total_yield": (5004, 1, 0.1),       # kWh, 16-bit only
        "dc_power": (5031, 1, 1),            # W, 16-bit (active power)
        "mppt1_voltage": (5011, 1, 0.1),     # V
        "mppt1_current": (5012, 1, 0.1),     # A
        "mppt2_voltage": (5013, 1, 0.1),     # V
        "mppt2_current": (5014, 1, 0.1),     # A
        "temperature": (5001, 1, 1),         # °C (no scale)
        "grid_voltage": (5019, 1, 0.1),      # V
        "grid_frequency": (5036, 1, 0.1),    # Hz
        "running_state": None,               # Not available - infer from power
        "power_limit_toggle": (5147, 1, 1),  # Holding register (different!)
        "power_limit_percent": None,         # Not found in scan
    },
}

# Model name to register map mapping
# Keys are normalized (lowercase, no dots/dashes/spaces) to match model_key
MODEL_MAP = {
    # SG10RS family (newer register layout) - larger residential models
    "sg10rs": "sg10rs",
    "sg10": "sg10rs",
    "sg8rs": "sg10rs",
    "sg80rs": "sg10rs",    # Normalized SG8.0RS
    "sg100rs": "sg10rs",   # Normalized SG10.0RS
    "sg12rs": "sg10rs",
    "sg120rs": "sg10rs",   # Normalized SG12RS
    "sg15rs": "sg10rs",
    "sg150rs": "sg10rs",   # Normalized SG15RS
    "sg17rs": "sg10rs",
    "sg170rs": "sg10rs",   # Normalized SG17RS
    "sg20rs": "sg10rs",
    "sg200rs": "sg10rs",   # Normalized SG20RS
    # SG.05RS family (older register layout) - smaller residential models
    "sg5rs": "sg05rs",
    "sg50rs": "sg05rs",    # Normalized SG5.0RS
    "sg05rs": "sg05rs",
    "sg3rs": "sg05rs",
    "sg30rs": "sg05rs",    # Normalized SG3.0RS
    "sg36rs": "sg05rs",    # Normalized SG3.6RS
    "sg25rs": "sg05rs",    # Normalized SG2.5RS
    "sg40rs": "sg05rs",    # Normalized SG4.0RS
    "sg60rs": "sg05rs",    # Normalized SG6.0RS
    "sg70rs": "sg05rs",    # Normalized SG7.0RS
}


class SungrowController(InverterController):
    """Controller for Sungrow SG series inverters via Modbus TCP.

    Uses Modbus TCP to communicate with the inverter through
    the WiNet-S WiFi/Ethernet dongle.

    Supports multiple model families with different register maps:
    - SG10RS family: SG8RS, SG10RS (newer register layout)
    - SG.05RS family: SG3.0RS, SG5.0RS (older register layout)
    """

    # Run mode values (for curtailment control)
    RUN_MODE_SHUTDOWN = 206  # Stop inverter
    RUN_MODE_ENABLED = 207   # Normal operation

    # Power limit toggle values
    POWER_LIMIT_DISABLED = 85   # 0x55
    POWER_LIMIT_ENABLED = 170   # 0xAA

    # Running state values (varies by model)
    STATE_RUNNING = 0x0000       # Normal operation (SG10RS)
    STATE_RUNNING_ALT = 0x0002   # Normal operation (some models)
    STATE_STOP = 0x8000
    STATE_STANDBY = 0xA000
    STATE_INITIAL_STANDBY = 0x1400
    STATE_SHUTDOWN = 0x1200
    STATE_FAULT = 0x1300
    STATE_MAINTAIN = 0x1500

    # Run mode register (common across models)
    REGISTER_RUN_MODE = 5005

    # Timeout for Modbus operations
    TIMEOUT_SECONDS = 10.0

    def __init__(
        self,
        host: str,
        port: int = 502,
        slave_id: int = 1,
        model: Optional[str] = None,
    ):
        """Initialize Sungrow controller.

        Args:
            host: IP address of WiNet-S dongle
            port: Modbus TCP port (default: 502)
            slave_id: Modbus slave ID (default: 1)
            model: Sungrow model (e.g., 'sg10rs', 'sg5.0rs')
        """
        super().__init__(host, port, slave_id, model)
        self._client: Optional[AsyncModbusTcpClient] = None
        self._lock = asyncio.Lock()

        # Select register map based on model
        model_key = (model or "").lower().replace(".", "").replace("-", "").replace(" ", "")
        map_name = MODEL_MAP.get(model_key, "sg10rs")  # Default to sg10rs
        self._reg_map = REGISTER_MAPS[map_name]
        _LOGGER.info(f"Sungrow controller using register map '{map_name}' for model '{model}'")

    async def connect(self) -> bool:
        """Connect to the Sungrow inverter via Modbus TCP."""
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
                    _LOGGER.info(f"Connected to Sungrow inverter at {self.host}:{self.port}")
                else:
                    _LOGGER.error(f"Failed to connect to Sungrow inverter at {self.host}:{self.port}")

                return connected

            except Exception as e:
                _LOGGER.error(f"Error connecting to Sungrow inverter: {e}")
                self._connected = False
                return False

    async def disconnect(self) -> None:
        """Disconnect from the Sungrow inverter."""
        async with self._lock:
            if self._client:
                self._client.close()
                self._client = None
            self._connected = False
            _LOGGER.debug(f"Disconnected from Sungrow inverter at {self.host}")

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

    async def _read_register(self, address: int, count: int = 1) -> Optional[list]:
        """Read values from Modbus holding registers (for control/config values).

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
                **{_SLAVE_PARAM: self.slave_id},
            )

            if result.isError():
                _LOGGER.debug(f"Modbus holding read error at register {address}: {result}")
                return None

            return result.registers

        except ModbusException as e:
            _LOGGER.debug(f"Modbus exception reading holding register {address}: {e}")
            return None
        except Exception as e:
            _LOGGER.debug(f"Error reading holding register {address}: {e}")
            return None

    async def _read_input_register(self, address: int, count: int = 1) -> Optional[list]:
        """Read values from Modbus input registers (for status/measurement values).

        Sungrow inverters use input registers (function code 0x04) for data
        in the 5xxx address range.

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
                **{_SLAVE_PARAM: self.slave_id},
            )

            if result.isError():
                _LOGGER.debug(f"Modbus input read error at register {address}: {result}")
                return None

            return result.registers

        except ModbusException as e:
            _LOGGER.debug(f"Modbus exception reading input register {address}: {e}")
            return None
        except Exception as e:
            _LOGGER.debug(f"Error reading input register {address}: {e}")
            return None

    def _to_signed16(self, value: int) -> int:
        """Convert unsigned 16-bit to signed."""
        if value >= 0x8000:
            return value - 0x10000
        return value

    def _to_unsigned32(self, regs: list) -> int:
        """Convert two unsigned 16-bit registers to unsigned 32-bit.

        Sungrow stores 32-bit values as LOW:HIGH (little-endian),
        so regs[0] is the low word and regs[1] is the high word.
        """
        return (regs[1] << 16) | regs[0]

    async def _read_any_register(self, address: int, count: int = 1) -> Optional[list]:
        """Try reading registers using input registers first, then holding registers.

        Some Sungrow models/firmware versions use input registers (0x04),
        others use holding registers (0x03). This method tries both.
        """
        # Try input registers first (most common for measurement data)
        result = await self._read_input_register(address, count)
        if result:
            return result

        # Fall back to holding registers
        _LOGGER.debug(f"Input register read failed at {address}, trying holding registers")
        return await self._read_register(address, count)

    async def _read_register_from_map(self, key: str, use_holding: bool = False) -> Optional[tuple]:
        """Read a register using the model-specific register map.

        Args:
            key: Register key name (e.g., 'dc_power', 'daily_yield')
            use_holding: If True, use holding registers instead of input

        Returns:
            Tuple of (raw_value, scaled_value) or None if not available/failed
        """
        reg_info = self._reg_map.get(key)
        if not reg_info:
            return None

        address, count, scale = reg_info

        if use_holding:
            regs = await self._read_register(address, count)
        else:
            regs = await self._read_any_register(address, count)

        if not regs:
            return None

        # Convert based on register count
        if count == 2:
            raw_value = self._to_unsigned32(regs)
        else:
            raw_value = regs[0]

        scaled_value = raw_value * scale
        return (raw_value, scaled_value)

    async def _read_all_registers(self) -> dict:
        """Read all registers using model-specific register map.

        Automatically uses the correct register addresses based on the
        detected model family (SG10RS vs SG.05RS, etc.).
        """
        attrs = {}

        try:
            # Read DC/Active power - most important reading
            power_result = await self._read_register_from_map("dc_power")
            if power_result:
                attrs["dc_power"] = int(power_result[1])
                _LOGGER.debug(f"Sungrow power: {attrs['dc_power']}W")
            else:
                _LOGGER.warning("Failed to read power register")

            # Read daily yield
            daily_result = await self._read_register_from_map("daily_yield")
            if daily_result:
                attrs["daily_pv_generation"] = round(daily_result[1], 2)

            # Read total yield
            total_result = await self._read_register_from_map("total_yield")
            if total_result:
                attrs["total_pv_generation"] = round(total_result[1], 1)

            # Read MPPT values
            mppt1_v = await self._read_register_from_map("mppt1_voltage")
            mppt1_i = await self._read_register_from_map("mppt1_current")
            if mppt1_v and mppt1_i:
                attrs["mppt1_voltage"] = round(mppt1_v[1], 1)
                attrs["mppt1_current"] = round(mppt1_i[1], 1)
                attrs["mppt1_power"] = round(attrs["mppt1_voltage"] * attrs["mppt1_current"], 0)

            mppt2_v = await self._read_register_from_map("mppt2_voltage")
            mppt2_i = await self._read_register_from_map("mppt2_current")
            if mppt2_v and mppt2_i:
                attrs["mppt2_voltage"] = round(mppt2_v[1], 1)
                attrs["mppt2_current"] = round(mppt2_i[1], 1)
                attrs["mppt2_power"] = round(attrs["mppt2_voltage"] * attrs["mppt2_current"], 0)

            # Read temperature
            temp_result = await self._read_register_from_map("temperature")
            if temp_result:
                # Some models use signed values
                temp_scale = self._reg_map["temperature"][2]
                if temp_scale < 1:  # If scaled, treat as signed
                    attrs["inverter_temperature"] = round(self._to_signed16(temp_result[0]) * temp_scale, 1)
                else:
                    attrs["inverter_temperature"] = temp_result[1]

            # Read grid voltage
            voltage_result = await self._read_register_from_map("grid_voltage")
            if voltage_result:
                attrs["grid_voltage"] = round(voltage_result[1], 1)

            # Read grid frequency
            freq_result = await self._read_register_from_map("grid_frequency")
            if freq_result:
                attrs["grid_frequency"] = round(freq_result[1], 2)

            # Read power limit settings (holding registers)
            limit_toggle = await self._read_register_from_map("power_limit_toggle", use_holding=True)
            if limit_toggle:
                attrs["power_limit_enabled"] = limit_toggle[0] == self.POWER_LIMIT_ENABLED
                _LOGGER.debug(f"Sungrow power limit toggle: {limit_toggle[0]}")

            limit_percent = await self._read_register_from_map("power_limit_percent", use_holding=True)
            if limit_percent:
                attrs["power_limit_percent"] = min(limit_percent[1], 100)
                _LOGGER.debug(f"Sungrow power limit percent: {attrs['power_limit_percent']}%")

            _LOGGER.info(f"Sungrow register read complete: {len(attrs)} attributes collected")

        except Exception as e:
            _LOGGER.warning(f"Error reading some registers: {e}")

        return attrs

    async def curtail(self) -> bool:
        """Stop the Sungrow inverter to prevent solar export.

        Writes shutdown command (206) to the run mode register.

        Returns:
            True if curtailment successful
        """
        _LOGGER.info(f"Curtailing Sungrow inverter at {self.host} (shutdown mode)")

        try:
            # Ensure connected
            if not await self.connect():
                _LOGGER.error("Cannot curtail: failed to connect to inverter")
                return False

            # Write shutdown command to run mode register
            success = await self._write_register(
                self.REGISTER_RUN_MODE,
                self.RUN_MODE_SHUTDOWN,
            )

            if success:
                _LOGGER.info(f"Successfully curtailed Sungrow inverter at {self.host}")
                # Verify the change
                await asyncio.sleep(1)  # Brief delay for inverter to process
                state = await self.get_status()
                if state.is_curtailed:
                    _LOGGER.info("Curtailment verified - inverter is in shutdown state")
                else:
                    _LOGGER.warning("Curtailment command sent but state not verified")
            else:
                _LOGGER.error(f"Failed to curtail Sungrow inverter at {self.host}")

            return success

        except Exception as e:
            _LOGGER.error(f"Error curtailing Sungrow inverter: {e}")
            return False

    async def restore(self) -> bool:
        """Restore normal operation of the Sungrow inverter.

        Writes enable command (207) to the run mode register.

        Returns:
            True if restore successful
        """
        _LOGGER.info(f"Restoring Sungrow inverter at {self.host} to normal operation")

        try:
            # Ensure connected
            if not await self.connect():
                _LOGGER.error("Cannot restore: failed to connect to inverter")
                return False

            # Write enable command to run mode register
            success = await self._write_register(
                self.REGISTER_RUN_MODE,
                self.RUN_MODE_ENABLED,
            )

            if success:
                _LOGGER.info(f"Successfully restored Sungrow inverter at {self.host}")
                # Verify the change
                await asyncio.sleep(1)  # Brief delay for inverter to process
                state = await self.get_status()
                if not state.is_curtailed:
                    _LOGGER.info("Restore verified - inverter is running")
                else:
                    _LOGGER.warning("Restore command sent but state not verified - may take time to start")
            else:
                _LOGGER.error(f"Failed to restore Sungrow inverter at {self.host}")

            return success

        except Exception as e:
            _LOGGER.error(f"Error restoring Sungrow inverter: {e}")
            return False

    async def get_status(self) -> InverterState:
        """Get current status of the Sungrow inverter.

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

            # Get power output from attrs
            power_output = attrs.get("dc_power")

            # Read running state register if available for this model
            running_state_reg = self._reg_map.get("running_state")
            running_state = None
            is_curtailed = False

            if running_state_reg:
                state_regs = await self._read_input_register(running_state_reg[0], 1)
                if state_regs:
                    running_state = state_regs[0]

            # Determine status based on running state (if available) or power output
            if running_state is not None:
                is_curtailed = running_state in (
                    self.STATE_STOP,
                    self.STATE_SHUTDOWN,
                    self.STATE_STANDBY,
                    self.STATE_INITIAL_STANDBY,
                )

                if running_state in (self.STATE_RUNNING, self.STATE_RUNNING_ALT):
                    status = InverterStatus.ONLINE
                    attrs["running_state"] = "running"
                elif running_state == self.STATE_FAULT:
                    status = InverterStatus.ERROR
                    attrs["running_state"] = "fault"
                elif is_curtailed:
                    status = InverterStatus.CURTAILED
                    attrs["running_state"] = "stopped"
                elif running_state == 0xFFFF or running_state == 65535:
                    # Register returned invalid value - infer from power output
                    if power_output is not None and power_output > 0:
                        status = InverterStatus.ONLINE
                        attrs["running_state"] = "running"
                    else:
                        status = InverterStatus.ONLINE
                        attrs["running_state"] = "idle"
                else:
                    status = InverterStatus.UNKNOWN
                    attrs["running_state"] = f"unknown (0x{running_state:04X})"
            else:
                # No running state register - infer from power output (e.g., SG.05RS)
                if power_output is not None and power_output > 0:
                    status = InverterStatus.ONLINE
                    attrs["running_state"] = "running"
                else:
                    status = InverterStatus.ONLINE
                    attrs["running_state"] = "idle"

            # Add model info
            attrs["model"] = self.model or "SG Series"
            attrs["host"] = self.host

            # Get power limit percentage (default to 100 if not available or not enabled)
            power_limit_pct = attrs.get("power_limit_percent", 100)
            if not attrs.get("power_limit_enabled", False):
                power_limit_pct = 100  # If limit not enabled, it's effectively 100%

            self._last_state = InverterState(
                status=status,
                is_curtailed=is_curtailed,
                power_output_w=float(power_output) if power_output else None,
                power_limit_percent=power_limit_pct,
                attributes=attrs,
            )

            return self._last_state

        except Exception as e:
            _LOGGER.error(f"Error getting Sungrow inverter status: {e}")
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
