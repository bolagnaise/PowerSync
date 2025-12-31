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

from .base import InverterController, InverterState, InverterStatus

_LOGGER = logging.getLogger(__name__)


class SungrowController(InverterController):
    """Controller for Sungrow SG series inverters via Modbus TCP.

    Uses Modbus TCP to communicate with the inverter through
    the WiNet-S WiFi/Ethernet dongle.
    """

    # Modbus register addresses (0-indexed for pymodbus)
    REGISTER_RUN_MODE = 5005          # Register 5006 in 1-indexed docs
    REGISTER_POWER_LIMIT_TOGGLE = 5006  # Register 5007
    REGISTER_POWER_LIMIT_PERCENT = 5007  # Register 5008

    # Run mode values
    RUN_MODE_SHUTDOWN = 206  # Stop inverter
    RUN_MODE_ENABLED = 207   # Normal operation

    # Power limit toggle values
    POWER_LIMIT_DISABLED = 85   # 0x55
    POWER_LIMIT_ENABLED = 170   # 0xAA

    # Status registers for reading inverter state
    REGISTER_RUNNING_STATE = 5037     # Current running state
    REGISTER_TOTAL_ACTIVE_POWER = 5016  # Total active power (W)

    # Running state values
    STATE_RUNNING = 0x0002
    STATE_STOP = 0x8000
    STATE_STANDBY = 0xA000
    STATE_INITIAL_STANDBY = 0x1400
    STATE_SHUTDOWN = 0x1200
    STATE_FAULT = 0x1300
    STATE_MAINTAIN = 0x1500

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
            model: Sungrow model (e.g., 'sg10')
        """
        super().__init__(host, port, slave_id, model)
        self._client: Optional[AsyncModbusTcpClient] = None
        self._lock = asyncio.Lock()

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
                _LOGGER.error(f"Modbus read error at register {address}: {result}")
                return None

            return result.registers

        except ModbusException as e:
            _LOGGER.error(f"Modbus exception reading register {address}: {e}")
            return None
        except Exception as e:
            _LOGGER.error(f"Error reading register {address}: {e}")
            return None

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
            InverterState with current status
        """
        try:
            # Ensure connected
            if not await self.connect():
                return InverterState(
                    status=InverterStatus.OFFLINE,
                    is_curtailed=False,
                    error_message="Failed to connect to inverter",
                )

            # Read running state register
            state_regs = await self._read_register(self.REGISTER_RUNNING_STATE, 1)
            power_regs = await self._read_register(self.REGISTER_TOTAL_ACTIVE_POWER, 1)

            if state_regs is None:
                return InverterState(
                    status=InverterStatus.ERROR,
                    is_curtailed=False,
                    error_message="Failed to read inverter state",
                )

            running_state = state_regs[0]
            power_output = power_regs[0] if power_regs else None

            # Determine status based on running state
            is_curtailed = running_state in (
                self.STATE_STOP,
                self.STATE_SHUTDOWN,
                self.STATE_STANDBY,
                self.STATE_INITIAL_STANDBY,
            )

            if running_state == self.STATE_RUNNING:
                status = InverterStatus.ONLINE
            elif running_state == self.STATE_FAULT:
                status = InverterStatus.ERROR
            elif is_curtailed:
                status = InverterStatus.CURTAILED
            else:
                status = InverterStatus.UNKNOWN

            self._last_state = InverterState(
                status=status,
                is_curtailed=is_curtailed,
                power_output_w=float(power_output) if power_output else None,
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
