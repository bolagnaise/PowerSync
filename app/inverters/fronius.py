"""Fronius inverter controller via Modbus TCP.

Supports Fronius inverters with SunSpec Modbus interface.
Uses power limiting (WMaxLimPct) combined with pre-configured
0W export limit for load following curtailment.

Reference: https://www.smartmotion.life/2023/09/12/amber-electric-curtailment-with-home-assistant/
"""
import asyncio
import logging
import re
from typing import Optional

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from .base import InverterController, InverterState, InverterStatus

_LOGGER = logging.getLogger(__name__)


class FroniusController(InverterController):
    """Controller for Fronius inverters via Modbus TCP (SunSpec).

    Uses SunSpec power limiting registers to enable/disable
    the pre-configured 0W export limit for load following.

    Prerequisites:
    - Fronius installer password (contact Fronius if installer won't provide)
    - Export Limiting Control set to 0W (load following mode)
    """

    # SunSpec Modbus register addresses
    # These are in the Immediate Controls model (Model 123)
    REG_WMAXLIMPCT = 40232          # Power output limit (0-10000 = 0-100%)
    REG_WMAXLIMPCT_RVRT = 40234     # Reversion timeout (seconds, 0=disabled)
    REG_WMAXLIM_ENA = 40236         # Enable power limiting (1=on, 0=off)

    # SunSpec Common Block (Model 1) - for reading model info
    REG_SUNSPEC_ID = 40000          # "SunS" marker (0x5375, 0x6E53)
    REG_MODEL = 40020               # Model string (16 registers = 32 chars)

    # Status registers for reading inverter state
    REG_STATUS = 40107              # Operating state
    REG_AC_POWER = 40083            # AC Power output (W)
    REG_AC_POWER_SF = 40084         # AC Power scale factor
    REG_DC_POWER = 40101            # DC Power (W)
    REG_TEMPERATURE = 40103         # Cabinet temperature

    # Operating state values
    STATUS_OFF = 1
    STATUS_SLEEPING = 2
    STATUS_STARTING = 3
    STATUS_MPPT = 4                 # Normal operation
    STATUS_THROTTLED = 5
    STATUS_SHUTTING_DOWN = 6
    STATUS_FAULT = 7
    STATUS_STANDBY = 8

    # Timeout for Modbus operations
    TIMEOUT_SECONDS = 10.0

    def __init__(
        self,
        host: str,
        port: int = 502,
        slave_id: int = 1,
        model: Optional[str] = None,
        load_following: bool = False,
    ):
        """Initialize Fronius controller.

        Args:
            host: IP address of Fronius inverter
            port: Modbus TCP port (default: 502)
            slave_id: Modbus slave ID (default: 1)
            model: Fronius model (e.g., 'primo', 'symo', 'gen24')
            load_following: If True, use calculated power limits instead of
                           relying on 0W soft export limit. Enable this if
                           you don't have installer access to set 0W limit.
        """
        super().__init__(host, port, slave_id, model)
        self._client: Optional[AsyncModbusTcpClient] = None
        self._lock: Optional[asyncio.Lock] = None  # Created lazily in async context
        # Track if slave was set in client constructor (pymodbus 3.6+)
        self._slave_in_client: bool = False
        # Load following mode (for users without 0W export profile)
        self._load_following = load_following
        self._rated_capacity_w: Optional[int] = None
        self._model_string: Optional[str] = None

    def _get_lock(self) -> asyncio.Lock:
        """Get or create the asyncio lock (lazy initialization for Flask compatibility)."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def connect(self) -> bool:
        """Connect to the Fronius inverter via Modbus TCP."""
        async with self._get_lock():
            try:
                if self._client and self._client.connected:
                    return True

                # Try to create client with device_id parameter (pymodbus 3.9+)
                # Then try slave (pymodbus 3.0-3.8), then without (older versions)
                self._slave_in_client = False
                try:
                    self._client = AsyncModbusTcpClient(
                        host=self.host,
                        port=self.port,
                        timeout=self.TIMEOUT_SECONDS,
                        device_id=self.slave_id,
                    )
                    self._slave_in_client = True
                except TypeError:
                    try:
                        self._client = AsyncModbusTcpClient(
                            host=self.host,
                            port=self.port,
                            timeout=self.TIMEOUT_SECONDS,
                            slave=self.slave_id,
                        )
                        self._slave_in_client = True
                    except TypeError:
                        # Older pymodbus version - neither param accepted in constructor
                        self._client = AsyncModbusTcpClient(
                            host=self.host,
                            port=self.port,
                            timeout=self.TIMEOUT_SECONDS,
                        )

                connected = await self._client.connect()
                if connected:
                    self._connected = True
                    _LOGGER.info(f"Connected to Fronius inverter at {self.host}:{self.port}")
                else:
                    _LOGGER.error(f"Failed to connect to Fronius inverter at {self.host}:{self.port}")

                return connected

            except Exception as e:
                _LOGGER.error(f"Error connecting to Fronius inverter: {e}")
                self._connected = False
                return False

    async def disconnect(self) -> None:
        """Disconnect from the Fronius inverter."""
        async with self._get_lock():
            if self._client:
                self._client.close()
                self._client = None
            self._connected = False
            _LOGGER.debug(f"Disconnected from Fronius inverter at {self.host}")

    async def _write_register(self, address: int, value: int) -> bool:
        """Write a value to a Modbus register."""
        if not self._client or not self._client.connected:
            if not await self.connect():
                return False

        try:
            # If slave was set in client constructor (pymodbus 3.6+), don't pass it again
            if self._slave_in_client:
                result = await self._client.write_register(address=address, value=value)
            else:
                # Try different parameter names for older pymodbus versions
                result = await self._try_modbus_call(
                    self._client.write_register,
                    address=address,
                    value=value,
                )

            if result is None or result.isError():
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
            # If slave was set in client constructor (pymodbus 3.6+), don't pass it again
            if self._slave_in_client:
                result = await self._client.read_holding_registers(address=address, count=count)
            else:
                # Try different parameter names for older pymodbus versions
                result = await self._try_modbus_call(
                    self._client.read_holding_registers,
                    address=address,
                    count=count,
                )

            if result is None or result.isError():
                _LOGGER.debug(f"Modbus read error at register {address}: {result}")
                return None

            return result.registers

        except ModbusException as e:
            _LOGGER.debug(f"Modbus exception reading register {address}: {e}")
            return None
        except Exception as e:
            _LOGGER.debug(f"Error reading register {address}: {e}")
            return None

    async def _try_modbus_call(self, method, **kwargs):
        """Try a modbus call with different slave/unit parameter names."""
        # Try without slave parameter first (if set in client)
        try:
            return await method(**kwargs)
        except TypeError:
            pass

        # Try with 'device_id' parameter (pymodbus 3.9+)
        try:
            return await method(**kwargs, device_id=self.slave_id)
        except TypeError:
            pass

        # Try with 'slave' parameter (pymodbus 3.0-3.8)
        try:
            return await method(**kwargs, slave=self.slave_id)
        except TypeError:
            pass

        # Try with 'unit' parameter (pymodbus 2.x)
        try:
            return await method(**kwargs, unit=self.slave_id)
        except TypeError:
            pass

        _LOGGER.error(f"Could not find compatible pymodbus API for {method.__name__}")
        return None

    def _to_signed16(self, value: int) -> int:
        """Convert unsigned 16-bit to signed."""
        if value >= 0x8000:
            return value - 0x10000
        return value

    async def get_rated_capacity(self) -> Optional[int]:
        """Read model from SunSpec Common Block and parse rated capacity.

        Fronius model names include capacity, e.g.:
        - "Primo 5.0-1" -> 5000W
        - "Symo 10.0-3-M" -> 10000W
        - "Symo GEN24 6.0 Plus" -> 6000W

        Returns:
            Rated capacity in watts, or None if not detected
        """
        # Return cached value if available
        if self._rated_capacity_w is not None:
            return self._rated_capacity_w

        try:
            if not await self.connect():
                return None

            # Read Model string from SunSpec Common Block (16 registers = 32 chars)
            regs = await self._read_register(self.REG_MODEL, 16)
            if not regs:
                _LOGGER.debug("Could not read model registers")
                return None

            # Convert registers to string (each register = 2 chars, big-endian)
            model_chars = []
            for reg in regs:
                high_byte = (reg >> 8) & 0xFF
                low_byte = reg & 0xFF
                if high_byte:
                    model_chars.append(chr(high_byte))
                if low_byte:
                    model_chars.append(chr(low_byte))
            self._model_string = ''.join(model_chars).strip('\x00').strip()

            if not self._model_string:
                _LOGGER.debug("Empty model string from inverter")
                return None

            _LOGGER.info(f"Fronius model detected: {self._model_string}")

            # Parse capacity from model string
            # Look for patterns like "5.0", "10.0", "8.2" etc.
            match = re.search(r'(\d+\.?\d*)', self._model_string)
            if match:
                capacity_kw = float(match.group(1))
                self._rated_capacity_w = int(capacity_kw * 1000)
                _LOGGER.info(f"Fronius rated capacity: {self._rated_capacity_w}W")
                return self._rated_capacity_w

            _LOGGER.warning(f"Could not parse capacity from model: {self._model_string}")
            return None

        except Exception as e:
            _LOGGER.warning(f"Error detecting Fronius capacity: {e}")
            return None

    async def curtail(self, home_load_w: Optional[float] = None) -> bool:
        """Enable load following curtailment on the Fronius inverter.

        Two modes available:
        1. Simple mode (default, load_following=False): Disables power limiting,
           sets 0% limit. Relies on inverter's 0W soft export limit.
        2. Load following mode (load_following=True): Calculates power limit
           percentage to match home consumption. Auto-detects inverter capacity.

        Args:
            home_load_w: Current home load in watts (for load following mode)

        Returns:
            True if curtailment successful
        """

        try:
            if not await self.connect():
                _LOGGER.error("Cannot curtail: failed to connect to inverter")
                return False

            # Determine which mode to use
            # If load_following enabled, try to auto-detect capacity
            rated_capacity_w = None
            if self._load_following:
                rated_capacity_w = await self.get_rated_capacity()
                if rated_capacity_w:
                    _LOGGER.debug(f"Using auto-detected capacity: {rated_capacity_w}W")

            use_load_following = (
                self._load_following and
                home_load_w is not None and
                rated_capacity_w is not None and
                rated_capacity_w > 0
            )

            if use_load_following:
                # Load following mode: Calculate power limit based on home load
                target_percent = min(100, max(0, (home_load_w / rated_capacity_w) * 100))
                target_value = int(target_percent * 100)  # SunSpec uses 0-10000 scale

                _LOGGER.info(
                    f"Curtailing Fronius at {self.host} using load following mode: "
                    f"home_load={home_load_w:.0f}W, rated={rated_capacity_w}W, limit={target_percent:.1f}%"
                )
            else:
                # Simple mode: Set 0% limit, rely on 0W soft export limit
                target_value = 0
                _LOGGER.info(
                    f"Curtailing Fronius at {self.host} using simple mode "
                    f"(requires 0W soft export limit configured)"
                )

            # Sequence: disable → write limit → enable
            # Some inverters don't apply new limit values while enabled

            # Step 1: Disable power limiting first
            success = await self._write_register(self.REG_WMAXLIM_ENA, 0)
            if not success:
                _LOGGER.warning("Failed to disable power limiting before update")

            await asyncio.sleep(0.1)

            # Step 2: Set power limit
            success = await self._write_register(self.REG_WMAXLIMPCT, target_value)
            if not success:
                _LOGGER.error("Failed to set power limit")
                return False

            await asyncio.sleep(0.1)

            # Step 3: Disable reversion timeout (stay curtailed indefinitely)
            success = await self._write_register(self.REG_WMAXLIMPCT_RVRT, 0)
            if not success:
                _LOGGER.warning("Failed to disable reversion timeout")
                # Continue anyway - curtailment may still work

            await asyncio.sleep(0.1)

            # Step 4: Re-enable power limiting with new value
            success = await self._write_register(self.REG_WMAXLIM_ENA, 1)
            if not success:
                _LOGGER.error("Failed to enable power limiting")
                return False

            _LOGGER.info(f"Successfully curtailed Fronius inverter at {self.host}")
            await asyncio.sleep(0.5)  # Brief delay for inverter to process
            return True

        except Exception as e:
            _LOGGER.error(f"Error curtailing Fronius inverter: {e}")
            return False

    async def restore(self) -> bool:
        """Restore normal operation of the Fronius inverter.

        Disables power limiting, returning to normal export behavior.

        Returns:
            True if restore successful
        """
        _LOGGER.info(f"Restoring Fronius inverter at {self.host} to normal operation")

        try:
            if not await self.connect():
                _LOGGER.error("Cannot restore: failed to connect to inverter")
                return False

            # Sequence: disable → write limit → enable
            # Some inverters don't apply new limit values while enabled

            # Step 1: Disable power limiting first
            success = await self._write_register(self.REG_WMAXLIM_ENA, 0)
            if not success:
                _LOGGER.warning("Failed to disable power limiting before restore")

            await asyncio.sleep(0.1)

            # Step 2: Set power limit to 100%
            success = await self._write_register(self.REG_WMAXLIMPCT, 10000)
            if not success:
                _LOGGER.error("Failed to set power limit to 100%")
                return False

            await asyncio.sleep(0.1)

            # Step 3: Re-enable power limiting at 100%
            success = await self._write_register(self.REG_WMAXLIM_ENA, 1)
            if not success:
                _LOGGER.error("Failed to enable power limiting")
                return False

            _LOGGER.info(f"Successfully restored Fronius inverter at {self.host}")
            await asyncio.sleep(0.5)
            return True

        except Exception as e:
            _LOGGER.error(f"Error restoring Fronius inverter: {e}")
            return False

    async def _read_all_registers(self) -> dict:
        """Read all available registers and return as attributes dict."""
        attrs = {}

        try:
            # Read AC power with scale factor
            ac_power = await self._read_register(self.REG_AC_POWER, 2)
            if ac_power and len(ac_power) >= 2:
                power = self._to_signed16(ac_power[0])
                scale_factor = self._to_signed16(ac_power[1])
                attrs["ac_power"] = int(power * (10 ** scale_factor))

            # Read DC power
            dc_power = await self._read_register(self.REG_DC_POWER, 1)
            if dc_power:
                attrs["dc_power"] = dc_power[0]

            # Read temperature
            temp = await self._read_register(self.REG_TEMPERATURE, 1)
            if temp:
                attrs["inverter_temperature"] = round(self._to_signed16(temp[0]) * 0.1, 1)

            # Read power limit status
            limit_ena = await self._read_register(self.REG_WMAXLIM_ENA, 1)
            if limit_ena:
                attrs["power_limit_enabled"] = limit_ena[0] == 1

            limit_pct = await self._read_register(self.REG_WMAXLIMPCT, 1)
            if limit_pct:
                attrs["power_limit_percent"] = round(limit_pct[0] / 100, 1)

        except Exception as e:
            _LOGGER.warning(f"Error reading some registers: {e}")

        return attrs

    async def get_status(self) -> InverterState:
        """Get current status of the Fronius inverter.

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

            # Read operating state
            state_regs = await self._read_register(self.REG_STATUS, 1)
            status = InverterStatus.ONLINE
            is_curtailed = False

            if state_regs:
                state_value = state_regs[0]
                if state_value == self.STATUS_MPPT:
                    status = InverterStatus.ONLINE
                    attrs["running_state"] = "mppt"
                elif state_value == self.STATUS_THROTTLED:
                    status = InverterStatus.CURTAILED
                    is_curtailed = True
                    attrs["running_state"] = "throttled"
                elif state_value == self.STATUS_FAULT:
                    status = InverterStatus.ERROR
                    attrs["running_state"] = "fault"
                elif state_value in (self.STATUS_OFF, self.STATUS_SLEEPING, self.STATUS_STANDBY):
                    status = InverterStatus.ONLINE
                    attrs["running_state"] = "standby"
                else:
                    attrs["running_state"] = f"state_{state_value}"

            # Check if power limiting is active
            if attrs.get("power_limit_enabled") and attrs.get("power_limit_percent", 100) < 100:
                is_curtailed = True
                if status == InverterStatus.ONLINE:
                    status = InverterStatus.CURTAILED

            # Add model info
            attrs["model"] = self.model or "Fronius"
            attrs["host"] = self.host

            power_output = attrs.get("ac_power")

            self._last_state = InverterState(
                status=status,
                is_curtailed=is_curtailed,
                power_output_w=float(power_output) if power_output else None,
                attributes=attrs,
            )

            return self._last_state

        except Exception as e:
            _LOGGER.error(f"Error getting Fronius inverter status: {e}")
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
