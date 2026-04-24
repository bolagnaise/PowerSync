"""Voltx inverter/battery controller via Modbus TCP.

Supports Voltx / Solplanet hybrid inverter-battery systems that expose the
AISWEI Modbus register map used by the proof-of-concept integration.

Key control registers:
- 1103: work mode (2=self-consumption, 4=custom)
- 1152: signed charge/discharge power command (negative=charge, positive=discharge)
- 1153: SOC max (% * 100)
- 1154: SOC min (% * 100)
"""

from __future__ import annotations

import asyncio
import logging
import struct
from typing import Optional

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException
import pymodbus

from .base import InverterController, InverterState, InverterStatus

_LOGGER = logging.getLogger(__name__)

try:
    _pymodbus_version = tuple(int(x) for x in pymodbus.__version__.split(".")[:2])
    # pymodbus renamed the unit-id kwarg in 3.9; detect it once so the rest of
    # the controller can stay agnostic to the installed library version.
    _SLAVE_PARAM = "device_id" if _pymodbus_version >= (3, 9) else "slave"
except Exception:
    _SLAVE_PARAM = "slave"


def _s16(raw: int) -> int:
    """Interpret a raw u16 register as signed int16."""
    return struct.unpack(">h", struct.pack(">H", raw & 0xFFFF))[0]


def _s32(hi: int, lo: int) -> int:
    """Combine two u16 registers into a signed int32."""
    raw = ((hi & 0xFFFF) << 16) | (lo & 0xFFFF)
    return struct.unpack(">i", struct.pack(">I", raw))[0]


class VoltxController(InverterController):
    """Controller for Voltx / Solplanet battery systems via Modbus TCP."""

    REG_WORK_MODE = 1103
    REG_CLOUD_STATUS = 1150
    REG_CHPWR = 1152
    REG_SOC_MAX = 1153
    REG_SOC_MIN = 1154

    WORK_MODE_SELF_CONSUMPTION = 2
    WORK_MODE_RESERVE_POWER = 3
    WORK_MODE_CUSTOM = 4
    WORK_MODE_TOU = 5

    DEFAULT_PORT = 502
    DEFAULT_SLAVE_ID = 3
    TIMEOUT_SECONDS = 10.0
    DEFAULT_FORCE_POWER_W = 5000.0

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        slave_id: int = DEFAULT_SLAVE_ID,
        model: Optional[str] = None,
    ) -> None:
        super().__init__(host, port, slave_id, model)
        self._client: Optional[AsyncModbusTcpClient] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> bool:
        """Open the Modbus TCP connection."""
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
                self._connected = connected
                if not connected:
                    _LOGGER.error(
                        "Failed to connect to Voltx at %s:%s", self.host, self.port
                    )
                return connected
            except Exception as err:
                _LOGGER.error("Error connecting to Voltx: %s", err)
                self._connected = False
                return False

    async def disconnect(self) -> None:
        """Close the Modbus TCP connection."""
        async with self._lock:
            if self._client:
                self._client.close()
                self._client = None
            self._connected = False

    async def _read_input_registers(self, address: int, count: int) -> Optional[list[int]]:
        """Read input registers."""
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
                return None
            return result.registers
        except ModbusException as err:
            _LOGGER.debug("Voltx input read failed at %d: %s", address, err)
            return None
        except Exception as err:
            _LOGGER.debug("Voltx input read error at %d: %s", address, err)
            return None

    async def _read_holding_registers(self, address: int, count: int) -> Optional[list[int]]:
        """Read holding registers."""
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
                return None
            return result.registers
        except ModbusException as err:
            _LOGGER.debug("Voltx holding read failed at %d: %s", address, err)
            return None
        except Exception as err:
            _LOGGER.debug("Voltx holding read error at %d: %s", address, err)
            return None

    async def _write_register(self, address: int, value: int) -> bool:
        """Write a single holding register."""
        if not self._client or not self._client.connected:
            if not await self.connect():
                return False
        try:
            result = await self._client.write_register(
                address=address,
                value=value & 0xFFFF,
                **{_SLAVE_PARAM: self.slave_id},
            )
            if result.isError():
                _LOGGER.warning("Voltx register write failed at %d: %s", address, result)
                return False
            return True
        except ModbusException as err:
            _LOGGER.warning("Voltx Modbus write failed at %d: %s", address, err)
            return False
        except Exception as err:
            _LOGGER.warning("Voltx register write error at %d: %s", address, err)
            return False

    async def _write_force_mode(self, power_w: float) -> bool:
        """Enable custom mode and set the signed charge/discharge power."""
        power = int(round(power_w))
        # PowerSync handles duration and auto-restore outside the inverter, so a
        # force command only needs to enter custom mode and write the setpoint.
        if not await self._write_register(self.REG_WORK_MODE, self.WORK_MODE_CUSTOM):
            return False
        return await self._write_register(self.REG_CHPWR, power)

    async def force_charge(self, power_kw: float = 5.0) -> bool:
        """Force charge using custom mode and a negative power setpoint."""
        power_w = abs(power_kw * 1000) or self.DEFAULT_FORCE_POWER_W
        return await self._write_force_mode(-power_w)

    async def force_discharge(self, power_kw: float = 5.0) -> bool:
        """Force discharge using custom mode and a positive power setpoint."""
        power_w = abs(power_kw * 1000) or self.DEFAULT_FORCE_POWER_W
        return await self._write_force_mode(power_w)

    async def restore_normal(self) -> bool:
        """Restore self-consumption mode and clear any custom power setpoint."""
        ok = await self._write_register(self.REG_CHPWR, 0)
        ok = await self._write_register(
            self.REG_WORK_MODE, self.WORK_MODE_SELF_CONSUMPTION
        ) and ok
        return ok

    async def set_self_consumption_mode(self) -> bool:
        """Alias for restore_normal used by the generic service handler."""
        return await self.restore_normal()

    async def set_backup_reserve(self, percent: int) -> bool:
        """Set the minimum SOC reserve percentage."""
        percent = max(0, min(100, int(percent)))
        return await self._write_register(self.REG_SOC_MIN, percent * 100)

    async def get_backup_reserve(self) -> int | None:
        """Read the currently configured minimum SOC reserve."""
        regs = await self._read_holding_registers(self.REG_SOC_MIN, 1)
        if not regs:
            return None
        return int(regs[0] // 100)

    async def curtail(
        self,
        home_load_w: Optional[float] = None,
        rated_capacity_w: Optional[float] = None,
    ) -> bool:
        """Voltx battery control does not implement separate solar curtailment."""
        _LOGGER.warning("Voltx controller does not support AC inverter curtailment")
        return False

    async def restore(self) -> bool:
        """Restore normal inverter operation."""
        return await self.restore_normal()

    async def get_status(self) -> InverterState:
        """Read the current inverter and battery state."""
        if not await self.connect():
            return InverterState(
                status=InverterStatus.OFFLINE,
                is_curtailed=False,
                error_message="Cannot connect to Voltx inverter",
            )

        inv_regs = await self._read_input_registers(1300, 80)
        batt_status_regs = await self._read_input_registers(1606, 2)
        batt_regs = await self._read_input_registers(1616, 13)
        hold_regs = await self._read_holding_registers(1100, 55)

        if not inv_regs or not batt_regs or not hold_regs:
            return InverterState(
                status=InverterStatus.ERROR,
                is_curtailed=False,
                error_message="Voltx inverter returned incomplete Modbus data",
            )

        pac_w = _s32(inv_regs[1370 - 1300], inv_regs[1371 - 1300])
        grid_voltage = round(inv_regs[1358 - 1300] * 0.1, 1)
        grid_current = round(inv_regs[1359 - 1300] * 0.1, 1)
        grid_frequency = round(inv_regs[1367 - 1300] * 0.01, 2)
        battery_power_w = _s32(batt_regs[2], batt_regs[3])
        battery_voltage = round(batt_regs[0] * 0.01, 2)
        battery_current = round(_s16(batt_regs[1]) * 0.1, 1)
        battery_soc = batt_regs[5]
        battery_soh = batt_regs[6]
        charge_limit_a = round(batt_regs[7] * 0.1, 1)
        discharge_limit_a = round(batt_regs[8] * 0.1, 1)
        charge_energy_today = round(
            (((batt_regs[9] & 0xFFFF) << 16) | (batt_regs[10] & 0xFFFF)) * 0.1, 1
        )
        discharge_energy_today = round(
            (((batt_regs[11] & 0xFFFF) << 16) | (batt_regs[12] & 0xFFFF)) * 0.1, 1
        )

        work_mode_raw = hold_regs[self.REG_WORK_MODE - 1100]
        charge_power_command_w = _s16(hold_regs[self.REG_CHPWR - 1100])
        soc_max = hold_regs[self.REG_SOC_MAX - 1100] // 100
        soc_min = hold_regs[self.REG_SOC_MIN - 1100] // 100

        # The public Modbus TCP registers do not expose full site CT telemetry,
        # so grid/load/solar are inferred from inverter active power and battery flow.
        grid_power_kw = (-pac_w if pac_w < 0 else 0) / 1000.0
        load_power_kw = max(0.0, (pac_w + max(0, grid_power_kw * 1000)) / 1000.0)
        solar_power_kw = max(0.0, (pac_w - battery_power_w) / 1000.0)
        battery_power_kw = battery_power_w / 1000.0
        max_charge_power_w = round(charge_limit_a * battery_voltage)
        max_discharge_power_w = round(discharge_limit_a * battery_voltage)

        attributes = {
            "ac_power_kw": pac_w / 1000.0,
            "pv_power_kw": solar_power_kw,
            "grid_power_kw": grid_power_kw,
            "load_power_kw": load_power_kw,
            "battery_power_kw": battery_power_kw,
            "battery_soc": battery_soc,
            "battery_soh": battery_soh,
            "battery_voltage_v": battery_voltage,
            "battery_current_a": battery_current,
            "battery_temperature_c": round(_s16(batt_regs[4]) * 0.1, 1),
            "grid_voltage_v": grid_voltage,
            "grid_current_a": grid_current,
            "grid_frequency_hz": grid_frequency,
            "charge_energy_today_kwh": charge_energy_today,
            "discharge_energy_today_kwh": discharge_energy_today,
            "battery_max_charge_power_w": max_charge_power_w,
            "battery_max_discharge_power_w": max_discharge_power_w,
            "battery_max_charge_power_kw": max_charge_power_w / 1000.0,
            "battery_max_discharge_power_kw": max_discharge_power_w / 1000.0,
            "work_mode_raw": work_mode_raw,
            "charge_power_command_w": charge_power_command_w,
            "backup_reserve_percent": soc_min,
            "battery_soc_max": soc_max,
            "cloud_status_raw": hold_regs[self.REG_CLOUD_STATUS - 1100],
        }
        if batt_status_regs and len(batt_status_regs) >= 2:
            attributes["battery_comm_status"] = batt_status_regs[0]
            attributes["battery_status_raw"] = batt_status_regs[1]

        state = InverterState(
            status=InverterStatus.ONLINE,
            is_curtailed=False,
            power_output_w=float(pac_w),
            attributes=attributes,
        )
        self._last_state = state
        return state

    async def __aenter__(self):
        """Context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        await self.disconnect()
