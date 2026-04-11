"""Solax inverter controller for AC-coupled curtailment.

Two modes of operation:
1. Standalone (direct Modbus TCP) — no extra integration needed
2. Entity mode — uses wills106/homeassistant-solax-modbus entities as fallback

Export control register 0x42 sets the user export limit in watts.
Factory limit register 0xB5 stores the default maximum.

Reference: https://github.com/wills106/homeassistant-solax-modbus
"""

import asyncio
import logging
from typing import Optional

from .base import InverterController, InverterState, InverterStatus

_LOGGER = logging.getLogger(__name__)

# Modbus registers
REG_EXPORT_CONTROL_USER_LIMIT = 0x42  # Holding register, writable (W)
REG_EXPORT_CONTROL_FACTORY_LIMIT = 0xB5  # Input register, read-only (W)

# Default entity IDs from solax-modbus integration (fallback mode)
DEFAULT_EXPORT_LIMIT_ENTITY = "number.solax_export_control_user_limit"
DEFAULT_FACTORY_LIMIT_ENTITY = "sensor.solax_export_control_factory_limit"

# Default max export for fallback when factory limit unavailable
DEFAULT_MAX_EXPORT_W = 5000


class SolaxController(InverterController):
    """Controller for Solax inverters.

    Prefers direct Modbus TCP when host is configured. Falls back to
    HA entity service calls if the solax-modbus integration is installed.

    Supports load-following curtailment via export_control_user_limit (reg 0x42).
    """

    TIMEOUT_SECONDS = 10.0

    def __init__(
        self,
        host: str,
        port: int = 502,
        slave_id: int = 1,
        model: Optional[str] = None,
        hass=None,
    ):
        super().__init__(host, port, slave_id, model)
        self._hass = hass
        self._client = None
        self._lock = asyncio.Lock()
        self._original_limit: float | None = None
        self._use_entity_mode = False

    async def connect(self) -> bool:
        """Connect via Modbus TCP, or fall back to HA entity mode."""
        # Try direct Modbus first (standalone mode)
        if self.host and self.host not in ("", "0.0.0.0", "none"):
            try:
                from pymodbus.client import AsyncModbusTcpClient
                import pymodbus

                try:
                    _ver = tuple(int(x) for x in pymodbus.__version__.split(".")[:2])
                    self._slave_param = "device_id" if _ver >= (3, 9) else "slave"
                except Exception:
                    self._slave_param = "slave"

                self._client = AsyncModbusTcpClient(
                    host=self.host,
                    port=self.port,
                    timeout=self.TIMEOUT_SECONDS,
                )
                connected = await self._client.connect()
                if connected:
                    self._connected = True
                    self._use_entity_mode = False
                    _LOGGER.info(
                        "Solax connected via Modbus TCP at %s:%d (slave %d)",
                        self.host,
                        self.port,
                        self.slave_id,
                    )
                    return True
                else:
                    _LOGGER.warning(
                        "Modbus TCP connection to %s:%d failed, trying entity mode",
                        self.host,
                        self.port,
                    )
            except ImportError:
                _LOGGER.debug("pymodbus not available, using entity mode")
            except Exception as e:
                _LOGGER.warning("Modbus TCP error: %s, trying entity mode", e)

        # Fall back to HA entity mode
        if self._hass:
            state = self._hass.states.get(DEFAULT_EXPORT_LIMIT_ENTITY)
            if state:
                self._connected = True
                self._use_entity_mode = True
                _LOGGER.info(
                    "Solax connected via HA entity: %s",
                    DEFAULT_EXPORT_LIMIT_ENTITY,
                )
                return True

        _LOGGER.error(
            "Solax: no Modbus connection and no HA entity found (%s). "
            "Configure the inverter IP or install homeassistant-solax-modbus.",
            DEFAULT_EXPORT_LIMIT_ENTITY,
        )
        return False

    async def disconnect(self) -> None:
        """Disconnect Modbus client."""
        if self._client:
            self._client.close()
            self._client = None
        self._connected = False

    async def _read_register(self, address: int, input_reg: bool = False) -> int | None:
        """Read a single Modbus register."""
        if not self._client or not self._client.connected:
            return None
        try:
            kwargs = {self._slave_param: self.slave_id}
            if input_reg:
                result = await self._client.read_input_registers(address, 1, **kwargs)
            else:
                result = await self._client.read_holding_registers(address, 1, **kwargs)
            if result.isError():
                _LOGGER.error("Solax read register 0x%X error: %s", address, result)
                return None
            return result.registers[0]
        except Exception as e:
            _LOGGER.error("Solax read register 0x%X exception: %s", address, e)
            return None

    async def _write_register(self, address: int, value: int) -> bool:
        """Write a single Modbus register."""
        if not self._client or not self._client.connected:
            return False
        try:
            kwargs = {self._slave_param: self.slave_id}
            result = await self._client.write_register(address, value, **kwargs)
            if result.isError():
                _LOGGER.error(
                    "Solax write register 0x%X=%d error: %s", address, value, result
                )
                return False
            return True
        except Exception as e:
            _LOGGER.error(
                "Solax write register 0x%X=%d exception: %s", address, value, e
            )
            return False

    async def _get_factory_limit(self) -> float:
        """Read factory export limit."""
        if self._use_entity_mode and self._hass:
            state = self._hass.states.get(DEFAULT_FACTORY_LIMIT_ENTITY)
            if state and state.state not in ("unavailable", "unknown"):
                try:
                    return float(state.state)
                except (ValueError, TypeError):
                    pass
        else:
            val = await self._read_register(
                REG_EXPORT_CONTROL_FACTORY_LIMIT, input_reg=True
            )
            if val is not None:
                return float(val)
        return DEFAULT_MAX_EXPORT_W

    async def _get_current_limit(self) -> float | None:
        """Read current export limit."""
        if self._use_entity_mode and self._hass:
            state = self._hass.states.get(DEFAULT_EXPORT_LIMIT_ENTITY)
            if state and state.state not in ("unavailable", "unknown"):
                try:
                    return float(state.state)
                except (ValueError, TypeError):
                    pass
            return None
        else:
            val = await self._read_register(REG_EXPORT_CONTROL_USER_LIMIT)
            return float(val) if val is not None else None

    async def _set_export_limit(self, watts: int) -> bool:
        """Set export limit in watts."""
        if self._use_entity_mode and self._hass:
            try:
                await self._hass.services.async_call(
                    "number",
                    "set_value",
                    {"entity_id": DEFAULT_EXPORT_LIMIT_ENTITY, "value": watts},
                    blocking=True,
                )
                return True
            except Exception as e:
                _LOGGER.error("Failed to set Solax export limit via entity: %s", e)
                return False
        else:
            return await self._write_register(REG_EXPORT_CONTROL_USER_LIMIT, watts)

    async def get_status(self) -> InverterState:
        """Get current inverter state."""
        current = await self._get_current_limit()
        factory = await self._get_factory_limit()

        if current is None:
            return InverterState(
                status=InverterStatus.OFFLINE,
                is_curtailed=False,
            )

        is_curtailed = current < factory
        return InverterState(
            status=InverterStatus.CURTAILED if is_curtailed else InverterStatus.ONLINE,
            is_curtailed=is_curtailed,
            power_output_w=current,
            attributes={
                "export_limit_w": current,
                "factory_limit_w": factory,
                "mode": "entity" if self._use_entity_mode else "modbus",
            },
        )

    async def curtail(
        self,
        home_load_w: float | None = None,
        rated_capacity_w: float | None = None,
    ) -> bool:
        """Curtail inverter export.

        Args:
            home_load_w: Home load in watts for load-following. None = zero export.
            rated_capacity_w: Not used.
        """
        async with self._lock:
            # Save factory limit for restore
            if self._original_limit is None:
                self._original_limit = await self._get_factory_limit()

            target_w = (
                max(0, int(home_load_w)) if home_load_w and home_load_w > 0 else 0
            )

            success = await self._set_export_limit(target_w)
            if success:
                _LOGGER.info(
                    "Solax export limit set to %dW (factory %dW) [%s mode]",
                    target_w,
                    int(self._original_limit),
                    "entity" if self._use_entity_mode else "modbus",
                )
            return success

    async def restore(self) -> bool:
        """Restore inverter to factory export limit."""
        async with self._lock:
            restore_w = self._original_limit or await self._get_factory_limit()

            success = await self._set_export_limit(int(restore_w))
            if success:
                _LOGGER.info("Solax export limit restored to %dW", int(restore_w))
                self._original_limit = None
            return success
