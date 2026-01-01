"""Zeversolar inverter controller via HTTP API.

Controls power output via the inverter's built-in web interface.
Uses HTTP POST to /pwrlim.cgi for power limiting.

Key parameters:
- enlim: 'on' to enable power limiting
- ac_mode: 1 = percentage-based limiting
- ac_value1: 0-100 percentage of max power
- ac_sys: Inverter AC capacity in watts (informational only)
"""
import logging
from typing import Optional

import aiohttp

from .base import InverterController, InverterState, InverterStatus

_LOGGER = logging.getLogger(__name__)


class ZeversolarController(InverterController):
    """Controller for Zeversolar inverters via HTTP API.

    Zeversolar inverters have a built-in web interface that accepts
    power limiting commands via POST to /pwrlim.cgi.

    Curtailment is achieved by setting ac_value1=0 (0% power).
    Restore sets ac_value1=100 (100% power).
    """

    # Timeout for HTTP operations
    TIMEOUT_SECONDS = 30.0

    def __init__(
        self,
        host: str,
        port: int = 80,
        slave_id: int = 1,  # Not used, kept for interface compatibility
        model: Optional[str] = None,
        ac_capacity_w: int = 5000,
    ):
        """Initialize the Zeversolar controller.

        Args:
            host: IP address of the inverter
            port: HTTP port (default: 80)
            slave_id: Not used for Zeversolar (HTTP API)
            model: Inverter model (e.g., 'tlc5000')
            ac_capacity_w: Inverter AC capacity in watts (default: 5000)
        """
        super().__init__(host, port, slave_id, model)
        self.ac_capacity_w = ac_capacity_w
        self._session: Optional[aiohttp.ClientSession] = None
        self._current_power_percent: int = 100

    @property
    def base_url(self) -> str:
        """Get the base URL for the inverter."""
        return f"http://{self.host}:{self.port}" if self.port != 80 else f"http://{self.host}"

    async def connect(self) -> bool:
        """Establish connection to the inverter.

        For Zeversolar, we just create an HTTP session and verify connectivity.
        """
        try:
            if self._session is None:
                timeout = aiohttp.ClientTimeout(total=self.TIMEOUT_SECONDS)
                self._session = aiohttp.ClientSession(timeout=timeout)

            # Test connectivity by fetching the advanced settings page
            async with self._session.get(f"{self.base_url}/adv.cgi") as response:
                if response.status == 200:
                    # Parse the response to get current settings
                    text = await response.text()
                    self._parse_adv_response(text)
                    self._connected = True
                    _LOGGER.info(f"Connected to Zeversolar at {self.host}")
                    return True
                else:
                    _LOGGER.error(f"Zeversolar returned status {response.status}")
                    return False

        except aiohttp.ClientError as e:
            _LOGGER.error(f"Failed to connect to Zeversolar at {self.host}: {e}")
            return False
        except Exception as e:
            _LOGGER.error(f"Unexpected error connecting to Zeversolar: {e}")
            return False

    def _parse_adv_response(self, text: str) -> None:
        """Parse the adv.cgi response to get current settings.

        Response format is newline-separated values:
        [0] wifi_enabled
        ...
        [8] enlim (0=disabled, 1=enabled)
        [9] drm_sp
        [10] ac_sys
        [11] ac_value1 (percentage)
        [12] ac_value2
        [13] ac_value3
        [14] ac_mode (1=percentage, 2=fixed watt, 3=DRM)
        [15] em_ml
        """
        try:
            lines = text.strip().split('\n')
            if len(lines) >= 15:
                self.ac_capacity_w = int(lines[10]) if lines[10].isdigit() else self.ac_capacity_w
                self._current_power_percent = int(lines[11]) if lines[11].isdigit() else 100
                _LOGGER.debug(f"Zeversolar settings: ac_sys={self.ac_capacity_w}W, power_limit={self._current_power_percent}%")
        except Exception as e:
            _LOGGER.warning(f"Failed to parse Zeversolar settings: {e}")

    async def disconnect(self) -> None:
        """Close connection to the inverter."""
        if self._session:
            await self._session.close()
            self._session = None
        self._connected = False

    async def _set_power_limit(self, percent: int) -> bool:
        """Set the power output limit as a percentage.

        Args:
            percent: Power limit percentage (0-100)

        Returns:
            True if successful, False otherwise
        """
        if percent < 0 or percent > 100:
            _LOGGER.error(f"Invalid power limit: {percent}%")
            return False

        if self._session is None:
            if not await self.connect():
                return False

        # Build the payload
        # enlim=on enables power limiting
        # ac_mode=1 means percentage-based
        # ac_value1 is the percentage (0-100)
        payload = {
            "enlim": "on",
            "ac_sys": str(self.ac_capacity_w),
            "ac_mode": "1",
            "ac_value1": str(percent),
            "ac_value2": "0",
            "em_ml": "0",
            "ac_value3": "0",
            "drm_sp": "16.67",
        }

        try:
            async with self._session.post(
                f"{self.base_url}/pwrlim.cgi",
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as response:
                if response.status == 200:
                    self._current_power_percent = percent
                    _LOGGER.info(f"Zeversolar power limit set to {percent}%")
                    return True
                else:
                    _LOGGER.error(f"Failed to set power limit, status: {response.status}")
                    return False

        except aiohttp.ClientError as e:
            _LOGGER.error(f"HTTP error setting power limit: {e}")
            return False
        except Exception as e:
            _LOGGER.error(f"Unexpected error setting power limit: {e}")
            return False

    async def curtail(self) -> bool:
        """Curtail (stop) inverter production.

        Sets power output to 0%.
        """
        _LOGGER.info(f"Curtailing Zeversolar at {self.host}")
        return await self._set_power_limit(0)

    async def restore(self) -> bool:
        """Restore normal inverter operation.

        Sets power output to 100%.
        """
        _LOGGER.info(f"Restoring Zeversolar at {self.host}")
        return await self._set_power_limit(100)

    async def get_status(self) -> InverterState:
        """Get current inverter status."""
        if self._session is None:
            if not await self.connect():
                return InverterState(
                    status=InverterStatus.OFFLINE,
                    is_curtailed=False,
                    error_message="Failed to connect",
                )

        try:
            # Refresh settings from inverter
            async with self._session.get(f"{self.base_url}/adv.cgi") as response:
                if response.status == 200:
                    text = await response.text()
                    self._parse_adv_response(text)

            is_curtailed = self._current_power_percent < 100
            status = InverterStatus.CURTAILED if is_curtailed else InverterStatus.ONLINE

            return InverterState(
                status=status,
                is_curtailed=is_curtailed,
                power_limit_percent=self._current_power_percent,
                attributes={
                    "ac_capacity_w": self.ac_capacity_w,
                    "brand": "zeversolar",
                    "model": self.model,
                },
            )

        except Exception as e:
            _LOGGER.error(f"Failed to get Zeversolar status: {e}")
            return InverterState(
                status=InverterStatus.ERROR,
                is_curtailed=False,
                error_message=str(e),
            )

    async def test_connection(self) -> tuple[bool, str]:
        """Test connection to the inverter."""
        try:
            if await self.connect():
                state = await self.get_status()
                await self.disconnect()
                return True, f"Connected to Zeversolar. Power limit: {state.power_limit_percent}%"
            return False, "Failed to connect to Zeversolar"
        except Exception as e:
            _LOGGER.error(f"Zeversolar connection test failed: {e}")
            return False, f"Connection error: {str(e)}"

    async def __aenter__(self):
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.disconnect()
