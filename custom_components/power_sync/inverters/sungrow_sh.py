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
    # Register 13000 in documentation = address 12999 in pymodbus
    REGISTER_SYSTEM_STATE = 12999

    # System state control values
    STATE_STOP = 0xCE   # 206 - Stop inverter
    STATE_START = 0xCF  # 207 - Start inverter

    # Status registers for reading inverter state
    # These may need adjustment based on actual SH series register map
    REGISTER_RUNNING_STATE = 13000     # Nominal running state
    REGISTER_TOTAL_ACTIVE_POWER = 13033  # Total active power (W)

    # Running state values (may differ from SG series)
    RUNNING_STATE_STOP = 0x8000
    RUNNING_STATE_STANDBY = 0x1400
    RUNNING_STATE_RUNNING = 0x0002
    RUNNING_STATE_FAULT = 0x1300

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

            # For SH series, status reading may need adjustment
            # For now, return a basic online status if connected
            self._last_state = InverterState(
                status=InverterStatus.ONLINE,
                is_curtailed=False,
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
