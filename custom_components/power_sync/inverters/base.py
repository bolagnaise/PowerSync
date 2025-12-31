"""Abstract base class for inverter controllers.

All inverter implementations must inherit from InverterController
and implement the required methods.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional
import logging

_LOGGER = logging.getLogger(__name__)


class InverterStatus(Enum):
    """Inverter connection and operational status."""
    UNKNOWN = "unknown"
    ONLINE = "online"
    OFFLINE = "offline"
    CURTAILED = "curtailed"
    ERROR = "error"


@dataclass
class InverterState:
    """Current state of the inverter."""
    status: InverterStatus
    is_curtailed: bool
    power_output_w: Optional[float] = None
    power_limit_percent: Optional[int] = None
    error_message: Optional[str] = None
    # Extended attributes from register readings
    attributes: Optional[dict] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for API responses."""
        result = {
            "status": self.status.value,
            "is_curtailed": self.is_curtailed,
            "power_output_w": self.power_output_w,
            "power_limit_percent": self.power_limit_percent,
            "error_message": self.error_message,
        }
        if self.attributes:
            result.update(self.attributes)
        return result


class InverterController(ABC):
    """Abstract base class for inverter controllers.

    All inverter brand implementations must inherit from this class
    and implement the required methods.
    """

    def __init__(
        self,
        host: str,
        port: int = 502,
        slave_id: int = 1,
        model: Optional[str] = None,
    ):
        """Initialize the inverter controller.

        Args:
            host: IP address of the inverter/gateway
            port: Modbus TCP port (default: 502)
            slave_id: Modbus slave ID (default: 1)
            model: Inverter model (optional)
        """
        self.host = host
        self.port = port
        self.slave_id = slave_id
        self.model = model
        self._connected = False
        self._last_state: Optional[InverterState] = None

    @property
    def is_connected(self) -> bool:
        """Return True if connected to the inverter."""
        return self._connected

    @abstractmethod
    async def connect(self) -> bool:
        """Establish connection to the inverter.

        Returns:
            True if connection successful, False otherwise
        """
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Close connection to the inverter."""
        pass

    @abstractmethod
    async def curtail(self) -> bool:
        """Curtail (stop/limit) inverter production.

        This should stop or severely limit solar production
        to prevent export during negative price periods.

        Returns:
            True if curtailment successful, False otherwise
        """
        pass

    @abstractmethod
    async def restore(self) -> bool:
        """Restore normal inverter operation.

        This should resume normal solar production after
        curtailment is no longer needed.

        Returns:
            True if restore successful, False otherwise
        """
        pass

    @abstractmethod
    async def get_status(self) -> InverterState:
        """Get current inverter status.

        Returns:
            InverterState with current status information
        """
        pass

    async def test_connection(self) -> tuple[bool, str]:
        """Test connection to the inverter.

        Returns:
            Tuple of (success, message)
        """
        try:
            if await self.connect():
                state = await self.get_status()
                await self.disconnect()
                return True, f"Connected successfully. Status: {state.status.value}"
            return False, "Failed to establish connection"
        except Exception as e:
            _LOGGER.error(f"Connection test failed: {e}")
            return False, f"Connection error: {str(e)}"

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(host={self.host}, port={self.port}, slave_id={self.slave_id})"
