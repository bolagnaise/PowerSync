"""Sigenergy inverter controller via Modbus TCP.

Supports Sigenergy hybrid inverter systems for DC solar curtailment.
Uses the plant-level PV power limit and active power percentage registers.

Reference: https://github.com/TypQxQ/Sigenergy-Local-Modbus
"""
import asyncio
from contextlib import asynccontextmanager
import logging
from typing import AsyncIterator, ClassVar, Optional

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


class SigenergyController(InverterController):
    """Controller for Sigenergy hybrid inverter systems via Modbus TCP.

    Uses Modbus TCP to communicate directly with the Sigenergy system
    for DC solar curtailment control.
    """

    # Modbus register addresses - use FULL addresses (pymodbus handles protocol details)
    # Reference: https://github.com/TypQxQ/Sigenergy-Local-Modbus

    # === PLANT-LEVEL REGISTERS (slave ID 247) ===
    # Holding registers (read/write)
    REG_PV_MAX_POWER_LIMIT = 40036        # PV max power limit (U32, gain 1000, kW)
    REG_ACTIVE_POWER_PCT_TARGET = 40005   # Active power % target (S16, gain 100)
    REG_ACTIVE_POWER_FIXED_TARGET = 40001 # Active power fixed target (S32, gain 1000, kW)
    REG_GRID_EXPORT_LIMIT = 40038         # Grid export limit (U32, gain 1000)
    REG_PCS_EXPORT_LIMIT = 40042          # PCS export limit (U32, gain 1000)
    REG_ESS_MAX_CHARGE_LIMIT = 40032      # ESS max charging (U32, gain 1000, kW)
    REG_ESS_MAX_DISCHARGE_LIMIT = 40034   # ESS max discharging (U32, gain 1000, kW)
    REG_REMOTE_EMS_ENABLE = 40029         # Remote EMS enable (U16: 0=disabled, 1=enabled)
    REG_REMOTE_EMS_CONTROL_MODE = 40031   # Remote EMS control mode (U16, see REMOTE_EMS_MODE_* constants)
    REG_ESS_BACKUP_SOC = 40046            # ESS backup SOC (U16, gain 10, %)
    REG_ESS_CHARGE_CUTOFF_SOC = 40047     # ESS charge cut-off SOC (U16, gain 10, %)
    REG_ESS_DISCHARGE_CUTOFF_SOC = 40048  # ESS discharge cut-off SOC (U16, gain 10, %)

    # Input registers (read-only) - Real-time power
    REG_PV_POWER = 30035                  # PV power (S32, gain 1000, kW) — DC-coupled only
    REG_THIRD_PARTY_PV_POWER = 30194     # Third-party inverter power (S32, gain 1000, kW) — AC-coupled (Smart Port)
    REG_ACTIVE_POWER = 30031              # Active power (S32, gain 1000, kW)
    REG_ESS_SOC = 30014                   # Battery SOC (U16, gain 10, %)
    REG_ESS_POWER = 30037                 # Battery power (S32, gain 1000, kW)
    REG_RUNNING_STATE = 30051             # Plant running state (U16)
    REG_GRID_SENSOR_POWER = 30005         # Grid sensor active power (S32, gain 1000, kW)
    REG_EMS_WORK_MODE = 30003             # EMS work mode (U16)

    # Input registers (read-only) - Battery health
    REG_ESS_RATED_CAPACITY = 30083        # ESS rated energy capacity (U32, gain 100, kWh)
    REG_ESS_SOH = 30087                   # Battery State of Health (U16, gain 10, %)

    # Input registers (read-only) - Energy totals (U64 = 4 registers, gain 100)
    REG_ACCUMULATED_PV_ENERGY = 30088     # Total PV generation (U64, gain 100, kWh)
    REG_DAILY_CONSUMED_ENERGY = 30092     # Daily load consumption (U32, gain 100, kWh)
    REG_ACCUMULATED_CONSUMED_ENERGY = 30094  # Total load consumption (U64, gain 100, kWh)
    REG_ACCUMULATED_BATTERY_CHARGE = 30200   # Total battery charged (U64, gain 100, kWh)
    REG_ACCUMULATED_BATTERY_DISCHARGE = 30204  # Total battery discharged (U64, gain 100, kWh)
    REG_ACCUMULATED_GRID_IMPORT = 30216   # Total grid import (U64, gain 100, kWh)
    REG_ACCUMULATED_GRID_EXPORT = 30220   # Total grid export (U64, gain 100, kWh)

    # === INVERTER-LEVEL REGISTERS (slave ID 1) ===
    # Fallback if plant registers don't work
    REG_INV_SOC = 30601                   # Inverter battery SOC (U16, gain 10, %)
    REG_INV_SOH = 30602                   # Inverter battery SOH (U16, gain 10, %)
    REG_INV_ACTIVE_POWER = 30587          # Inverter active power (S32, gain 1000, kW)
    REG_INV_ESS_POWER = 30599             # Inverter battery power (S32, gain 1000, kW)
    REG_INV_PV_POWER = 31035              # Inverter PV power (S32, gain 1000, kW)

    # Constants
    GAIN_POWER = 1000  # kW → scaled value (multiply to write, divide to read)
    GAIN_PERCENT = 100  # % → scaled value
    GAIN_SOC = 10      # % → scaled value
    GAIN_ENERGY = 100  # kWh → scaled value for energy registers

    # Remote EMS Control Modes (register 40031)
    # Remote EMS can be left permanently enabled — mode 2 is equivalent
    # to native self-consumption and allows instant transitions to other modes.
    REMOTE_EMS_MODE_PCS_REMOTE = 0          # PCS remote control
    REMOTE_EMS_MODE_STANDBY = 1             # Standby
    REMOTE_EMS_MODE_SELF_CONSUMPTION = 2    # Maximum self-consumption
    REMOTE_EMS_MODE_CHARGE_GRID = 3         # Command charging (grid first)
    REMOTE_EMS_MODE_CHARGE_PV = 4           # Command charging (PV first)
    REMOTE_EMS_MODE_DISCHARGE_PV = 5        # Command discharging (PV first)
    REMOTE_EMS_MODE_DISCHARGE_ESS = 6       # Command discharging (ESS first)

    # ESS rated power registers (input, read-only)
    REG_ESS_RATED_CHARGE_POWER = 30068    # ESS rated charge power (U32, gain 1000, kW)
    REG_ESS_RATED_DISCHARGE_POWER = 30070 # ESS rated discharge power (U32, gain 1000, kW)

    # Curtailment values
    # Zero export lets the inverter self-curtail PV at hardware speed —
    # solar continues powering house and charging battery, only grid export is blocked.
    EXPORT_LIMIT_ZERO = 0         # Zero export
    EXPORT_LIMIT_UNLIMITED = 0xFFFFFFFE  # Unlimited export (normal operation)
    EXPORT_LIMIT_INVALID = 0xFFFFFFFF    # Invalid register value (per Sigenergy Modbus docs)
    PV_POWER_LIMIT_ZERO = 0       # Set PV limit to 0 kW (full shutdown - not used)
    ACTIVE_POWER_PCT_ZERO = 0     # 0% active power

    # Default Modbus settings
    # Sigenergy uses different slave IDs for different register levels:
    # - Plant-level registers (30001-30099): Slave ID 247
    # - Inverter-level registers (30500+): Slave ID 1 (or specific inverter address)
    DEFAULT_PORT = 502
    DEFAULT_SLAVE_ID = 247  # Plant address - will auto-switch to 1 for inverter registers
    DEFAULT_INVERTER_SLAVE_ID = 1  # Default inverter address
    TIMEOUT_SECONDS = 10.0
    _MODBUS_LOCKS: ClassVar[dict[tuple[str, int], asyncio.Lock]] = {}

    def __init__(
        self,
        host: str,
        port: int = 502,
        slave_id: int = 1,
        model: Optional[str] = None,
        max_export_limit_kw: Optional[float] = None,
        configured_charge_rate_limit_kw: Optional[float] = None,
        configured_discharge_rate_limit_kw: Optional[float] = None,
    ):
        """Initialize Sigenergy controller.

        Args:
            host: IP address of Sigenergy system
            port: Modbus TCP port (default: 502)
            slave_id: Modbus slave ID (default: 1)
            model: Sigenergy model (optional)
            max_export_limit_kw: User-configured DNSP export limit in kW (None = auto-detect)
            configured_charge_rate_limit_kw: User-configured normal charge cap in kW
            configured_discharge_rate_limit_kw: User-configured normal discharge cap in kW
        """
        super().__init__(host, port, slave_id, model)
        self._client: Optional[AsyncModbusTcpClient] = None
        self._lock = asyncio.Lock()
        self._original_pv_limit: Optional[int] = None  # Store original limit for restore
        self._use_inverter_registers: Optional[bool] = None  # None=unknown, True=inverter, False=plant
        self._configured_max_export_limit_kw = max_export_limit_kw
        self._configured_charge_rate_limit_kw = configured_charge_rate_limit_kw
        self._configured_discharge_rate_limit_kw = configured_discharge_rate_limit_kw
        self._restore_backup_reserve_pct: Optional[int] = None  # Set by optimizer for restore_normal
        # For AC Charger setups: AC Charger is slave 1, inverter is slave 2
        # Use user-configured slave_id for inverter registers instead of hardcoded default
        # This allows users with AC Chargers to specify slave 2 and have it work correctly
        self._inverter_slave_id = slave_id if slave_id != self.DEFAULT_SLAVE_ID else self.DEFAULT_INVERTER_SLAVE_ID
        self._modbus_transaction_depth = 0
        self._modbus_transaction_owner = None

    @property
    def _modbus_lock(self) -> asyncio.Lock:
        """Shared lock for all Sigenergy Modbus clients talking to one host."""
        key = (self.host, int(self.port))
        lock = self._MODBUS_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._MODBUS_LOCKS[key] = lock
        return lock

    @asynccontextmanager
    async def _modbus_transaction(self) -> AsyncIterator[None]:
        """Serialize multi-step Modbus transactions across controller instances."""
        task = asyncio.current_task()
        if self._modbus_transaction_owner is task:
            self._modbus_transaction_depth += 1
            try:
                yield
            finally:
                self._modbus_transaction_depth -= 1
            return

        async with self._modbus_lock:
            self._modbus_transaction_owner = task
            self._modbus_transaction_depth = 1
            try:
                yield
            finally:
                self._modbus_transaction_depth = 0
            self._modbus_transaction_owner = None

    async def connect(self) -> bool:
        """Connect to the Sigenergy system via Modbus TCP."""
        async with self._modbus_transaction():
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
                        _LOGGER.info(
                            f"Connected to Sigenergy system at {self.host}:{self.port} "
                            f"(plant slave={self.slave_id}, inverter slave={self._inverter_slave_id})"
                        )
                    else:
                        _LOGGER.error(f"Failed to connect to Sigenergy at {self.host}:{self.port}")

                    return connected

                except Exception as e:
                    _LOGGER.error(f"Error connecting to Sigenergy: {e}")
                    self._connected = False
                    return False

    async def disconnect(self) -> None:
        """Disconnect from the Sigenergy system."""
        async with self._modbus_transaction():
            async with self._lock:
                if self._client:
                    self._client.close()
                    self._client = None
                self._connected = False
                _LOGGER.debug(f"Disconnected from Sigenergy at {self.host}")

    async def _write_holding_registers(self, address: int, values: list[int], slave_id: Optional[int] = None) -> bool:
        """Write values to holding registers.

        Args:
            address: Starting register address (0-indexed)
            values: List of values to write
            slave_id: Optional slave ID override (default: DEFAULT_SLAVE_ID=247 for plant registers)

        Returns:
            True if write successful
        """
        async with self._modbus_transaction():
            effective_slave = slave_id if slave_id is not None else self.DEFAULT_SLAVE_ID
            last_error: Exception | None = None

            for attempt in range(2):
                if not self._client or not self._client.connected:
                    if not await self.connect():
                        return False

                try:
                    result = await self._client.write_registers(
                        address=address,
                        values=values,
                        **{_SLAVE_PARAM: effective_slave},
                    )

                    if result.isError():
                        _LOGGER.error(f"Modbus write error at register {address}: {result}")
                        return False

                    _LOGGER.debug(f"Successfully wrote {values} to register {address}")
                    return True

                except Exception as e:
                    last_error = e
                    if attempt == 0:
                        _LOGGER.debug(
                            "Sigenergy write to register %s failed (%s), reconnecting once",
                            address,
                            e,
                        )
                        await self.disconnect()
                        continue

            _LOGGER.error(f"Error writing to register {address}: {last_error}")
            return False

    async def _read_holding_registers(self, address: int, count: int = 1, slave_id: Optional[int] = None) -> Optional[list]:
        """Read values from holding registers.

        Args:
            address: Starting register address (0-indexed)
            count: Number of registers to read
            slave_id: Optional slave ID override (default: DEFAULT_SLAVE_ID=247 for plant registers)

        Returns:
            List of register values or None on error
        """
        async with self._modbus_transaction():
            if not self._client or not self._client.connected:
                if not await self.connect():
                    return None

            effective_slave = slave_id if slave_id is not None else self.DEFAULT_SLAVE_ID

            try:
                result = await self._client.read_holding_registers(
                    address=address,
                    count=count,
                    **{_SLAVE_PARAM: effective_slave},
                )

                if result.isError():
                    _LOGGER.debug(f"Modbus read error at holding register {address}: {result}")
                    return None

                return result.registers

            except ModbusException as e:
                _LOGGER.debug(f"Modbus exception reading holding register {address}: {e}")
                return None
            except Exception as e:
                _LOGGER.debug(f"Error reading holding register {address}: {e}")
                return None

    async def _read_input_registers(self, address: int, count: int = 1, slave_id: Optional[int] = None) -> Optional[list]:
        """Read values from input registers.

        Args:
            address: Starting register address (0-indexed)
            count: Number of registers to read
            slave_id: Optional slave ID override (default: self.slave_id)

        Returns:
            List of register values or None on error
        """
        async with self._modbus_transaction():
            if not self._client or not self._client.connected:
                if not await self.connect():
                    return None

            effective_slave = slave_id if slave_id is not None else self.slave_id

            try:
                result = await self._client.read_input_registers(
                    address=address,
                    count=count,
                    **{_SLAVE_PARAM: effective_slave},
                )

                if result.isError():
                    _LOGGER.debug(f"Modbus read error at input register {address} [slave={effective_slave}]: {result}")
                    return None

                return result.registers

            except ModbusException as e:
                _LOGGER.debug(f"Modbus exception reading input register {address} [slave={effective_slave}]: {e}")
                return None
            except Exception as e:
                _LOGGER.debug(f"Error reading input register {address} [slave={effective_slave}]: {e}")
                return None

    def _to_signed32(self, high: int, low: int) -> int:
        """Convert two unsigned 16-bit registers to signed 32-bit."""
        value = (high << 16) | low
        if value >= 0x80000000:
            value -= 0x100000000
        return value

    def _to_unsigned32(self, high: int, low: int) -> int:
        """Convert two unsigned 16-bit registers to unsigned 32-bit."""
        return (high << 16) | low

    def _from_unsigned32(self, value: int) -> list[int]:
        """Convert unsigned 32-bit to two 16-bit registers [high, low]."""
        high = (value >> 16) & 0xFFFF
        low = value & 0xFFFF
        return [high, low]

    def _from_signed32(self, value: int) -> list[int]:
        """Convert signed 32-bit to two 16-bit registers [high, low]."""
        return self._from_unsigned32(value & 0xFFFFFFFF)

    def _to_unsigned64(self, regs: list[int]) -> int:
        """Convert four unsigned 16-bit registers to unsigned 64-bit.

        Register order: [high_high, high_low, low_high, low_low]
        """
        if len(regs) < 4:
            return 0
        return (regs[0] << 48) | (regs[1] << 32) | (regs[2] << 16) | regs[3]

    async def _get_current_pv_limit(self) -> Optional[int]:
        """Read current PV power limit."""
        regs = await self._read_holding_registers(self.REG_PV_MAX_POWER_LIMIT, 2)
        if regs and len(regs) >= 2:
            return self._to_unsigned32(regs[0], regs[1])
        return None

    async def _get_current_export_limit(self) -> Optional[int]:
        """Read current grid export limit."""
        regs = await self._read_holding_registers(self.REG_GRID_EXPORT_LIMIT, 2)
        if regs and len(regs) >= 2:
            return self._to_unsigned32(regs[0], regs[1])
        return None

    async def _get_effective_export_safety_cap_kw(self) -> Optional[float]:
        """Determine the effective export safety cap from multiple sources.

        Priority chain:
        1. User-configured cap (CONF_SIGENERGY_EXPORT_LIMIT_KW)
        2. ESS rated charge/discharge power (input registers 30079/30081) — takes the lower
        3. ESS max charge/discharge limits (holding registers 40032/40034) — takes the lower
        4. PCS export limit register (40042) — when valid
        5. Grid export limit register (40038) — when valid and finite

        Returns:
            Safety cap in kW, or None if no cap could be determined
        """
        # 1. User-configured cap takes priority
        if self._configured_max_export_limit_kw is not None:
            return self._configured_max_export_limit_kw

        if not await self.connect():
            return None

        # 2. ESS rated power (input registers — hardware specs)
        rated_charge_regs = await self._read_input_registers(self.REG_ESS_RATED_CHARGE_POWER, 2)
        rated_discharge_regs = await self._read_input_registers(self.REG_ESS_RATED_DISCHARGE_POWER, 2)
        if rated_charge_regs and len(rated_charge_regs) >= 2 and rated_discharge_regs and len(rated_discharge_regs) >= 2:
            rated_charge = self._to_unsigned32(rated_charge_regs[0], rated_charge_regs[1])
            rated_discharge = self._to_unsigned32(rated_discharge_regs[0], rated_discharge_regs[1])
            if rated_charge > 0 and rated_discharge > 0:
                cap_kw = min(rated_charge, rated_discharge) / self.GAIN_POWER
                _LOGGER.debug(f"Export safety cap from ESS rated power: {cap_kw} kW")
                return cap_kw

        # 3. ESS max charge/discharge limits (holding registers — current config)
        max_charge_regs = await self._read_holding_registers(self.REG_ESS_MAX_CHARGE_LIMIT, 2)
        max_discharge_regs = await self._read_holding_registers(self.REG_ESS_MAX_DISCHARGE_LIMIT, 2)
        if max_charge_regs and len(max_charge_regs) >= 2 and max_discharge_regs and len(max_discharge_regs) >= 2:
            max_charge = self._to_unsigned32(max_charge_regs[0], max_charge_regs[1])
            max_discharge = self._to_unsigned32(max_discharge_regs[0], max_discharge_regs[1])
            if 0 < max_charge < self.EXPORT_LIMIT_UNLIMITED and 0 < max_discharge < self.EXPORT_LIMIT_UNLIMITED:
                cap_kw = min(max_charge, max_discharge) / self.GAIN_POWER
                _LOGGER.debug(f"Export safety cap from ESS max limits: {cap_kw} kW")
                return cap_kw

        # 4. PCS export limit (holding register 40042)
        pcs_regs = await self._read_holding_registers(self.REG_PCS_EXPORT_LIMIT, 2)
        if pcs_regs and len(pcs_regs) >= 2:
            pcs_limit = self._to_unsigned32(pcs_regs[0], pcs_regs[1])
            if 0 < pcs_limit < self.EXPORT_LIMIT_UNLIMITED and pcs_limit != self.EXPORT_LIMIT_INVALID:
                cap_kw = pcs_limit / self.GAIN_POWER
                _LOGGER.debug(f"Export safety cap from PCS export limit: {cap_kw} kW")
                return cap_kw

        # 5. Current grid export limit (if finite)
        export_regs = await self._read_holding_registers(self.REG_GRID_EXPORT_LIMIT, 2)
        if export_regs and len(export_regs) >= 2:
            export_limit = self._to_unsigned32(export_regs[0], export_regs[1])
            if 0 < export_limit < self.EXPORT_LIMIT_UNLIMITED and export_limit != self.EXPORT_LIMIT_INVALID:
                cap_kw = export_limit / self.GAIN_POWER
                _LOGGER.debug(f"Export safety cap from grid export limit: {cap_kw} kW")
                return cap_kw

        return None

    async def resolve_export_safety_cap_kw(self) -> Optional[float]:
        """Resolve and return the export safety cap.

        Public method for use during setup to auto-detect and persist the cap.
        """
        cap = await self._get_effective_export_safety_cap_kw()
        if cap is not None:
            _LOGGER.info(f"Resolved Sigenergy export safety cap: {cap} kW")
        else:
            _LOGGER.warning("Could not resolve Sigenergy export safety cap from any source")
        return cap

    async def curtail(
        self,
        home_load_w: Optional[float] = None,
        rated_capacity_w: Optional[float] = None,
    ) -> bool:
        """Curtail solar export by setting export limit to zero.

        Sets the grid export limit to 0 kW. The Sigenergy inverter handles
        PV curtailment at hardware speed — solar continues to power the house
        and charge the battery, only grid export is blocked.

        Args:
            home_load_w: Unused (kept for interface compatibility)
            rated_capacity_w: Unused (kept for interface compatibility)

        Returns:
            True if curtailment successful
        """
        try:
            if not await self.connect():
                _LOGGER.error("Cannot curtail: failed to connect to Sigenergy")
                return False

            # Store original export limit if not already stored. A read of
            # EXPORT_LIMIT_ZERO here means the inverter is already curtailed
            # (e.g. a fresh controller instance after a config reload
            # mid-curtailment) — that is not a real "original" value, so
            # don't capture it or the true DNSP cap would be lost on restore.
            if self._original_pv_limit is None:
                current_limit = await self._get_current_export_limit()
                if current_limit is not None and current_limit > self.EXPORT_LIMIT_ZERO:
                    self._original_pv_limit = current_limit
                    limit_str = f"{self._original_pv_limit / self.GAIN_POWER} kW" if self._original_pv_limit < self.EXPORT_LIMIT_UNLIMITED else "unlimited"
                    _LOGGER.info(f"Stored original export limit: {limit_str}")

            _LOGGER.info(f"Curtailing Sigenergy at {self.host} (zero export)")

            # Set export limit to 0 — inverter self-curtails PV at hardware speed
            values = self._from_unsigned32(self.EXPORT_LIMIT_ZERO)
            success = await self._write_holding_registers(self.REG_GRID_EXPORT_LIMIT, values)

            if success:
                _LOGGER.info(f"Successfully set zero export on Sigenergy")
            else:
                _LOGGER.error(f"Failed to curtail Sigenergy at {self.host}")

            return success

        except Exception as e:
            _LOGGER.error(f"Error curtailing Sigenergy: {e}")
            return False

    async def restore(self) -> bool:
        """Restore normal export operation.

        Restores grid export limit to the original value, safety cap, or unlimited.

        Returns:
            True if restore successful
        """
        _LOGGER.info(f"Restoring Sigenergy export at {self.host}")

        try:
            if not await self.connect():
                _LOGGER.error("Cannot restore: failed to connect to Sigenergy")
                return False

            # Use stored original limit, or fall back to safety cap, or unlimited.
            # Note: `is not None` (not truthy) — a stored 0 is a legitimate
            # captured limit, distinct from "no original captured" (None).
            if self._original_pv_limit is not None and self._original_pv_limit < self.EXPORT_LIMIT_UNLIMITED:
                restore_value = self._original_pv_limit
            else:
                safety_cap = await self._get_effective_export_safety_cap_kw()
                if safety_cap is not None:
                    restore_value = int(safety_cap * self.GAIN_POWER)
                else:
                    restore_value = self.EXPORT_LIMIT_UNLIMITED
            limit_str = f"{restore_value / self.GAIN_POWER} kW" if restore_value < self.EXPORT_LIMIT_UNLIMITED else "unlimited"
            _LOGGER.info(f"Restoring export limit to: {limit_str}")

            values = self._from_unsigned32(restore_value)
            success = await self._write_holding_registers(self.REG_GRID_EXPORT_LIMIT, values)

            if success:
                _LOGGER.info(f"Successfully restored Sigenergy export at {self.host}")
                # Clear stored limit after successful restore
                self._original_pv_limit = None
                # Brief delay then verify
                await asyncio.sleep(1)
                state = await self.get_status()
                if not state.is_curtailed:
                    _LOGGER.info("Restore verified - normal export resumed")
                else:
                    _LOGGER.warning("Restore command sent but may take time to resume")
            else:
                _LOGGER.error(f"Failed to restore Sigenergy at {self.host}")

            return success

        except Exception as e:
            _LOGGER.error(f"Error restoring Sigenergy: {e}")
            return False

    async def _read_plant_registers(self) -> dict:
        """Try to read plant-level registers."""
        attrs = {}
        success_count = 0

        # Read PV power (S32, 2 registers) — DC-coupled solar only
        pv_power_regs = await self._read_input_registers(self.REG_PV_POWER, 2)
        if pv_power_regs and len(pv_power_regs) >= 2:
            pv_power_kw = self._to_signed32(pv_power_regs[0], pv_power_regs[1]) / self.GAIN_POWER
            attrs["pv_power_kw"] = round(pv_power_kw, 2)
            attrs["pv_power_w"] = pv_power_kw * 1000
            success_count += 1

        # Read third-party PV power (S32, 2 registers) — AC-coupled solar via Smart Port
        tp_pv_regs = await self._read_input_registers(self.REG_THIRD_PARTY_PV_POWER, 2)
        if tp_pv_regs and len(tp_pv_regs) >= 2:
            tp_pv_kw = self._to_signed32(tp_pv_regs[0], tp_pv_regs[1]) / self.GAIN_POWER
            # Only include positive values (generation); 0 is normal for systems without Smart Port
            if tp_pv_kw > 0:
                attrs["third_party_pv_power_kw"] = round(tp_pv_kw, 2)

        # Read battery SOC (U16)
        soc_regs = await self._read_input_registers(self.REG_ESS_SOC, 1)
        if soc_regs:
            attrs["battery_soc"] = round(soc_regs[0] / self.GAIN_SOC, 1)
            success_count += 1

        # Read grid sensor power (S32, 2 registers)
        grid_power_regs = await self._read_input_registers(self.REG_GRID_SENSOR_POWER, 2)
        if grid_power_regs and len(grid_power_regs) >= 2:
            grid_power_kw = self._to_signed32(grid_power_regs[0], grid_power_regs[1]) / self.GAIN_POWER
            attrs["grid_power_kw"] = round(grid_power_kw, 2)
            success_count += 1

        # Read battery power (S32, 2 registers)
        ess_power_regs = await self._read_input_registers(self.REG_ESS_POWER, 2)
        if ess_power_regs and len(ess_power_regs) >= 2:
            ess_power_kw = self._to_signed32(ess_power_regs[0], ess_power_regs[1]) / self.GAIN_POWER
            attrs["battery_power_kw"] = round(ess_power_kw, 2)
            success_count += 1

        # Read battery SOH (U16, gain 10)
        soh_regs = await self._read_input_registers(self.REG_ESS_SOH, 1)
        if soh_regs:
            attrs["battery_soh"] = round(soh_regs[0] / self.GAIN_SOC, 1)

        # Read rated capacity (U32, gain 100, kWh)
        capacity_regs = await self._read_input_registers(self.REG_ESS_RATED_CAPACITY, 2)
        if capacity_regs and len(capacity_regs) >= 2:
            capacity_kwh = self._to_unsigned32(capacity_regs[0], capacity_regs[1]) / self.GAIN_ENERGY
            attrs["battery_capacity_kwh"] = round(capacity_kwh, 2)

        attrs["_success_count"] = success_count
        attrs["_register_level"] = "plant"
        return attrs

    async def _read_inverter_registers(self) -> dict:
        """Try to read inverter-level registers (fallback).

        Uses inverter slave ID (default: 1) instead of plant slave ID (247).
        """
        attrs = {}
        success_count = 0
        inv_slave = self._inverter_slave_id
        _LOGGER.debug(f"Reading inverter registers with slave ID {inv_slave}")

        # Read inverter PV power (S32, 2 registers)
        pv_power_regs = await self._read_input_registers(self.REG_INV_PV_POWER, 2, slave_id=inv_slave)
        if pv_power_regs and len(pv_power_regs) >= 2:
            pv_power_kw = self._to_signed32(pv_power_regs[0], pv_power_regs[1]) / self.GAIN_POWER
            attrs["pv_power_kw"] = round(pv_power_kw, 2)
            attrs["pv_power_w"] = pv_power_kw * 1000
            success_count += 1

        # Read inverter battery SOC (U16)
        soc_regs = await self._read_input_registers(self.REG_INV_SOC, 1, slave_id=inv_slave)
        if soc_regs:
            attrs["battery_soc"] = round(soc_regs[0] / self.GAIN_SOC, 1)
            success_count += 1

        # Read inverter active power (S32, 2 registers) - use as grid proxy
        active_power_regs = await self._read_input_registers(self.REG_INV_ACTIVE_POWER, 2, slave_id=inv_slave)
        if active_power_regs and len(active_power_regs) >= 2:
            active_power_kw = self._to_signed32(active_power_regs[0], active_power_regs[1]) / self.GAIN_POWER
            attrs["active_power_kw"] = round(active_power_kw, 2)
            success_count += 1

        # Read inverter battery power (S32, 2 registers)
        ess_power_regs = await self._read_input_registers(self.REG_INV_ESS_POWER, 2, slave_id=inv_slave)
        if ess_power_regs and len(ess_power_regs) >= 2:
            ess_power_kw = self._to_signed32(ess_power_regs[0], ess_power_regs[1]) / self.GAIN_POWER
            attrs["battery_power_kw"] = round(ess_power_kw, 2)
            success_count += 1

        attrs["_success_count"] = success_count
        attrs["_register_level"] = "inverter"
        attrs["_inverter_slave_id"] = inv_slave
        return attrs

    async def get_energy_summary(self) -> dict:
        """Read accumulated energy totals from Modbus registers.

        Returns a dict with lifetime and daily energy statistics in kWh.
        All U64 values use gain factor 100.
        """
        energy = {}

        try:
            if not await self.connect():
                return {"error": "Failed to connect to Sigenergy"}

            # Total PV generation (U64, 4 registers, gain 100)
            pv_regs = await self._read_input_registers(self.REG_ACCUMULATED_PV_ENERGY, 4)
            if pv_regs and len(pv_regs) >= 4:
                energy["total_pv_energy_kwh"] = round(self._to_unsigned64(pv_regs) / self.GAIN_ENERGY, 2)

            # Daily load consumption (U32, 2 registers, gain 100)
            daily_load_regs = await self._read_input_registers(self.REG_DAILY_CONSUMED_ENERGY, 2)
            if daily_load_regs and len(daily_load_regs) >= 2:
                energy["daily_load_energy_kwh"] = round(self._to_unsigned32(daily_load_regs[0], daily_load_regs[1]) / self.GAIN_ENERGY, 2)

            # Total load consumption (U64, 4 registers, gain 100)
            total_load_regs = await self._read_input_registers(self.REG_ACCUMULATED_CONSUMED_ENERGY, 4)
            if total_load_regs and len(total_load_regs) >= 4:
                energy["total_load_energy_kwh"] = round(self._to_unsigned64(total_load_regs) / self.GAIN_ENERGY, 2)

            # Total battery charged (U64, 4 registers, gain 100)
            charge_regs = await self._read_input_registers(self.REG_ACCUMULATED_BATTERY_CHARGE, 4)
            if charge_regs and len(charge_regs) >= 4:
                energy["total_battery_charged_kwh"] = round(self._to_unsigned64(charge_regs) / self.GAIN_ENERGY, 2)

            # Total battery discharged (U64, 4 registers, gain 100)
            discharge_regs = await self._read_input_registers(self.REG_ACCUMULATED_BATTERY_DISCHARGE, 4)
            if discharge_regs and len(discharge_regs) >= 4:
                energy["total_battery_discharged_kwh"] = round(self._to_unsigned64(discharge_regs) / self.GAIN_ENERGY, 2)

            # Total grid import (U64, 4 registers, gain 100)
            import_regs = await self._read_input_registers(self.REG_ACCUMULATED_GRID_IMPORT, 4)
            if import_regs and len(import_regs) >= 4:
                energy["total_grid_import_kwh"] = round(self._to_unsigned64(import_regs) / self.GAIN_ENERGY, 2)

            # Total grid export (U64, 4 registers, gain 100)
            export_regs = await self._read_input_registers(self.REG_ACCUMULATED_GRID_EXPORT, 4)
            if export_regs and len(export_regs) >= 4:
                energy["total_grid_export_kwh"] = round(self._to_unsigned64(export_regs) / self.GAIN_ENERGY, 2)

            _LOGGER.debug(f"Sigenergy energy summary: {energy}")
            return energy

        except Exception as e:
            _LOGGER.error(f"Error reading energy summary: {e}")
            return {"error": str(e)}

    async def get_status(self) -> InverterState:
        """Get current status of the Sigenergy system.

        Tries plant-level registers first, falls back to inverter-level if those fail.

        Returns:
            InverterState with current status and power readings
        """
        try:
            if not await self.connect():
                return InverterState(
                    status=InverterStatus.OFFLINE,
                    is_curtailed=False,
                    error_message="Failed to connect to Sigenergy",
                )

            attrs = {}

            # Determine which register set to use
            if self._use_inverter_registers is None:
                # First time - try plant registers, then inverter if plant fails
                plant_attrs = await self._read_plant_registers()
                if plant_attrs.get("_success_count", 0) >= 2:
                    attrs = plant_attrs
                    self._use_inverter_registers = False
                    _LOGGER.info("Sigenergy: Using plant-level registers")
                else:
                    # Try inverter-level registers with inverter slave ID
                    inv_attrs = await self._read_inverter_registers()
                    if inv_attrs.get("_success_count", 0) >= 2:
                        attrs = inv_attrs
                        self._use_inverter_registers = True
                        _LOGGER.info(f"Sigenergy: Using inverter-level registers with slave ID {self._inverter_slave_id} (plant registers unavailable)")
                    else:
                        # Neither worked - return what we have
                        attrs = plant_attrs if plant_attrs.get("_success_count", 0) > inv_attrs.get("_success_count", 0) else inv_attrs
                        _LOGGER.warning(f"Sigenergy: Limited register access (plant={plant_attrs.get('_success_count', 0)}, inverter={inv_attrs.get('_success_count', 0)})")
            elif self._use_inverter_registers:
                attrs = await self._read_inverter_registers()
            else:
                attrs = await self._read_plant_registers()

            # Clean up internal tracking fields
            attrs.pop("_success_count", None)
            register_level = attrs.pop("_register_level", "unknown")

            # Read export limit for curtailment status (only available at plant level)
            export_limit = None
            is_curtailed = False
            if not self._use_inverter_registers:
                export_limit_regs = await self._read_holding_registers(self.REG_GRID_EXPORT_LIMIT, 2)
                if export_limit_regs and len(export_limit_regs) >= 2:
                    export_limit = self._to_unsigned32(export_limit_regs[0], export_limit_regs[1])
                    is_curtailed = export_limit < 100  # Less than 0.1 kW threshold
                    if export_limit < self.EXPORT_LIMIT_UNLIMITED:
                        attrs["export_limit_kw"] = round(export_limit / self.GAIN_POWER, 2)
                    else:
                        attrs["export_limit_kw"] = "unlimited"

                # Also read PCS export limit for diagnostics
                pcs_limit_regs = await self._read_holding_registers(self.REG_PCS_EXPORT_LIMIT, 2)
                if pcs_limit_regs and len(pcs_limit_regs) >= 2:
                    pcs_limit = self._to_unsigned32(pcs_limit_regs[0], pcs_limit_regs[1])
                    if pcs_limit < self.EXPORT_LIMIT_UNLIMITED and pcs_limit != self.EXPORT_LIMIT_INVALID:
                        attrs["pcs_export_limit_kw"] = round(pcs_limit / self.GAIN_POWER, 2)

            # If we couldn't read ANY meaningful registers, the inverter is likely sleeping/offline
            if not attrs or len(attrs) == 0:
                _LOGGER.debug("Sigenergy: No register data - inverter likely sleeping")
                return InverterState(
                    status=InverterStatus.OFFLINE,
                    is_curtailed=False,
                    error_message="No register data (inverter sleeping)",
                    attributes={"host": self.host, "model": self.model or "Sigenergy"},
                )

            # Get PV power for status determination
            pv_power_w = attrs.get("pv_power_w")

            # Determine overall status
            if is_curtailed:
                status = InverterStatus.CURTAILED
                attrs["curtailment_mode"] = "zero_export"
            elif pv_power_w is not None and pv_power_w > 0:
                status = InverterStatus.ONLINE
            else:
                status = InverterStatus.ONLINE  # Connected but no PV production

            # Add model info
            attrs["model"] = self.model or "Sigenergy"
            attrs["host"] = self.host
            attrs["register_level"] = register_level

            # In zero-export mode, PV is not limited - only grid export is blocked
            self._last_state = InverterState(
                status=status,
                is_curtailed=is_curtailed,
                power_output_w=pv_power_w,
                power_limit_percent=100,  # Load-following doesn't limit PV power
                attributes=attrs,
            )

            return self._last_state

        except Exception as e:
            _LOGGER.error(f"Error getting Sigenergy status: {e}")
            return InverterState(
                status=InverterStatus.ERROR,
                is_curtailed=False,
                error_message=str(e),
            )

    async def set_pv_power_limit(self, limit_kw: float) -> bool:
        """Set a specific PV power limit.

        Args:
            limit_kw: Power limit in kW (0 = curtail, very high = no limit)

        Returns:
            True if successful
        """
        transaction = self._modbus_transaction()
        await transaction.__aenter__()
        try:
            if not await self.connect():
                return False

            # Convert kW to scaled value (multiply by gain)
            scaled_value = int(limit_kw * self.GAIN_POWER)
            if scaled_value < 0:
                scaled_value = 0
            if scaled_value > 0xFFFFFFFE:
                scaled_value = 0xFFFFFFFE  # Max valid value

            _LOGGER.info(f"Setting Sigenergy PV limit to {limit_kw} kW")
            values = self._from_unsigned32(scaled_value)
            return await self._write_holding_registers(self.REG_PV_MAX_POWER_LIMIT, values)

        except Exception as e:
            _LOGGER.error(f"Error setting PV power limit: {e}")
            return False
        finally:
            await transaction.__aexit__(None, None, None)

    async def set_export_limit(self, limit_kw: float) -> bool:
        """Set a specific grid export limit, clamped to the safety cap.

        Args:
            limit_kw: Export limit in kW (0 = no export)

        Returns:
            True if successful
        """
        transaction = self._modbus_transaction()
        await transaction.__aenter__()
        try:
            if not await self.connect():
                return False

            # Clamp to safety cap if available
            safety_cap = await self._get_effective_export_safety_cap_kw()
            if safety_cap is not None and limit_kw > safety_cap:
                _LOGGER.warning(
                    f"Requested export limit {limit_kw} kW exceeds safety cap {safety_cap} kW — clamping"
                )
                limit_kw = safety_cap

            scaled_value = int(limit_kw * self.GAIN_POWER)
            if scaled_value < 0:
                scaled_value = 0
            if scaled_value > 0xFFFFFFFE:
                scaled_value = 0xFFFFFFFE

            _LOGGER.info(f"Setting Sigenergy export limit to {limit_kw} kW")
            values = self._from_unsigned32(scaled_value)
            return await self._write_holding_registers(self.REG_GRID_EXPORT_LIMIT, values)

        except Exception as e:
            _LOGGER.error(f"Error setting export limit: {e}")
            return False
        finally:
            await transaction.__aexit__(None, None, None)

    def set_configured_export_limit(self, limit_kw: Optional[float]) -> None:
        """Update the durable export cap and any active curtailment baseline.

        The mobile Controls endpoint can change the user's site cap while a
        zero-export curtailment is active.  In that case ``_original_pv_limit``
        already contains the old restore target, so it must move with the
        configured cap or the next restore will undo the user's change.
        """
        if limit_kw is None:
            self._configured_max_export_limit_kw = None
            if self._original_pv_limit is not None:
                self._original_pv_limit = None
            return

        normalized_kw = max(0.0, float(limit_kw))
        self._configured_max_export_limit_kw = normalized_kw
        if self._original_pv_limit is not None:
            self._original_pv_limit = int(normalized_kw * self.GAIN_POWER)

    async def apply_configured_export_limit(
        self,
        limit_kw: Optional[float],
        *,
        curtailment_active: bool,
    ) -> bool:
        """Apply a durable cap without lifting active zero-export protection.

        Runtime restore state is committed only when the hardware write
        succeeds.  This keeps the prior cap authoritative after a failed
        Modbus request and prevents the optimizer/config layers from advancing
        ahead of the inverter.
        """
        previous_configured = self._configured_max_export_limit_kw
        previous_original = self._original_pv_limit
        self.set_configured_export_limit(limit_kw)

        if curtailment_active:
            # Do not write the normal cap before zero export or let curtail()
            # recapture a stale live register after reload.  Establish the
            # requested restore target explicitly, then write only zero.
            self._original_pv_limit = (
                None
                if limit_kw is None
                else int(max(0.0, float(limit_kw)) * self.GAIN_POWER)
            )
            success = await self.set_export_limit(0.0)
        elif limit_kw is None:
            success = await self.restore_export_limit()
        else:
            success = await self.set_export_limit(float(limit_kw))

        if not success:
            self._configured_max_export_limit_kw = previous_configured
            self._original_pv_limit = previous_original
        return success

    async def restore_export_limit(self) -> bool:
        """Restore the export limit to the safety cap (or unlimited if no cap).

        Uses the export safety cap to prevent restoring to a value that
        exceeds DNSP limits or hardware ratings.

        Returns:
            True if successful
        """
        transaction = self._modbus_transaction()
        await transaction.__aenter__()
        try:
            if not await self.connect():
                return False

            safety_cap = await self._get_effective_export_safety_cap_kw()
            if safety_cap is not None:
                restore_value = int(safety_cap * self.GAIN_POWER)
                _LOGGER.info(f"Restoring Sigenergy export limit to safety cap: {safety_cap} kW")
            else:
                restore_value = self.EXPORT_LIMIT_UNLIMITED
                _LOGGER.info("Restoring Sigenergy export limit to unlimited (no safety cap available)")

            values = self._from_unsigned32(restore_value)
            success = await self._write_holding_registers(self.REG_GRID_EXPORT_LIMIT, values)

            if success:
                limit_str = f"{safety_cap} kW" if safety_cap is not None else "unlimited"
                _LOGGER.info(f"Successfully restored Sigenergy export limit to {limit_str}")
                self._original_pv_limit = None
            else:
                _LOGGER.error("Failed to restore Sigenergy export limit")

            return success

        except Exception as e:
            _LOGGER.error(f"Error restoring export limit: {e}")
            return False
        finally:
            await transaction.__aexit__(None, None, None)

    async def set_charge_rate_limit(self, limit_kw: float) -> bool:
        """Set the maximum battery charge rate.

        Args:
            limit_kw: Charge rate limit in kW (0 to disable charging)

        Returns:
            True if successful
        """
        transaction = self._modbus_transaction()
        await transaction.__aenter__()
        try:
            if not await self.connect():
                return False

            scaled_value = int(limit_kw * self.GAIN_POWER)
            if scaled_value < 0:
                scaled_value = 0
            if scaled_value > 0xFFFFFFFE:
                scaled_value = 0xFFFFFFFE

            _LOGGER.info(f"Setting Sigenergy charge rate limit to {limit_kw} kW")
            values = self._from_unsigned32(scaled_value)
            return await self._write_holding_registers(self.REG_ESS_MAX_CHARGE_LIMIT, values)

        except Exception as e:
            _LOGGER.error(f"Error setting charge rate limit: {e}")
            return False
        finally:
            await transaction.__aexit__(None, None, None)

    async def set_discharge_rate_limit(self, limit_kw: float) -> bool:
        """Set the maximum battery discharge rate.

        Args:
            limit_kw: Discharge rate limit in kW (0 to disable discharging)

        Returns:
            True if successful
        """
        try:
            if not await self.connect():
                return False

            scaled_value = int(limit_kw * self.GAIN_POWER)
            if scaled_value < 0:
                scaled_value = 0
            if scaled_value > 0xFFFFFFFE:
                scaled_value = 0xFFFFFFFE

            _LOGGER.info(f"Setting Sigenergy discharge rate limit to {limit_kw} kW")
            values = self._from_unsigned32(scaled_value)
            return await self._write_holding_registers(self.REG_ESS_MAX_DISCHARGE_LIMIT, values)

        except Exception as e:
            _LOGGER.error(f"Error setting discharge rate limit: {e}")
            return False

    async def apply_configured_charge_rate_limit(self, limit_kw: float) -> bool:
        """Apply and remember the user's normal-operation charge cap."""
        success = await self.set_charge_rate_limit(limit_kw)
        if success:
            self._configured_charge_rate_limit_kw = float(limit_kw)
        return success

    async def apply_configured_discharge_rate_limit(self, limit_kw: float) -> bool:
        """Apply and remember the user's normal-operation discharge cap."""
        success = await self.set_discharge_rate_limit(limit_kw)
        if success:
            self._configured_discharge_rate_limit_kw = float(limit_kw)
        return success

    async def set_self_consumption_mode(self) -> bool:
        """Set Remote EMS to maximum self-consumption mode.

        Enables Remote EMS and sets mode 2 (MAXIMUM_SELF_CONSUMPTION).
        The inverter handles PV -> house -> battery -> grid priority at
        hardware speed, equivalent to native self-consumption mode.

        Remote EMS stays permanently enabled — mode 2 allows instant
        transitions to charge/discharge modes without re-enabling.

        Returns:
            True if successful
        """
        transaction = self._modbus_transaction()
        await transaction.__aenter__()
        try:
            if not await self.connect():
                return False

            # Enable Remote EMS (idempotent if already enabled)
            ems_result = await self._write_holding_registers(self.REG_REMOTE_EMS_ENABLE, [1])
            if not ems_result:
                _LOGGER.error("Failed to enable Remote EMS for self-consumption")
                return False

            # Set mode to maximum self-consumption
            mode_result = await self._write_holding_registers(
                self.REG_REMOTE_EMS_CONTROL_MODE, [self.REMOTE_EMS_MODE_SELF_CONSUMPTION]
            )
            if not mode_result:
                _LOGGER.error("Failed to set Remote EMS mode to self-consumption")
                return False

            _LOGGER.info("Sigenergy Remote EMS set to MAXIMUM_SELF_CONSUMPTION (mode 2)")
            return True

        except Exception as e:
            _LOGGER.error(f"Error setting self-consumption mode: {e}")
            return False
        finally:
            await transaction.__aexit__(None, None, None)

    async def set_standby_mode(self) -> bool:
        """Set Remote EMS to STANDBY mode for IDLE hold.

        STANDBY stops all battery charge/discharge without touching backup_reserve.
        This prevents the Sigenergy firmware from grid-charging to reach backup SOC
        when the optimizer just wants to hold the current SOC.

        Returns:
            True if successful
        """
        transaction = self._modbus_transaction()
        await transaction.__aenter__()
        try:
            if not await self.connect():
                return False

            ems_result = await self._write_holding_registers(self.REG_REMOTE_EMS_ENABLE, [1])
            if not ems_result:
                _LOGGER.error("Failed to enable Remote EMS for standby")
                return False

            mode_result = await self._write_holding_registers(
                self.REG_REMOTE_EMS_CONTROL_MODE, [self.REMOTE_EMS_MODE_STANDBY]
            )
            if not mode_result:
                _LOGGER.error("Failed to set Remote EMS mode to standby")
                return False

            _LOGGER.info("Sigenergy Remote EMS set to STANDBY (mode 1) for IDLE hold")
            return True

        except Exception as e:
            _LOGGER.error(f"Error setting standby mode: {e}")
            return False
        finally:
            await transaction.__aexit__(None, None, None)

    async def restore_from_standby(self) -> bool:
        """Restore from STANDBY to self-consumption mode."""
        return await self.set_self_consumption_mode()

    async def disable_remote_ems(self) -> bool:
        """Disable Remote EMS so Sigenergy native/VPP control can resume."""
        transaction = self._modbus_transaction()
        await transaction.__aenter__()
        try:
            if not await self.connect():
                return False

            result = await self._write_holding_registers(self.REG_REMOTE_EMS_ENABLE, [0])
            if not result:
                _LOGGER.error("Failed to disable Sigenergy Remote EMS")
                return False

            _LOGGER.info("Sigenergy Remote EMS disabled; native/VPP control restored")
            return True

        except Exception as e:
            _LOGGER.error(f"Error disabling Sigenergy Remote EMS: {e}")
            return False
        finally:
            await transaction.__aexit__(None, None, None)

    async def force_charge(self, power_kw: float = 10.0) -> bool:
        """Force battery to charge from grid.

        Enables Remote EMS mode and sets control mode to charge.
        Applies the requested ESS max charge limit before entering charge mode
        so optimizer/manual setpoints do not charge at the inverter's rated
        capacity.

        Args:
            power_kw: Charge power in kW (used for logging, rate set by inverter)

        Returns:
            True if all commands successful
        """
        transaction = self._modbus_transaction()
        await transaction.__aenter__()
        try:
            if not await self.connect():
                return False

            try:
                target_kw = max(0.0, float(power_kw))
            except (TypeError, ValueError):
                target_kw = 10.0
            if (
                self._configured_charge_rate_limit_kw is not None
                and target_kw > self._configured_charge_rate_limit_kw
            ):
                _LOGGER.info(
                    "Force charge target %.2f kW exceeds configured charge cap "
                    "%.2f kW — clamping",
                    target_kw,
                    self._configured_charge_rate_limit_kw,
                )
                target_kw = self._configured_charge_rate_limit_kw

            scaled_value = int(target_kw * self.GAIN_POWER)
            scaled_value = min(max(scaled_value, 0), 0xFFFFFFFE)
            rate_result = await self._write_holding_registers(
                self.REG_ESS_MAX_CHARGE_LIMIT,
                self._from_unsigned32(scaled_value),
            )
            if not rate_result:
                _LOGGER.error(
                    "Failed to set ESS max charge limit to %.2f kW for force charge",
                    target_kw,
                )
                return False
            _LOGGER.info(
                "Sigenergy ESS max charge limit set to %.2f kW for force charge",
                target_kw,
            )

            # 1. Enable Remote EMS
            ems_result = await self._write_holding_registers(self.REG_REMOTE_EMS_ENABLE, [1])
            if not ems_result:
                _LOGGER.error("Failed to enable Remote EMS for force charge")
                return False
            _LOGGER.info("Remote EMS enabled for force charge")

            # 2. Set control mode to charge (PV first — charges from solar and supplements
            # with grid as needed). CHARGE_GRID (mode 3) suppresses solar generation entirely;
            # CHARGE_PV (mode 4) uses solar and fills the remainder from the grid.
            mode_result = await self._write_holding_registers(
                self.REG_REMOTE_EMS_CONTROL_MODE, [self.REMOTE_EMS_MODE_CHARGE_PV]
            )
            if not mode_result:
                _LOGGER.error("Failed to set Remote EMS control mode to charge")
                return False
            _LOGGER.info("Remote EMS control mode set to CHARGE_PV")

            _LOGGER.info(f"Sigenergy FORCE CHARGE active (target {target_kw} kW)")
            return True

        except Exception as e:
            _LOGGER.error(f"Error in Sigenergy force charge: {e}")
            return False
        finally:
            await transaction.__aexit__(None, None, None)

    async def force_discharge(self, power_kw: float = 10.0) -> bool:
        """Force battery to discharge to grid/load.

        Enables Remote EMS mode, sets control mode to discharge, and sets
        the active power target plus grid export limit to the requested export
        power. ESS max discharge is left unchanged so home load does not
        consume the grid-export allowance.

        Args:
            power_kw: Target grid export power in kW (default: 10.0)

        Returns:
            True if all commands successful
        """
        transaction = self._modbus_transaction()
        await transaction.__aenter__()
        try:
            if not await self.connect():
                return False

            try:
                target_kw = max(0.0, float(power_kw))
            except (TypeError, ValueError):
                target_kw = 10.0
            effective_kw = target_kw
            if self._configured_max_export_limit_kw is not None:
                if effective_kw > self._configured_max_export_limit_kw:
                    _LOGGER.info(
                        "Force discharge target %.2f kW exceeds configured DNSP "
                        "export limit %.2f kW — clamping",
                        effective_kw,
                        self._configured_max_export_limit_kw,
                    )
                    effective_kw = self._configured_max_export_limit_kw
            if (
                self._configured_discharge_rate_limit_kw is not None
                and effective_kw > self._configured_discharge_rate_limit_kw
            ):
                _LOGGER.info(
                    "Force discharge target %.2f kW exceeds configured discharge "
                    "cap %.2f kW — clamping",
                    effective_kw,
                    self._configured_discharge_rate_limit_kw,
                )
                effective_kw = self._configured_discharge_rate_limit_kw

            # The two Sigenergy discharge modes behave differently across sites:
            # PV-first preserves solar but may not pull from the battery; ESS-first
            # pulls from storage but can suppress PV. Pick the least invasive mode
            # that can plausibly satisfy the requested export target.
            mode = self.REMOTE_EMS_MODE_DISCHARGE_PV
            mode_name = "DISCHARGE_PV"
            attrs: dict = {}
            try:
                state = await self.get_status()
                attrs = getattr(state, "attributes", {}) or {}
                solar_known = (
                    "pv_power_kw" in attrs
                    or "third_party_pv_power_kw" in attrs
                )
                if solar_known:
                    solar_kw = max(0.0, float(attrs.get("pv_power_kw", 0) or 0))
                    solar_kw += max(
                        0.0,
                        float(attrs.get("third_party_pv_power_kw", 0) or 0),
                    )
                    if solar_kw < effective_kw * 0.8:
                        mode = self.REMOTE_EMS_MODE_DISCHARGE_ESS
                        mode_name = "DISCHARGE_ESS"
                    _LOGGER.info(
                        "Sigenergy force discharge mode selected: %s "
                        "(solar %.2f kW, target %.2f kW)",
                        mode_name,
                        solar_kw,
                        effective_kw,
                    )
            except Exception as e:
                _LOGGER.debug(
                    "Sigenergy force discharge mode selection using default "
                    "PV-first mode: %s",
                    e,
                )

            # 1. Enable Remote EMS
            ems_result = await self._write_holding_registers(self.REG_REMOTE_EMS_ENABLE, [1])
            if not ems_result:
                _LOGGER.error("Failed to enable Remote EMS for force discharge")
                return False
            _LOGGER.info("Remote EMS enabled for force discharge")

            # 2. Set control mode to discharge.
            mode_result = await self._write_holding_registers(
                self.REG_REMOTE_EMS_CONTROL_MODE, [mode]
            )
            if not mode_result:
                _LOGGER.error("Failed to set Remote EMS control mode to discharge")
                return False
            _LOGGER.info("Remote EMS control mode set to %s", mode_name)

            scaled_value = int(effective_kw * self.GAIN_POWER)

            # 3. Set active power target. The export limit below is only a
            # ceiling; this signed target is what tells Sigenergy to actually
            # push power out instead of just covering local load.
            target_values = self._from_signed32(-scaled_value)
            target_result = await self._write_holding_registers(
                self.REG_ACTIVE_POWER_FIXED_TARGET,
                target_values,
            )
            if not target_result:
                _LOGGER.warning(
                    "Failed to set Sigenergy active power target to %.2f kW export; "
                    "falling back to export limit only",
                    effective_kw,
                )
            else:
                _LOGGER.info("Sigenergy active power target set to %.2f kW export", effective_kw)

            # 4. Set grid export limit. The dynamic safety cap is bypassed because
            # path 5 of _get_effective_export_safety_cap_kw reads back
            # REG_GRID_EXPORT_LIMIT itself — a curtailment-set low value would
            # then clamp force_discharge to that low value. The user-configured
            # DNSP limit is still honored (it's path 1 of the cap chain and not
            # circular).
            values = self._from_unsigned32(scaled_value)
            rate_result = await self._write_holding_registers(self.REG_GRID_EXPORT_LIMIT, values)
            if not rate_result:
                _LOGGER.error(f"Failed to set grid export limit to {effective_kw} kW")
                return False
            _LOGGER.info(f"Sigenergy grid export limit set to {effective_kw} kW")

            _LOGGER.info(f"Sigenergy FORCE DISCHARGE active — target {effective_kw} kW, export limit {effective_kw} kW")
            return True

        except Exception as e:
            _LOGGER.error(f"Error in Sigenergy force discharge: {e}")
            return False
        finally:
            await transaction.__aexit__(None, None, None)

    async def _restore_ess_max_limits_to_rated(
        self,
        *,
        use_configured_caps: bool = True,
    ) -> bool:
        """Restore ESS max limits to configured caps or rated values.

        Force/preserve activity may have set temporary lower limits. Normal
        PowerSync operation returns to the user's durable caps when present;
        handing control back to the inverter deliberately clears them to rated.
        """
        async def resolve_target(
            configured_kw: float | None,
            rated_register: int,
            label: str,
        ) -> tuple[int | None, str]:
            if use_configured_caps and configured_kw is not None:
                return (
                    int(max(0.0, float(configured_kw)) * self.GAIN_POWER),
                    "configured",
                )
            try:
                rated_regs = await self._read_input_registers(rated_register, 2)
            except Exception as err:
                _LOGGER.warning("Failed to read rated ESS %s limit: %s", label, err)
                return None, "rated"
            if not rated_regs or len(rated_regs) < 2:
                _LOGGER.warning("Rated ESS %s limit is unavailable", label)
                return None, "rated"
            rated_value = self._to_unsigned32(rated_regs[0], rated_regs[1])
            if not 0 < rated_value < self.EXPORT_LIMIT_UNLIMITED:
                _LOGGER.warning(
                    "Rated ESS %s limit is invalid: %s",
                    label,
                    rated_value,
                )
                return None, "rated"
            return rated_value, "rated"

        async def write_target(
            register: int,
            target: int | None,
            source: str,
            label: str,
        ) -> bool:
            if target is None:
                return False
            try:
                success = await self._write_holding_registers(
                    register,
                    self._from_unsigned32(target),
                )
            except Exception as err:
                _LOGGER.warning("Failed to restore ESS %s limit: %s", label, err)
                return False
            if not success:
                _LOGGER.warning("Failed to write ESS %s limit", label)
                return False
            _LOGGER.debug(
                "Restored ESS max %s limit to %s: %s kW",
                label,
                source,
                target / self.GAIN_POWER,
            )
            return True

        charge_target, charge_source = await resolve_target(
            self._configured_charge_rate_limit_kw,
            self.REG_ESS_RATED_CHARGE_POWER,
            "charge",
        )
        discharge_target, discharge_source = await resolve_target(
            self._configured_discharge_rate_limit_kw,
            self.REG_ESS_RATED_DISCHARGE_POWER,
            "discharge",
        )
        charge_success = await write_target(
            self.REG_ESS_MAX_CHARGE_LIMIT,
            charge_target,
            charge_source,
            "charge",
        )
        discharge_success = await write_target(
            self.REG_ESS_MAX_DISCHARGE_LIMIT,
            discharge_target,
            discharge_source,
            "discharge",
        )
        return charge_success and discharge_success

    async def restore_normal(self, native_control: bool = False) -> bool:
        """Restore normal self-consumption operation.

        By default, sets Remote EMS to maximum self-consumption mode (mode 2),
        restores grid export limit to the safety cap, and restores ESS max
        limits to configured user caps (or rated values when no caps exist).
        Remote EMS stays enabled so PowerSync can transition to
        charge/discharge modes quickly.

        When ``native_control`` is true, clears PowerSync limits and disables
        Remote EMS so Sigenergy native/VPP control can resume.

        Returns:
            True if successful
        """
        transaction = self._modbus_transaction()
        await transaction.__aenter__()
        try:
            if not await self.connect():
                return False

            if not native_control:
                # 1. Set Remote EMS to self-consumption (leave enabled)
                result = await self.set_self_consumption_mode()
                if not result:
                    _LOGGER.error("Failed to set self-consumption mode during restore")
                    return False

            # 2. Restore grid export limit to safety cap
            # force_discharge sets this to a specific value — need to clear it
            export_result = await self.restore_export_limit()
            if not export_result:
                _LOGGER.warning("Failed to restore grid export limit")

            # 3. Restore durable user caps for PowerSync control. Native/VPP
            # handoff clears PowerSync's caps back to the hardware ratings.
            ess_limits_result = await self._restore_ess_max_limits_to_rated(
                use_configured_caps=not native_control,
            )
            if not ess_limits_result:
                _LOGGER.warning("Failed to fully restore ESS max limits")

            # 4. Restore backup reserve if a target is set
            # Sigenergy firmware may reset backup SOC when Remote EMS mode
            # is toggled. Write it back to ensure the user's setting persists.
            reserve_result = True
            if self._restore_backup_reserve_pct is not None:
                reserve_result = await self.set_backup_reserve(
                    self._restore_backup_reserve_pct
                )
                if reserve_result:
                    _LOGGER.info(
                        "Sigenergy backup reserve restored to %d%%",
                        self._restore_backup_reserve_pct,
                    )

            # 5. Hand control back only after every PowerSync-owned limit and
            # reserve has been restored. Disabling Remote EMS earlier could
            # strand a temporary cap if any later cleanup step fails.
            native_result = True
            if native_control:
                if export_result and ess_limits_result and reserve_result:
                    native_result = await self.disable_remote_ems()
                else:
                    native_result = False
                    _LOGGER.warning(
                        "Keeping Remote EMS enabled because native cleanup "
                        "did not restore all limits"
                    )

            restore_succeeded = bool(
                export_result
                and ess_limits_result
                and native_result
                and reserve_result
            )
            if not restore_succeeded:
                _LOGGER.warning(
                    "Sigenergy restore incomplete: export=%s ess_limits=%s "
                    "native=%s reserve=%s",
                    export_result,
                    ess_limits_result,
                    native_result,
                    reserve_result,
                )
                return False

            if native_control:
                _LOGGER.info("Sigenergy restored to native/VPP control")
                return True

            _LOGGER.info("Sigenergy restored to self-consumption (Remote EMS mode 2, export limit restored)")
            return True

        except Exception as e:
            _LOGGER.error(f"Error restoring Sigenergy normal operation: {e}")
            return False
        finally:
            await transaction.__aexit__(None, None, None)

    async def get_backup_reserve(self) -> Optional[int]:
        """Get the current backup reserve (backup SOC) percentage.

        Returns:
            Backup reserve percentage (0-100) or None on error
        """
        try:
            if not await self.connect():
                return None

            regs = await self._read_holding_registers(self.REG_ESS_BACKUP_SOC, 1)
            if regs and len(regs) >= 1:
                # U16 with gain 10
                reserve_pct = regs[0] / self.GAIN_SOC
                _LOGGER.debug(f"Sigenergy backup reserve: {reserve_pct}%")
                return int(reserve_pct)
            return None

        except Exception as e:
            _LOGGER.error(f"Error getting backup reserve: {e}")
            return None

    async def set_backup_reserve(self, percent: int) -> bool:
        """Set the backup reserve (backup SOC) percentage.

        This is the minimum battery level that will be preserved.
        The battery won't discharge below this level except in backup/outage mode.

        Args:
            percent: Backup reserve percentage (0-100)

        Returns:
            True if successful
        """
        try:
            if not await self.connect():
                return False

            if percent < 0:
                percent = 0
            if percent > 100:
                percent = 100

            # U16 with gain 10
            scaled_value = int(percent * self.GAIN_SOC)

            _LOGGER.info(f"Setting Sigenergy backup reserve to {percent}%")
            success = await self._write_holding_registers(self.REG_ESS_BACKUP_SOC, [scaled_value])

            if success:
                _LOGGER.info(f"✅ Successfully set Sigenergy backup reserve to {percent}%")
            else:
                _LOGGER.error(f"Failed to set Sigenergy backup reserve")

            return success

        except Exception as e:
            _LOGGER.error(f"Error setting backup reserve: {e}")
            return False

    async def get_discharge_cutoff_soc(self) -> Optional[int]:
        """Get the discharge cut-off SOC percentage.

        Returns:
            Discharge cut-off SOC percentage (0-100) or None on error
        """
        try:
            if not await self.connect():
                return None

            regs = await self._read_holding_registers(self.REG_ESS_DISCHARGE_CUTOFF_SOC, 1)
            if regs and len(regs) >= 1:
                cutoff_pct = regs[0] / self.GAIN_SOC
                _LOGGER.debug(f"Sigenergy discharge cut-off SOC: {cutoff_pct}%")
                return int(cutoff_pct)
            return None

        except Exception as e:
            _LOGGER.error(f"Error getting discharge cut-off SOC: {e}")
            return None

    async def set_discharge_cutoff_soc(self, percent: int) -> bool:
        """Set the discharge cut-off SOC percentage.

        The battery will stop discharging when it reaches this level.

        Args:
            percent: Discharge cut-off SOC percentage (0-100)

        Returns:
            True if successful
        """
        try:
            if not await self.connect():
                return False

            if percent < 0:
                percent = 0
            if percent > 100:
                percent = 100

            scaled_value = int(percent * self.GAIN_SOC)

            _LOGGER.info(f"Setting Sigenergy discharge cut-off SOC to {percent}%")
            success = await self._write_holding_registers(self.REG_ESS_DISCHARGE_CUTOFF_SOC, [scaled_value])

            if success:
                _LOGGER.info(f"✅ Successfully set Sigenergy discharge cut-off SOC to {percent}%")
            else:
                _LOGGER.error(f"Failed to set Sigenergy discharge cut-off SOC")

            return success

        except Exception as e:
            _LOGGER.error(f"Error setting discharge cut-off SOC: {e}")
            return False

    async def __aenter__(self):
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.disconnect()
