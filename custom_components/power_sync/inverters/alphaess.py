"""AlphaESS inverter/battery controller via Modbus TCP.

Supports AlphaESS SMILE / Storion hybrid inverter-battery systems.
Register map sourced from the official AlphaESS parameter address table
(AlphaESS-HouseholdModbusRegisterParameterList).

Key facts (differ from every other brand in PowerSync — read the plan):
- Default slave ID is 0x55 (85), NOT 1 or 247
- Battery power register 0126H: negative = charge, positive = discharge
  (already matches PowerSync convention — no sign flip needed)
- Dispatch uses the 0x0880 block (0x0722 is HHE-MEC only and silently
  no-ops on SMILE).
- Active-power register 0x0881 uses a +32000 OFFSET encoding:
  raw = 32000 + (watts × direction), where direction = -1 for charge
  and +1 for discharge. The PDF's Note29 description of "battery control
  power (W)" direct-signed was misleading; the working Alpha2MQTT
  implementation (proven against SMILE hardware) uses the offset.
- Cutoff SOC (0x0886) uses percent × 2.5 (so 100% → 250, 10% → 25).
- Dispatch Time (0x0887-0x0888) is seconds — inverter auto-stops when
  the timer elapses.
- Write ORDER matters: start → power → time → SOC → mode (LAST). The
  mode write is what actually commits the dispatch configuration.
"""
import asyncio
import logging
from typing import Optional

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException
import pymodbus

from .base import InverterController, InverterState, InverterStatus

_LOGGER = logging.getLogger(__name__)

try:
    _pymodbus_version = tuple(int(x) for x in pymodbus.__version__.split(".")[:2])
    _SLAVE_PARAM = "device_id" if _pymodbus_version >= (3, 9) else "slave"
except Exception:
    _SLAVE_PARAM = "slave"


class AlphaESSController(InverterController):
    """Controller for AlphaESS SMILE / Storion systems via Modbus TCP."""

    # === TELEMETRY REGISTERS (all holding registers, function code 0x03) ===
    # Grid meter
    REG_GRID_TOTAL_ACTIVE_POWER = 0x0021   # S32, 1 W/bit, + = import / − = export (to verify)

    # PV meter (optional, CT-based; use PV total power at 0453H as primary)
    REG_PV_METER_TOTAL_ACTIVE_POWER = 0x00A1  # S32, 1 W/bit

    # Battery
    REG_BAT_VOLTAGE = 0x0100            # U16, 0.1 V/bit
    REG_BAT_CURRENT = 0x0101            # S16, 0.1 A/bit
    REG_BAT_SOC = 0x0102                # U16, 0.1 %/bit  (read: raw/10 = percent)
    REG_BAT_MAX_CHARGE_CURRENT = 0x0111  # U16, 0.1 A/bit (BMS-reported limit)
    REG_BAT_MAX_DISCHARGE_CURRENT = 0x0112  # U16, 0.1 A/bit
    REG_BAT_CAPACITY = 0x0119           # U16, 0.1 kWh/bit
    REG_BAT_SOH = 0x011B                # U16, 0.1 %/bit
    REG_BAT_CHARGE_ENERGY = 0x0120      # U32, 0.1 kWh/bit (cumulative)
    REG_BAT_DISCHARGE_ENERGY = 0x0122   # U32, 0.1 kWh/bit (cumulative)
    REG_BAT_CHARGE_FROM_GRID_ENERGY = 0x0124  # U32, 0.1 kWh/bit
    REG_BAT_POWER = 0x0126              # S16, 1 W/bit, − = charge / + = discharge
    REG_BAT_MAX_CHARGE_POWER = 0x012C   # U16, 1 W/bit
    REG_BAT_MAX_DISCHARGE_POWER = 0x012D  # U16, 1 W/bit

    # Inverter
    REG_INV_WORK_MODE = 0x0440          # U16, Note5 enum (value meaning TBD from hardware testing)
    REG_INV_BAT_POWER = 0x0443          # S16, 1 W/bit (redundant with 0126H)
    REG_PV_TOTAL_POWER = 0x0453         # U32, 1 W/bit — primary solar reading
    REG_PV1_POWER = 0x041F              # U32, 1 W/bit (per-string, PV1..PV6)
    REG_PV2_POWER = 0x0423
    REG_PV3_POWER = 0x0427
    REG_PV4_POWER = 0x042B
    REG_PV5_POWER = 0x042F
    REG_PV6_POWER = 0x0433

    # === CONTROL REGISTERS (R/W) ===
    # Power Dispatch block at 0x0880-0x088A (Note29 "Power Dispatch parameter list").
    # This is the block that ACTUALLY works on SMILE / Storion hardware.
    #
    # An older block at 0x0722-0x0728 exists in the PDF under "Household System
    # (Only applicable to HHE MEC)". We initially shipped against that and got
    # confirmed acknowledgements with no battery movement — wrong hardware
    # target. Live SMILE hardware uses 0x0880 per Hillview Lodge and Note29.
    REG_DISPATCH_START = 0x0880        # U16: 1=start, 0=stop
    REG_DISPATCH_ACTIVE_POWER = 0x0881  # U32, +32000 offset: raw = 32000 + watts × direction
    REG_DISPATCH_MODE = 0x0885         # U16 mode enum (written LAST to commit)
    REG_DISPATCH_SOC = 0x0886          # U16, percent × 2.5 (100% → 250, 10% → 25)
    REG_DISPATCH_TIME = 0x0887         # U32, seconds — inverter auto-stops when timer elapses
    # Para3 (0x0883 reactive power), Para7 (0x0889 direction) and Para8
    # (0x088A PV switch) are intentionally left alone. The Alpha2MQTT
    # reference implementation does not write them for mode 2 charge/discharge,
    # and earlier hardware tests showed writing Para7/Para8 prevented the
    # inverter from executing the dispatch.

    # Direction constants (used for logging + the +32000 offset calculation;
    # no actual Para7 register is written — see class docstring).
    DISPATCH_DIRECTION_CHARGE = -1    # power field multiplier for charge
    DISPATCH_DIRECTION_DISCHARGE = 1  # power field multiplier for discharge
    DISPATCH_POWER_OFFSET = 32000     # raw = OFFSET + watts × direction

    # Export / feed-in limit
    REG_MAX_FEED_INTO_GRID_PERCENT = 0x0800  # U16, 1 %/bit, 0 = zero export, 100 = unlimited

    # Scale / offset constants
    GAIN_SOC = 10             # 0.1 %/bit for read (0102H, 011BH)
    # Cutoff SOC for Para5 (0x0886): encoding is raw = percent × 2.5, so 100% → 250
    DISPATCH_CUTOFF_SOC_SCALE = 2.5
    EXPORT_LIMIT_ZERO = 0     # 0% → zero export
    EXPORT_LIMIT_UNLIMITED = 100  # 100% → unlimited export
    # Default Dispatch Time when the caller doesn't pass one. Keep this short
    # enough that a lost connection doesn't strand the battery in forced mode.
    # The coordinator normally passes the real duration from force_charge /
    # force_discharge calls.
    DEFAULT_DISPATCH_SECONDS = 3600

    # Dispatch Mode values (written to Para4 at 0x0885). Same enum as Note7;
    # mode 2 "State of Charge Control" is the direct power-setpoint mode.
    #
    #   1  Battery only charges from PV
    #   2  State of Charge Control  ← Para2 = signed W, Para5 = cutoff SOC
    #   3  Load Following
    #   4  Maximise Output
    #   5  Normal Mode
    #   6  Optimise Consumption
    #   7  Maximise Consumption
    #  19  No Battery Charge
    #
    # For mode 2:
    #   - Para2 > 0 (positive watts) AND cutoff SOC < current battery SOC
    #       → discharge at the configured rate until cutoff SOC is reached
    #   - Para2 < 0 (negative watts) AND cutoff SOC > current battery SOC
    #       → charge from grid at the configured rate until cutoff SOC reached
    DISPATCH_MODE_SOC_CONTROL = 2

    # Connection defaults
    DEFAULT_PORT = 502
    DEFAULT_SLAVE_ID = 85    # 0x55 — AlphaESS default from register 080FH
    TIMEOUT_SECONDS = 10.0

    def __init__(
        self,
        host: str,
        port: int = 502,
        slave_id: int = 85,
        model: Optional[str] = None,
        max_export_limit_kw: Optional[float] = None,
    ):
        """Initialize AlphaESS controller.

        Args:
            host: IP address of the AlphaESS inverter.
            port: Modbus TCP port (default 502).
            slave_id: Modbus slave ID (default 85 / 0x55).
            model: AlphaESS model string (e.g. "smile5", "storion-t30") for display only.
            max_export_limit_kw: User-configured export safety cap in kW.
        """
        super().__init__(host, port, slave_id, model)
        self._client: Optional[AsyncModbusTcpClient] = None
        self._lock = asyncio.Lock()
        self._configured_max_export_limit_kw = max_export_limit_kw
        self._original_export_percent: Optional[int] = None  # Previous 0800H for restore
        self._dispatch_active: bool = False                  # Track whether we hold 0722H=1

    # ---- Connection lifecycle ----

    async def connect(self) -> bool:
        """Open Modbus TCP connection to the inverter."""
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
                        f"Connected to AlphaESS at {self.host}:{self.port} (slave={self.slave_id})"
                    )
                else:
                    _LOGGER.error(f"Failed to connect to AlphaESS at {self.host}:{self.port}")
                return connected

            except Exception as e:
                _LOGGER.error(f"Error connecting to AlphaESS: {e}")
                self._connected = False
                return False

    async def disconnect(self) -> None:
        """Close the Modbus TCP connection.

        Note: this intentionally does NOT release forced dispatch. force_charge
        / force_discharge open a connection, write the dispatch block, and
        close it again via an ``async with`` context manager — if we released
        here, the dispatch would be undone milliseconds after being set.

        Use ``release_dispatch()`` or the coordinator's ``async_shutdown``
        path to write ``0x0722=0`` explicitly before the final disconnect on
        integration unload.
        """
        async with self._lock:
            if self._client:
                self._client.close()
                self._client = None
            self._connected = False

    async def release_dispatch(self) -> bool:
        """Explicitly release forced dispatch (write Para1=0) if we hold it.

        Called by the coordinator on shutdown to guarantee the inverter doesn't
        stay locked in charge/discharge after HA unloads. The Para6 duration
        timer would eventually stop it anyway, but writing Para1=0 is the
        authoritative release and is safe even mid-duration.
        """
        if not self._dispatch_active:
            return True
        try:
            ok = await self._write_holding_registers(self.REG_DISPATCH_START, [0])
            if ok:
                _LOGGER.info("AlphaESS dispatch released (0x0880=0)")
                self._dispatch_active = False
                return True
            _LOGGER.warning("Failed to release AlphaESS dispatch — register write returned False")
            return False
        except Exception as e:
            _LOGGER.warning(f"Failed to release AlphaESS dispatch: {e}")
            return False

    # ---- Low-level Modbus I/O ----

    async def _read_holding_registers(self, address: int, count: int = 1) -> Optional[list]:
        """Read N holding registers starting at address."""
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
                _LOGGER.debug(f"Modbus read error at 0x{address:04X}: {result}")
                return None
            return result.registers

        except ModbusException as e:
            _LOGGER.debug(f"Modbus exception reading 0x{address:04X}: {e}")
            return None
        except Exception as e:
            _LOGGER.debug(f"Error reading 0x{address:04X}: {e}")
            return None

    async def _write_holding_registers(self, address: int, values: list[int]) -> bool:
        """Write N holding registers starting at address."""
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
                _LOGGER.error(f"Modbus write error at 0x{address:04X}: {result}")
                return False
            _LOGGER.debug(f"Wrote {values} to 0x{address:04X}")
            return True

        except ModbusException as e:
            _LOGGER.error(f"Modbus exception writing to 0x{address:04X}: {e}")
            return False
        except Exception as e:
            _LOGGER.error(f"Error writing to 0x{address:04X}: {e}")
            return False

    # ---- Type conversion helpers ----

    @staticmethod
    def _to_signed16(value: int) -> int:
        if value >= 0x8000:
            return value - 0x10000
        return value

    @staticmethod
    def _to_signed32(high: int, low: int) -> int:
        value = (high << 16) | low
        if value >= 0x80000000:
            value -= 0x100000000
        return value

    @staticmethod
    def _to_unsigned32(high: int, low: int) -> int:
        return (high << 16) | low

    @staticmethod
    def _from_signed32(value: int) -> list[int]:
        """Encode a signed 32-bit integer as two U16 registers (high, low)."""
        if value < 0:
            value = value + 0x100000000
        return [(value >> 16) & 0xFFFF, value & 0xFFFF]

    # ---- InverterController interface ----

    async def get_status(self) -> InverterState:
        """Read the current system state into an InverterState."""
        try:
            if not await self.connect():
                return InverterState(
                    status=InverterStatus.OFFLINE,
                    is_curtailed=False,
                    error_message="Failed to connect to AlphaESS",
                )

            attrs: dict = {"host": self.host, "model": self.model or "AlphaESS"}

            # Battery SOC (U16, 0.1 %/bit)
            soc_regs = await self._read_holding_registers(self.REG_BAT_SOC, 1)
            if soc_regs:
                attrs["battery_soc"] = round(soc_regs[0] / self.GAIN_SOC, 1)

            # Battery SOH (U16, 0.1 %/bit)
            soh_regs = await self._read_holding_registers(self.REG_BAT_SOH, 1)
            if soh_regs:
                attrs["battery_soh"] = round(soh_regs[0] / self.GAIN_SOC, 1)

            # Battery capacity (U16, 0.1 kWh/bit)
            cap_regs = await self._read_holding_registers(self.REG_BAT_CAPACITY, 1)
            if cap_regs:
                attrs["battery_capacity_kwh"] = round(cap_regs[0] / 10.0, 2)

            # Battery power (S16, 1 W/bit) — − = charge, + = discharge (PowerSync convention)
            bat_regs = await self._read_holding_registers(self.REG_BAT_POWER, 1)
            if bat_regs:
                bat_w = self._to_signed16(bat_regs[0])
                attrs["battery_power_w"] = bat_w
                attrs["battery_power_kw"] = round(bat_w / 1000.0, 3)

            # Battery max charge/discharge power (U16, 1 W/bit) — BMS limits
            max_ch = await self._read_holding_registers(self.REG_BAT_MAX_CHARGE_POWER, 1)
            if max_ch:
                attrs["battery_max_charge_power_w"] = max_ch[0]
            max_dis = await self._read_holding_registers(self.REG_BAT_MAX_DISCHARGE_POWER, 1)
            if max_dis:
                attrs["battery_max_discharge_power_w"] = max_dis[0]

            # Grid total active power (S32, 1 W/bit) — assumed + = import
            grid_regs = await self._read_holding_registers(self.REG_GRID_TOTAL_ACTIVE_POWER, 2)
            if grid_regs and len(grid_regs) >= 2:
                grid_w = self._to_signed32(grid_regs[0], grid_regs[1])
                attrs["grid_power_w"] = grid_w
                attrs["grid_power_kw"] = round(grid_w / 1000.0, 3)

            # PV total power (U32, 1 W/bit)
            pv_regs = await self._read_holding_registers(self.REG_PV_TOTAL_POWER, 2)
            if pv_regs and len(pv_regs) >= 2:
                pv_w = self._to_unsigned32(pv_regs[0], pv_regs[1])
                attrs["pv_power_w"] = pv_w
                attrs["pv_power_kw"] = round(pv_w / 1000.0, 3)

            # Inverter work mode (U16) — raw value, enum mapping TBD from hardware testing
            wm_regs = await self._read_holding_registers(self.REG_INV_WORK_MODE, 1)
            if wm_regs:
                attrs["work_mode_raw"] = wm_regs[0]

            # Export limit (U16, 1 %/bit)
            export_regs = await self._read_holding_registers(self.REG_MAX_FEED_INTO_GRID_PERCENT, 1)
            is_curtailed = False
            if export_regs:
                export_pct = export_regs[0]
                attrs["export_limit_percent"] = export_pct
                is_curtailed = export_pct <= self.EXPORT_LIMIT_ZERO

            # Determine status
            if len(attrs) <= 2:  # Only host/model, no real data
                return InverterState(
                    status=InverterStatus.OFFLINE,
                    is_curtailed=False,
                    error_message="No register data (inverter sleeping?)",
                    attributes=attrs,
                )

            status = InverterStatus.CURTAILED if is_curtailed else InverterStatus.ONLINE
            if is_curtailed:
                attrs["curtailment_mode"] = "zero_export"

            self._last_state = InverterState(
                status=status,
                is_curtailed=is_curtailed,
                power_output_w=attrs.get("pv_power_w"),
                power_limit_percent=attrs.get("export_limit_percent"),
                attributes=attrs,
            )
            return self._last_state

        except Exception as e:
            _LOGGER.error(f"Error reading AlphaESS status: {e}")
            return InverterState(
                status=InverterStatus.ERROR,
                is_curtailed=False,
                error_message=str(e),
            )

    async def curtail(
        self,
        home_load_w: Optional[float] = None,
        rated_capacity_w: Optional[float] = None,
    ) -> bool:
        """Curtail grid export to 0% via register 0800H.

        The inverter self-curtails PV at hardware speed — solar still powers
        the house and charges the battery; only grid export is blocked.
        `home_load_w` / `rated_capacity_w` are accepted for interface
        compatibility but unused (zero-export is always the action).
        """
        try:
            if not await self.connect():
                _LOGGER.error("Cannot curtail: failed to connect to AlphaESS")
                return False

            # Store previous value before overwriting (skip if already curtailed)
            if self._original_export_percent is None:
                current = await self._read_holding_registers(self.REG_MAX_FEED_INTO_GRID_PERCENT, 1)
                if current:
                    self._original_export_percent = current[0]
                    _LOGGER.info(
                        f"AlphaESS stored original export limit: {self._original_export_percent}%"
                    )

            _LOGGER.info(f"Curtailing AlphaESS at {self.host} (0% export)")
            success = await self._write_holding_registers(
                self.REG_MAX_FEED_INTO_GRID_PERCENT, [self.EXPORT_LIMIT_ZERO]
            )
            if success:
                _LOGGER.info("AlphaESS export limit set to 0%")
            else:
                _LOGGER.error(f"Failed to curtail AlphaESS at {self.host}")
            return success

        except Exception as e:
            _LOGGER.error(f"Error curtailing AlphaESS: {e}")
            return False

    async def restore(self) -> bool:
        """Restore grid export to the previously-stored percentage (or 100%)."""
        try:
            if not await self.connect():
                _LOGGER.error("Cannot restore: failed to connect to AlphaESS")
                return False

            restore_pct = self._original_export_percent
            if restore_pct is None or restore_pct <= 0:
                restore_pct = self.EXPORT_LIMIT_UNLIMITED

            _LOGGER.info(f"Restoring AlphaESS export limit to {restore_pct}%")
            success = await self._write_holding_registers(
                self.REG_MAX_FEED_INTO_GRID_PERCENT, [restore_pct]
            )
            if success:
                self._original_export_percent = None
                await asyncio.sleep(1)
                state = await self.get_status()
                if not state.is_curtailed:
                    _LOGGER.info("AlphaESS restore verified — normal export resumed")
                else:
                    _LOGGER.warning("Restore command sent but inverter still reports curtailed")
            else:
                _LOGGER.error(f"Failed to restore AlphaESS at {self.host}")
            return success

        except Exception as e:
            _LOGGER.error(f"Error restoring AlphaESS: {e}")
            return False

    # ---- Extended controls (used by LP optimizer / force-mode services) ----

    async def set_self_consumption_mode(self) -> bool:
        """Release forced dispatch — inverter returns to autonomous self-consumption."""
        try:
            if not await self.connect():
                return False
            success = await self._write_holding_registers(self.REG_DISPATCH_START, [0])
            if success:
                self._dispatch_active = False
                _LOGGER.info("AlphaESS dispatch released (0x0880=0) — self-consumption resumed")
            return success
        except Exception as e:
            _LOGGER.error(f"Error releasing AlphaESS dispatch: {e}")
            return False

    async def set_standby_mode(self) -> bool:
        """IDLE hold by releasing dispatch entirely.

        AlphaESS mode 2 (SoC Control) with power=0 has undefined behaviour per
        Hillview's rules (they require power != 0 and a cutoff on the right
        side of current SOC). Safest idle is to clear Para1 and let the
        inverter return to autonomous — the LP optimizer's IDLE path uses
        self-consumption anyway, so this matches.
        """
        return await self.set_self_consumption_mode()

    async def restore_from_standby(self) -> bool:
        return await self.set_self_consumption_mode()

    async def restore_normal(self) -> bool:
        """Full restore: release dispatch AND restore export limit."""
        release_ok = await self.set_self_consumption_mode()
        restore_ok = await self.restore()
        return release_ok and restore_ok

    async def force_charge(
        self,
        power_kw: float = 5.0,
        target_soc_pct: float = 100.0,
        duration_seconds: int = 3600,
    ) -> bool:
        """Force the battery to charge at the given power from grid.

        Args:
            power_kw: Desired charge power, in kW (positive).
            target_soc_pct: Cutoff SOC (0-100 %). Charging stops when reached.
            duration_seconds: Auto-stop duration — inverter drops out of
                forced dispatch after this many seconds. Coordinator typically
                passes the force-mode duration in seconds.
        """
        power_w = max(0.0, power_kw) * 1000.0
        # Clamp to BMS-reported max charge power if we know it
        max_regs = await self._read_holding_registers(self.REG_BAT_MAX_CHARGE_POWER, 1)
        if max_regs and max_regs[0] > 0 and power_w > max_regs[0]:
            _LOGGER.info(
                f"AlphaESS clamping force_charge from {power_w}W to "
                f"BMS max {max_regs[0]}W"
            )
            power_w = max_regs[0]
        return await self._set_dispatch(
            power_w=power_w,
            target_soc_pct=target_soc_pct,
            direction=self.DISPATCH_DIRECTION_CHARGE,
            duration_seconds=duration_seconds,
        )

    async def force_discharge(
        self,
        power_kw: float = 5.0,
        target_soc_pct: float = 10.0,
        duration_seconds: int = 3600,
    ) -> bool:
        """Force the battery to discharge at the given power to grid.

        Args:
            power_kw: Desired discharge power, in kW (positive).
            target_soc_pct: Floor SOC (0-100 %). Discharge stops when reached.
            duration_seconds: Auto-stop duration (see force_charge).
        """
        power_w = max(0.0, power_kw) * 1000.0
        max_regs = await self._read_holding_registers(self.REG_BAT_MAX_DISCHARGE_POWER, 1)
        if max_regs and max_regs[0] > 0 and power_w > max_regs[0]:
            _LOGGER.info(
                f"AlphaESS clamping force_discharge from {power_w}W to "
                f"BMS max {max_regs[0]}W"
            )
            power_w = max_regs[0]
        return await self._set_dispatch(
            power_w=power_w,
            target_soc_pct=target_soc_pct,
            direction=self.DISPATCH_DIRECTION_DISCHARGE,
            duration_seconds=duration_seconds,
        )

    async def _set_dispatch(
        self,
        power_w: float,
        target_soc_pct: float,
        direction: int,
        duration_seconds: int,
    ) -> bool:
        """Write the dispatch block in the exact order Alpha2MQTT uses.

        Order matters: the mode register (0x0885) is written LAST — writing
        it is what actually commits the configuration. Writing it first (or
        mid-stream) has been observed to either no-op or leave the inverter
        in a partial state where it halts existing motion but refuses to
        execute the new direction.

            1. Start             0x0880 = 1
            2. Active power      0x0881-0x0882 = 32000 + (watts × direction)
            3. Time (seconds)    0x0887-0x0888 = duration
            4. Cutoff SoC        0x0886 = percent × 2.5
            5. Mode              0x0885 = 2 (SoC Control)      ← commit

        Args:
            power_w: Absolute magnitude in watts (always >= 0).
            target_soc_pct: Cutoff SoC in percent.
            direction: DISPATCH_DIRECTION_CHARGE (-1) or
                DISPATCH_DIRECTION_DISCHARGE (+1). Multiplies watts in the
                offset calc.
            duration_seconds: Auto-stop duration.
        """
        try:
            if not await self.connect():
                return False

            abs_watts = int(round(abs(power_w)))
            raw_power = self.DISPATCH_POWER_OFFSET + abs_watts * int(direction)

            # 1. Start = 1
            if not await self._write_holding_registers(
                self.REG_DISPATCH_START, [1]
            ):
                _LOGGER.error("Failed to write AlphaESS dispatch start (0x0880)")
                return False

            # 2. Active power (U32 with +32000 offset)
            hi = (raw_power >> 16) & 0xFFFF
            lo = raw_power & 0xFFFF
            if not await self._write_holding_registers(
                self.REG_DISPATCH_ACTIVE_POWER, [hi, lo]
            ):
                _LOGGER.error("Failed to write AlphaESS dispatch power (0x0881)")
                return False

            # 3. Time (U32 seconds)
            duration_clamped = max(60, int(duration_seconds))
            t_hi = (duration_clamped >> 16) & 0xFFFF
            t_lo = duration_clamped & 0xFFFF
            if not await self._write_holding_registers(
                self.REG_DISPATCH_TIME, [t_hi, t_lo]
            ):
                _LOGGER.error("Failed to write AlphaESS dispatch time (0x0887)")
                return False

            # 4. Cutoff SoC (U16, percent × 2.5)
            soc_clamped = max(0.0, min(100.0, target_soc_pct))
            soc_raw = int(round(soc_clamped * self.DISPATCH_CUTOFF_SOC_SCALE))
            if not await self._write_holding_registers(
                self.REG_DISPATCH_SOC, [soc_raw]
            ):
                _LOGGER.error("Failed to write AlphaESS dispatch SoC (0x0886)")
                return False

            # 5. Mode (commits) — SoC Control
            if not await self._write_holding_registers(
                self.REG_DISPATCH_MODE, [self.DISPATCH_MODE_SOC_CONTROL]
            ):
                _LOGGER.error("Failed to write AlphaESS dispatch mode (0x0885)")
                return False

            self._dispatch_active = True
            action = "CHARGE" if direction < 0 else "DISCHARGE"
            _LOGGER.info(
                "AlphaESS dispatch %s — power=%d W, raw=%d (offset %d), "
                "cutoff_soc=%.1f%% (raw %d), duration=%ds, mode=2",
                action, abs_watts, raw_power, self.DISPATCH_POWER_OFFSET,
                soc_clamped, soc_raw, duration_clamped,
            )
            return True

        except Exception as e:
            _LOGGER.error(f"Error setting AlphaESS dispatch: {e}")
            return False

    async def __aenter__(self):
        """Async context manager entry — opens the Modbus connection."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit — closes the Modbus connection."""
        await self.disconnect()

    async def get_energy_summary(self) -> dict:
        """Read lifetime battery and PV energy totals (kWh)."""
        energy: dict = {}
        try:
            if not await self.connect():
                return {"error": "Failed to connect to AlphaESS"}

            charge_regs = await self._read_holding_registers(self.REG_BAT_CHARGE_ENERGY, 2)
            if charge_regs and len(charge_regs) >= 2:
                energy["total_battery_charged_kwh"] = round(
                    self._to_unsigned32(charge_regs[0], charge_regs[1]) / 10.0, 2
                )

            discharge_regs = await self._read_holding_registers(self.REG_BAT_DISCHARGE_ENERGY, 2)
            if discharge_regs and len(discharge_regs) >= 2:
                energy["total_battery_discharged_kwh"] = round(
                    self._to_unsigned32(discharge_regs[0], discharge_regs[1]) / 10.0, 2
                )

            grid_chg_regs = await self._read_holding_registers(self.REG_BAT_CHARGE_FROM_GRID_ENERGY, 2)
            if grid_chg_regs and len(grid_chg_regs) >= 2:
                energy["total_battery_charged_from_grid_kwh"] = round(
                    self._to_unsigned32(grid_chg_regs[0], grid_chg_regs[1]) / 10.0, 2
                )

            return energy
        except Exception as e:
            _LOGGER.error(f"Error reading AlphaESS energy summary: {e}")
            return {"error": str(e)}
