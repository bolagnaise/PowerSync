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
import pymodbus

from .base import InverterController, InverterState, InverterStatus

_LOGGER = logging.getLogger(__name__)

# pymodbus 3.9+ changed 'slave' parameter to 'device_id'
try:
    _pymodbus_version = tuple(int(x) for x in pymodbus.__version__.split(".")[:2])
    _SLAVE_PARAM = "device_id" if _pymodbus_version >= (3, 9) else "slave"
except Exception:
    _SLAVE_PARAM = "slave"  # Fallback to older parameter name


class SungrowSHController(InverterController):
    """Controller for Sungrow SH series hybrid inverters via Modbus TCP.

    Uses Modbus TCP to communicate with the inverter through
    the internal LAN port or WiNet-S WiFi/Ethernet dongle.

    Supports load following curtailment via export power limiting,
    which allows self-consumption while preventing grid export.
    """

    # Modbus register addresses (0-indexed for pymodbus)
    # Documentation register - 1 = pymodbus address

    # Export power limiting (load following) - PREFERRED METHOD
    REG_EXPORT_LIMIT = 13072           # 13073 - Export power limit (W)
    REG_EXPORT_LIMIT_MODE = 13085      # 13086 - Export limit mode (0xAA=enable, 0x55=disable)
    EXPORT_LIMIT_ENABLE = 0xAA         # 170 - Enable export limiting
    EXPORT_LIMIT_DISABLE = 0x55        # 85 - Disable export limiting

    # System state control (fallback - full shutdown)
    REGISTER_SYSTEM_STATE = 12999      # 13000 - System state control
    STATE_STOP = 0xCE   # 206 - Stop inverter
    STATE_START = 0xCF  # 207 - Start inverter

    # Running state register and values
    REGISTER_RUNNING_STATE = 12999     # 13000 - Running state
    RUNNING_STATE_STOP = 0x8000
    RUNNING_STATE_STANDBY = 0x1400
    RUNNING_STATE_RUNNING = 0x0002
    RUNNING_STATE_FAULT = 0x1300

    # ===== SH Series Register Addresses (0-indexed) =====
    # PV Generation
    REG_DAILY_PV = 13000               # 13001 - Daily PV generation (kWh * 0.1)
    REG_TOTAL_PV = 13001               # 13002-13003 - Total PV generation (kWh * 0.1, U32)

    # Power readings
    REG_LOAD_POWER = 13006             # 13007-13008 - Load power (W, I32)
    REG_EXPORT_POWER = 13008           # 13009-13010 - Export power (W, I32)
    REG_TOTAL_ACTIVE_POWER = 13032     # 13033-13034 - Total active power (W, I32)

    # Battery
    REG_BATTERY_VOLTAGE = 13018        # 13019 - Battery voltage (V * 0.1)
    REG_BATTERY_CURRENT = 13019        # 13020 - Battery current (A * 0.1, signed)
    REG_BATTERY_POWER = 13020          # 13021 - Battery power (W, signed)
    REG_BATTERY_LEVEL = 13021          # 13022 - Battery level (% * 0.1)
    REG_BATTERY_SOH = 13022            # 13023 - Battery state of health (% * 0.1)
    REG_BATTERY_TEMP = 13023           # 13024 - Battery temperature (°C * 0.1, signed)
    REG_DAILY_BATTERY_DISCHARGE = 13024  # 13025 - Daily battery discharge (kWh * 0.1)
    REG_DAILY_BATTERY_CHARGE = 13038   # 13039 - Daily battery charge (kWh * 0.1)

    # Energy accounting
    REG_DAILY_IMPORT = 13034           # 13035 - Daily imported energy (kWh * 0.1)
    REG_TOTAL_IMPORT = 13035           # 13036-13037 - Total imported energy (kWh * 0.1, U32)
    REG_DAILY_EXPORT = 13043           # 13044 - Daily exported energy (kWh * 0.1)
    REG_TOTAL_EXPORT = 13044           # 13045-13046 - Total exported energy (kWh * 0.1, U32)

    # Temperature
    REG_INVERTER_TEMP = 5006           # 5007 - Inverter temperature (°C * 0.1, signed)

    # Grid
    REG_GRID_FREQUENCY = 5035          # 5036 - Grid frequency (Hz * 0.1)
    REG_PHASE_A_VOLTAGE = 5018         # 5019 - Phase A voltage (V * 0.1)

    # ===== Battery Control Registers (for SH-series as battery system) =====
    # EMS Mode Control
    REG_EMS_MODE = 13049               # 13050 - EMS mode (0=Self-consumption, 2=Forced, 3=External EMS)
    REG_CHARGE_CMD = 13050             # 13051 - Charge/discharge command
    CMD_CHARGE = 0xAA                  # 170 - Force charge
    CMD_DISCHARGE = 0xBB               # 187 - Force discharge
    CMD_STOP = 0xCC                    # 204 - Stop forced mode
    EMS_AI = 0                         # AI mode (iHM docs)
    EMS_SELF_CONSUMPTION = 0           # Self-consumption (same as AI on non-iHM)
    EMS_FORCED = 2
    EMS_EXTERNAL = 3
    EMS_VPP = 4                        # Virtual power plant mode

    # SOC Limits
    REG_MAX_SOC = 13057                # 13058 - Maximum SOC limit (% * 10)
    REG_MIN_SOC = 13058                # 13059 - Minimum SOC (backup reserve) (% * 10)

    # Current Limits
    REG_MAX_DISCHARGE_CURRENT = 13065  # 13066 - Max discharge current (A * 1000)
    REG_MAX_CHARGE_CURRENT = 13066     # 13067 - Max charge current (A * 1000)

    # Export Control
    REG_EXPORT_LIMIT_SETTING = 13073   # 13074 - Export power limit setting (W)
    REG_EXPORT_LIMIT_ENABLED = 13086   # 13087 - Export limit enabled (0=disabled, 1=enabled)

    # Backup Reserve
    REG_BACKUP_RESERVE = 13099         # 13100 - Reserved SOC for backup (% * 10)

    # Fallback battery voltage for kW to Amp conversion (used until real voltage is read)
    BATTERY_VOLTAGE_FALLBACK = 48      # Typical LFP battery pack voltage

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
        self._battery_voltage: float = self.BATTERY_VOLTAGE_FALLBACK
        self._original_ems_mode: Optional[int] = None

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
                **{_SLAVE_PARAM: self.slave_id},
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

    async def _read_all_registers(self) -> dict:
        """Read all SH series registers and return as attributes dict."""
        attrs = {}

        try:
            # Read battery registers (13019-13025) in one batch
            battery_regs = await self._read_register(self.REG_BATTERY_VOLTAGE, 7)
            if battery_regs and len(battery_regs) >= 7:
                voltage = round(battery_regs[0] * 0.1, 1)
                attrs["battery_voltage"] = voltage
                if voltage > 0:
                    self._battery_voltage = voltage
                attrs["battery_current"] = round(self._to_signed16(battery_regs[1]) * 0.1, 1)
                attrs["battery_power"] = self._to_signed16(battery_regs[2])
                attrs["battery_level"] = round(battery_regs[3] * 0.1, 1)
                attrs["battery_soh"] = round(battery_regs[4] * 0.1, 1)
                attrs["battery_temperature"] = round(self._to_signed16(battery_regs[5]) * 0.1, 1)
                attrs["daily_battery_discharge"] = round(battery_regs[6] * 0.1, 2)

            # Read daily PV generation
            daily_pv = await self._read_register(self.REG_DAILY_PV, 1)
            if daily_pv:
                attrs["daily_pv_generation"] = round(daily_pv[0] * 0.1, 2)

            # Read total PV generation (32-bit)
            total_pv = await self._read_register(self.REG_TOTAL_PV, 2)
            if total_pv and len(total_pv) >= 2:
                attrs["total_pv_generation"] = round(self._to_unsigned32(total_pv[0], total_pv[1]) * 0.1, 1)

            # Read load power (32-bit signed)
            load_power = await self._read_register(self.REG_LOAD_POWER, 2)
            if load_power and len(load_power) >= 2:
                attrs["load_power"] = self._to_signed32(load_power[0], load_power[1])

            # Read export power (32-bit signed)
            export_power = await self._read_register(self.REG_EXPORT_POWER, 2)
            if export_power and len(export_power) >= 2:
                attrs["export_power"] = self._to_signed32(export_power[0], export_power[1])

            # Read total active power (32-bit signed)
            active_power = await self._read_register(self.REG_TOTAL_ACTIVE_POWER, 2)
            if active_power and len(active_power) >= 2:
                attrs["active_power"] = self._to_signed32(active_power[0], active_power[1])

            # Read daily import/export
            daily_import = await self._read_register(self.REG_DAILY_IMPORT, 1)
            if daily_import:
                attrs["daily_import"] = round(daily_import[0] * 0.1, 2)

            # Read total (lifetime) import energy (32-bit unsigned)
            total_import = await self._read_register(self.REG_TOTAL_IMPORT, 2)
            if total_import and len(total_import) >= 2:
                attrs["total_import"] = round(self._to_unsigned32(total_import[0], total_import[1]) * 0.1, 1)

            daily_export = await self._read_register(self.REG_DAILY_EXPORT, 1)
            if daily_export:
                attrs["daily_export"] = round(daily_export[0] * 0.1, 2)

            # Read total (lifetime) export energy (32-bit unsigned)
            total_export = await self._read_register(self.REG_TOTAL_EXPORT, 2)
            if total_export and len(total_export) >= 2:
                attrs["total_export"] = round(self._to_unsigned32(total_export[0], total_export[1]) * 0.1, 1)

            # Read daily battery charge
            daily_charge = await self._read_register(self.REG_DAILY_BATTERY_CHARGE, 1)
            if daily_charge:
                attrs["daily_battery_charge"] = round(daily_charge[0] * 0.1, 2)

            # Read inverter temperature (from 5xxx range)
            inv_temp = await self._read_register(self.REG_INVERTER_TEMP, 1)
            if inv_temp:
                attrs["inverter_temperature"] = round(self._to_signed16(inv_temp[0]) * 0.1, 1)

            # Read grid frequency
            grid_freq = await self._read_register(self.REG_GRID_FREQUENCY, 1)
            if grid_freq:
                attrs["grid_frequency"] = round(grid_freq[0] * 0.1, 2)

            # Read phase A voltage
            voltage = await self._read_register(self.REG_PHASE_A_VOLTAGE, 1)
            if voltage:
                attrs["grid_voltage"] = round(voltage[0] * 0.1, 1)

            # Read export limit status
            export_limit = await self._read_register(self.REG_EXPORT_LIMIT, 1)
            if export_limit:
                attrs["export_limit_w"] = export_limit[0]

            export_mode = await self._read_register(self.REG_EXPORT_LIMIT_MODE, 1)
            if export_mode:
                attrs["export_limit_enabled"] = export_mode[0] == self.EXPORT_LIMIT_ENABLE

        except Exception as e:
            _LOGGER.warning(f"Error reading some registers: {e}")

        return attrs

    async def curtail(
        self,
        home_load_w: Optional[float] = None,
        rated_capacity_w: Optional[float] = None,
    ) -> bool:
        """Enable load following curtailment on the Sungrow SH inverter.

        If home_load_w is provided, limits export to match home load.
        Otherwise sets export limit to 0W (zero export).

        Falls back to full shutdown if export limiting fails.

        Returns:
            True if curtailment successful
        """
        export_limit_w = int(home_load_w) if home_load_w is not None and home_load_w > 0 else 0
        mode_str = f"load-following: {export_limit_w}W" if export_limit_w > 0 else "zero export"
        _LOGGER.info(f"Curtailing Sungrow SH inverter at {self.host} ({mode_str})")

        try:
            if not await self.connect():
                _LOGGER.error("Cannot curtail: failed to connect to inverter")
                return False

            # Step 1: Set export limit
            success = await self._write_register(self.REG_EXPORT_LIMIT, export_limit_w)
            if not success:
                _LOGGER.warning("Failed to set export limit, trying full shutdown")
                # Fallback to full shutdown
                success = await self._write_register(self.REGISTER_SYSTEM_STATE, self.STATE_STOP)
                if success:
                    _LOGGER.info(f"Curtailed via full shutdown at {self.host}")
                return success

            # Step 2: Enable export limiting mode
            success = await self._write_register(self.REG_EXPORT_LIMIT_MODE, self.EXPORT_LIMIT_ENABLE)
            if not success:
                _LOGGER.warning("Failed to enable export limit mode, trying full shutdown")
                success = await self._write_register(self.REGISTER_SYSTEM_STATE, self.STATE_STOP)
                if success:
                    _LOGGER.info(f"Curtailed via full shutdown at {self.host}")
                return success

            _LOGGER.info(f"Successfully curtailed Sungrow SH inverter at {self.host} ({mode_str})")
            await asyncio.sleep(1)
            return True

        except Exception as e:
            _LOGGER.error(f"Error curtailing Sungrow SH inverter: {e}")
            return False

    async def restore(self) -> bool:
        """Restore normal operation of the Sungrow SH inverter.

        Disables export power limiting to return to normal export behavior.

        Returns:
            True if restore successful
        """
        _LOGGER.info(f"Restoring Sungrow SH inverter at {self.host} to normal operation")

        try:
            if not await self.connect():
                _LOGGER.error("Cannot restore: failed to connect to inverter")
                return False

            # Disable export limiting mode
            success = await self._write_register(self.REG_EXPORT_LIMIT_MODE, self.EXPORT_LIMIT_DISABLE)
            if not success:
                _LOGGER.warning("Failed to disable export limit, trying start command")
                # Fallback: ensure inverter is running
                success = await self._write_register(self.REGISTER_SYSTEM_STATE, self.STATE_START)
                if success:
                    _LOGGER.info(f"Restored via start command at {self.host}")
                return success

            _LOGGER.info(f"Successfully restored Sungrow SH inverter at {self.host}")
            await asyncio.sleep(1)
            return True

        except Exception as e:
            _LOGGER.error(f"Error restoring Sungrow SH inverter: {e}")
            return False

    async def get_status(self) -> InverterState:
        """Get current status of the Sungrow SH inverter.

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

            # If we couldn't read ANY registers, the inverter is likely sleeping/offline
            if not attrs or len(attrs) == 0:
                _LOGGER.debug("Sungrow SH: No register data - inverter likely sleeping")
                return InverterState(
                    status=InverterStatus.OFFLINE,
                    is_curtailed=False,
                    error_message="No register data (inverter sleeping)",
                    attributes={"host": self.host, "model": self.model or "SH Series"},
                )

            # Determine status based on running state
            running_state = await self._read_register(self.REGISTER_RUNNING_STATE, 1)
            status = InverterStatus.ONLINE
            is_curtailed = False

            if running_state:
                state_value = running_state[0]
                if state_value == self.RUNNING_STATE_STOP:
                    status = InverterStatus.CURTAILED
                    is_curtailed = True
                    attrs["running_state"] = "stopped"
                elif state_value == self.RUNNING_STATE_FAULT:
                    status = InverterStatus.ERROR
                    attrs["running_state"] = "fault"
                elif state_value == self.RUNNING_STATE_STANDBY:
                    status = InverterStatus.ONLINE
                    attrs["running_state"] = "standby"
                else:
                    attrs["running_state"] = "running"

            # Check if export limiting is active (load following mode)
            if attrs.get("export_limit_enabled") and attrs.get("export_limit_w", 10000) == 0:
                is_curtailed = True
                attrs["running_state"] = "load_following"
                if status == InverterStatus.ONLINE:
                    status = InverterStatus.CURTAILED

            # Add model info
            attrs["model"] = self.model or "SH Series"
            attrs["host"] = self.host

            # Get power output from active_power if available
            power_output = attrs.get("active_power")

            self._last_state = InverterState(
                status=status,
                is_curtailed=is_curtailed,
                power_output_w=power_output,
                attributes=attrs,
            )

            return self._last_state

        except Exception as e:
            _LOGGER.error(f"Error getting Sungrow SH inverter status: {e}")
            return InverterState(
                status=InverterStatus.ERROR,
                is_curtailed=False,
                error_message=str(e),
            )

    # ===== Battery Control Methods (for use as battery system) =====

    async def _write_forced_mode(self, cmd: int, label: str) -> bool:
        """Set EMS to forced mode with a charge/discharge command and verify.

        Writes REG_EMS_MODE and REG_CHARGE_CMD, then reads back both registers
        to confirm the inverter accepted them. Retries once on verification
        failure (covers silent Modbus collisions).
        """
        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            try:
                if not await self.connect():
                    return False

                # Save current EMS mode for restore (only on first attempt)
                if attempt == 1 and self._original_ems_mode is None:
                    ems_raw = await self._read_register(self.REG_EMS_MODE, 1)
                    if ems_raw:
                        self._original_ems_mode = ems_raw[0]

                success = await self._write_register(self.REG_EMS_MODE, self.EMS_FORCED)
                if not success:
                    _LOGGER.warning("Sungrow %s: EMS mode write failed (attempt %d/%d)", label, attempt, max_attempts)
                    if attempt < max_attempts:
                        await asyncio.sleep(1)
                        continue
                    return False

                success = await self._write_register(self.REG_CHARGE_CMD, cmd)
                if not success:
                    _LOGGER.warning("Sungrow %s: charge cmd write failed (attempt %d/%d)", label, attempt, max_attempts)
                    if attempt < max_attempts:
                        await asyncio.sleep(1)
                        continue
                    return False

                # Verify: read back EMS mode to confirm inverter accepted it
                await asyncio.sleep(0.5)
                ems_check = await self._read_register(self.REG_EMS_MODE, 1)
                if ems_check and ems_check[0] == self.EMS_FORCED:
                    _LOGGER.info(
                        "Sungrow SH at %s now in %s mode%s",
                        self.host, label,
                        "" if attempt == 1 else f" (attempt {attempt})",
                    )
                    return True
                else:
                    _LOGGER.warning(
                        "Sungrow %s verify failed: EMS mode=%s (attempt %d/%d)",
                        label, ems_check, attempt, max_attempts,
                    )
                    if attempt < max_attempts:
                        await asyncio.sleep(1)
                        continue
                    return False

            except Exception as e:
                _LOGGER.error("Error setting %s: %s", label, e)
                if attempt < max_attempts:
                    await asyncio.sleep(1)
                    continue
                return False

        return False

    async def force_charge(self) -> bool:
        """Set EMS to forced charge mode."""
        _LOGGER.info("Setting Sungrow SH at %s to forced charge mode", self.host)
        return await self._write_forced_mode(self.CMD_CHARGE, "force charge")

    async def force_discharge(self) -> bool:
        """Set EMS to forced discharge mode."""
        _LOGGER.info("Setting Sungrow SH at %s to forced discharge mode", self.host)
        return await self._write_forced_mode(self.CMD_DISCHARGE, "force discharge")

    async def restore_normal(self) -> bool:
        """Restore to the EMS mode that was active before force charge/discharge.

        Returns:
            True if successful, False otherwise
        """
        target_mode = self._original_ems_mode if self._original_ems_mode is not None else self.EMS_SELF_CONSUMPTION
        mode_name = {0: "self-consumption", 2: "forced", 3: "external EMS", 4: "VPP"}.get(target_mode, f"mode {target_mode}")
        _LOGGER.info("Restoring Sungrow SH at %s to %s", self.host, mode_name)
        try:
            if not await self.connect():
                return False

            # Stop forced mode
            success = await self._write_register(self.REG_CHARGE_CMD, self.CMD_STOP)
            if not success:
                _LOGGER.warning("Failed to send stop command")

            # Restore original EMS mode
            success = await self._write_register(self.REG_EMS_MODE, target_mode)
            if not success:
                _LOGGER.error("Failed to set EMS to %s", mode_name)
                return False

            self._original_ems_mode = None
            _LOGGER.info("Sungrow SH at %s restored to %s", self.host, mode_name)
            return True

        except Exception as e:
            _LOGGER.error("Error restoring normal mode: %s", e)
            return False

    async def set_idle_mode(self) -> bool:
        """Set Sungrow to Forced + Stop for IDLE (prevents self-consumption discharge).

        In self-consumption mode, backup_reserve is only a passive floor — the
        battery still discharges to serve home load until it reaches the reserve.
        Using EMS_FORCED + CMD_STOP halts all battery activity so the grid
        serves the home load instead. This is the Sungrow equivalent of
        FoxESS Backup mode or Tesla autonomous mode for holding SOC.

        Returns:
            True if successful, False otherwise
        """
        _LOGGER.info(f"Setting Sungrow SH at {self.host} to Forced+Stop (IDLE hold)")
        try:
            if not await self.connect():
                return False

            # Set EMS to forced mode
            success = await self._write_register(self.REG_EMS_MODE, self.EMS_FORCED)
            if not success:
                _LOGGER.error("Failed to set EMS to forced mode for IDLE")
                return False

            # Send stop command — halts all charge/discharge
            success = await self._write_register(self.REG_CHARGE_CMD, self.CMD_STOP)
            if not success:
                _LOGGER.error("Failed to send stop command for IDLE")
                return False

            _LOGGER.info(f"Sungrow SH at {self.host} now in Forced+Stop (IDLE hold)")
            return True

        except Exception as e:
            _LOGGER.error(f"Error setting IDLE mode: {e}")
            return False

    async def restore_from_idle(self) -> bool:
        """Restore self-consumption mode after IDLE.

        Returns:
            True if successful, False otherwise
        """
        return await self.restore_normal()

    async def set_backup_reserve(self, percent: int) -> bool:
        """Set backup reserve percentage.

        Args:
            percent: Backup reserve SOC percentage (0-100)

        Returns:
            True if successful, False otherwise
        """
        _LOGGER.info(f"Setting Sungrow SH at {self.host} backup reserve to {percent}%")
        try:
            if not await self.connect():
                return False

            # Register value is percentage * 10 (0.1% scale)
            value = int(percent * 10)
            success = await self._write_register(self.REG_BACKUP_RESERVE, value)
            if not success:
                _LOGGER.error("Failed to set backup reserve")
                return False

            _LOGGER.info(f"Sungrow SH backup reserve set to {percent}%")
            return True

        except Exception as e:
            _LOGGER.error(f"Error setting backup reserve: {e}")
            return False

    async def set_min_soc(self, percent: int) -> bool:
        """Set minimum SOC (backup reserve via MIN_SOC register).

        Args:
            percent: Minimum SOC percentage (0-100)

        Returns:
            True if successful, False otherwise
        """
        _LOGGER.info(f"Setting Sungrow SH at {self.host} min SOC to {percent}%")
        try:
            if not await self.connect():
                return False

            # Register value is percentage * 10 (0.1% scale)
            value = int(percent * 10)
            success = await self._write_register(self.REG_MIN_SOC, value)
            if not success:
                _LOGGER.error("Failed to set min SOC")
                return False

            _LOGGER.info(f"Sungrow SH min SOC set to {percent}%")
            return True

        except Exception as e:
            _LOGGER.error(f"Error setting min SOC: {e}")
            return False

    async def set_charge_rate_limit(self, kw: float) -> bool:
        """Set maximum charge rate in kW.

        Converts kW to Amps using the actual battery voltage (read from
        register 13019), falling back to 48V if not yet available.
        """
        voltage = self._battery_voltage
        _LOGGER.info("Setting Sungrow SH at %s charge rate limit to %s kW (using %.1fV)", self.host, kw, voltage)
        try:
            if not await self.connect():
                return False

            amps = kw * 1000 / voltage
            value = int(amps * 1000)
            success = await self._write_register(self.REG_MAX_CHARGE_CURRENT, value)
            if not success:
                _LOGGER.error("Failed to set charge rate limit")
                return False

            _LOGGER.info("Sungrow SH charge rate limit set to %s kW (%.1f A @ %.1fV)", kw, amps, voltage)
            return True

        except Exception as e:
            _LOGGER.error("Error setting charge rate limit: %s", e)
            return False

    async def set_discharge_rate_limit(self, kw: float) -> bool:
        """Set maximum discharge rate in kW.

        Converts kW to Amps using the actual battery voltage (read from
        register 13019), falling back to 48V if not yet available.
        """
        voltage = self._battery_voltage
        _LOGGER.info("Setting Sungrow SH at %s discharge rate limit to %s kW (using %.1fV)", self.host, kw, voltage)
        try:
            if not await self.connect():
                return False

            amps = kw * 1000 / voltage
            value = int(amps * 1000)
            success = await self._write_register(self.REG_MAX_DISCHARGE_CURRENT, value)
            if not success:
                _LOGGER.error("Failed to set discharge rate limit")
                return False

            _LOGGER.info("Sungrow SH discharge rate limit set to %s kW (%.1f A @ %.1fV)", kw, amps, voltage)
            return True

        except Exception as e:
            _LOGGER.error("Error setting discharge rate limit: %s", e)
            return False

    async def set_max_soc(self, percent: int) -> bool:
        """Set maximum battery SOC percentage (0-100%).

        Args:
            percent: Maximum SOC limit (e.g. 90 for 90%)

        Returns:
            True if successful
        """
        _LOGGER.info("Setting Sungrow SH at %s max SOC to %d%%", self.host, percent)
        try:
            if not await self.connect():
                return False
            value = int(percent * 10)  # 0.1% scale
            return await self._write_register(self.REG_MAX_SOC, value)
        except Exception as e:
            _LOGGER.error("Error setting max SOC: %s", e)
            return False

    async def set_export_limit(self, watts: Optional[int]) -> bool:
        """Set export power limit in watts.

        Args:
            watts: Export limit in watts, or None to disable limit

        Returns:
            True if successful, False otherwise
        """
        try:
            if not await self.connect():
                return False

            if watts is None:
                # Disable export limit
                _LOGGER.info(f"Disabling Sungrow SH at {self.host} export limit")
                success = await self._write_register(self.REG_EXPORT_LIMIT_ENABLED, 0)
            else:
                # Set and enable export limit
                _LOGGER.info(f"Setting Sungrow SH at {self.host} export limit to {watts} W")
                success = await self._write_register(self.REG_EXPORT_LIMIT_SETTING, watts)
                if success:
                    success = await self._write_register(self.REG_EXPORT_LIMIT_ENABLED, 1)

            return success

        except Exception as e:
            _LOGGER.error(f"Error setting export limit: {e}")
            return False

    async def get_battery_data(self) -> dict:
        """Read all battery-related registers for coordinator use.

        Returns:
            Dictionary with battery data including SOC, SOH, power, and settings
        """
        data = {}

        try:
            if not await self.connect():
                return data

            # Read battery state registers (13018-13024)
            battery_regs = await self._read_register(self.REG_BATTERY_VOLTAGE, 7)
            if battery_regs and len(battery_regs) >= 7:
                voltage = round(battery_regs[0] * 0.1, 1)
                data["battery_voltage"] = voltage
                if voltage > 0:
                    self._battery_voltage = voltage
                data["battery_current"] = round(self._to_signed16(battery_regs[1]) * 0.1, 1)
                data["battery_power"] = self._to_signed16(battery_regs[2])
                data["battery_soc"] = round(battery_regs[3] * 0.1, 1)
                data["battery_soh"] = round(battery_regs[4] * 0.1, 1)
                data["battery_temp"] = round(self._to_signed16(battery_regs[5]) * 0.1, 1)
                data["daily_battery_discharge"] = round(battery_regs[6] * 0.1, 2)

            # Read daily battery charge
            daily_charge = await self._read_register(self.REG_DAILY_BATTERY_CHARGE, 1)
            if daily_charge:
                data["daily_battery_charge"] = round(daily_charge[0] * 0.1, 2)

            # Read load power (32-bit signed)
            load_power = await self._read_register(self.REG_LOAD_POWER, 2)
            if load_power and len(load_power) >= 2:
                data["load_power"] = self._to_signed32(load_power[0], load_power[1])

            # Read export power (32-bit signed)
            export_power = await self._read_register(self.REG_EXPORT_POWER, 2)
            if export_power and len(export_power) >= 2:
                data["export_power"] = self._to_signed32(export_power[0], export_power[1])

            # Read EMS mode
            ems_mode = await self._read_register(self.REG_EMS_MODE, 1)
            if ems_mode:
                data["ems_mode"] = ems_mode[0]
                data["ems_mode_name"] = {
                    0: "self_consumption",
                    2: "forced",
                    3: "external_ems",
                    4: "vpp",
                }.get(ems_mode[0], "unknown")

            # Read charge command state
            charge_cmd = await self._read_register(self.REG_CHARGE_CMD, 1)
            if charge_cmd:
                data["charge_cmd"] = charge_cmd[0]

            # Read min/max SOC
            min_soc = await self._read_register(self.REG_MIN_SOC, 1)
            if min_soc:
                data["min_soc"] = round(min_soc[0] * 0.1, 1)

            max_soc = await self._read_register(self.REG_MAX_SOC, 1)
            if max_soc:
                data["max_soc"] = round(max_soc[0] * 0.1, 1)

            # Read backup reserve
            backup_reserve = await self._read_register(self.REG_BACKUP_RESERVE, 1)
            if backup_reserve:
                data["backup_reserve"] = round(backup_reserve[0] * 0.1, 1)

            # Read charge/discharge current limits and convert to kW using actual voltage
            max_charge_current = await self._read_register(self.REG_MAX_CHARGE_CURRENT, 1)
            if max_charge_current:
                amps = max_charge_current[0] / 1000  # Convert from milliamps
                data["charge_rate_limit_kw"] = round(amps * self._battery_voltage / 1000, 2)

            max_discharge_current = await self._read_register(self.REG_MAX_DISCHARGE_CURRENT, 1)
            if max_discharge_current:
                amps = max_discharge_current[0] / 1000  # Convert from milliamps
                data["discharge_rate_limit_kw"] = round(amps * self._battery_voltage / 1000, 2)

            # Read export limit
            export_limit = await self._read_register(self.REG_EXPORT_LIMIT_SETTING, 1)
            if export_limit:
                data["export_limit_w"] = export_limit[0]

            export_limit_enabled = await self._read_register(self.REG_EXPORT_LIMIT_ENABLED, 1)
            if export_limit_enabled:
                data["export_limit_enabled"] = export_limit_enabled[0] == 1

        except Exception as e:
            _LOGGER.error(f"Error reading battery data: {e}")

        return data

    async def __aenter__(self):
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.disconnect()
