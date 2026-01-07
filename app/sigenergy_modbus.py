# app/sigenergy_modbus.py
"""Sigenergy Modbus TCP client for Flask.

Provides real-time power data from Sigenergy inverters via Modbus TCP.
Based on the Home Assistant PowerSync integration's Sigenergy controller.
"""

import logging
from typing import Optional
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

logger = logging.getLogger(__name__)


class SigenergyModbusClient:
    """Client for reading Sigenergy inverter data via Modbus TCP."""

    # Modbus register addresses - use FULL addresses (pymodbus handles protocol details)
    # Reference: https://github.com/TypQxQ/Sigenergy-Local-Modbus

    # Holding registers (read/write)
    REG_GRID_EXPORT_LIMIT = 40038         # Grid export limit (U32, gain 1000)
    REG_ESS_MAX_CHARGE_LIMIT = 40032      # Max charge rate (U32, gain 1000, kW)
    REG_ESS_MAX_DISCHARGE_LIMIT = 40034   # Max discharge rate (U32, gain 1000, kW)

    # Input registers (read-only) - Real-time power
    REG_PV_POWER = 30035                  # PV power (S32, gain 1000, kW)
    REG_ACTIVE_POWER = 30031              # Active power (S32, gain 1000, kW)
    REG_ESS_SOC = 30014                   # Battery SOC (U16, gain 10, %)
    REG_ESS_POWER = 30037                 # Battery power (S32, gain 1000, kW) - positive=discharge, negative=charge
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

    # Constants
    GAIN_POWER = 1000  # kW → scaled value
    GAIN_SOC = 10      # % → scaled value
    GAIN_ENERGY = 100  # kWh → scaled value for energy registers
    EXPORT_LIMIT_UNLIMITED = 0xFFFFFFFE
    DEFAULT_MAX_RATE_KW = 10.0  # Default max charge/discharge rate in kW

    def __init__(self, host: str, port: int = 502, slave_id: int = 1):
        """Initialize Sigenergy Modbus client.

        Args:
            host: IP address of Sigenergy system
            port: Modbus TCP port (default: 502)
            slave_id: Modbus slave ID (default: 1)
        """
        self.host = host
        self.port = port
        self.slave_id = slave_id
        self._client: Optional[ModbusTcpClient] = None

    def connect(self) -> bool:
        """Connect to the Sigenergy system."""
        try:
            if self._client and self._client.connected:
                return True

            self._client = ModbusTcpClient(
                host=self.host,
                port=self.port,
                timeout=10,
            )

            connected = self._client.connect()
            if connected:
                logger.info(f"Connected to Sigenergy at {self.host}:{self.port}")
            else:
                logger.error(f"Failed to connect to Sigenergy at {self.host}:{self.port}")

            return connected

        except Exception as e:
            logger.error(f"Error connecting to Sigenergy: {e}")
            return False

    def disconnect(self):
        """Disconnect from the Sigenergy system."""
        if self._client:
            self._client.close()
            self._client = None
        logger.debug(f"Disconnected from Sigenergy at {self.host}")

    def _to_signed32(self, high: int, low: int) -> int:
        """Convert two unsigned 16-bit registers to signed 32-bit."""
        value = (high << 16) | low
        if value >= 0x80000000:
            value -= 0x100000000
        return value

    def _to_unsigned32(self, high: int, low: int) -> int:
        """Convert two unsigned 16-bit registers to unsigned 32-bit."""
        return (high << 16) | low

    def _to_unsigned64(self, regs: list) -> int:
        """Convert four unsigned 16-bit registers to unsigned 64-bit.

        Register order: [high_high, high_low, low_high, low_low]
        """
        if len(regs) < 4:
            return 0
        return (regs[0] << 48) | (regs[1] << 32) | (regs[2] << 16) | regs[3]

    def _read_input_registers(self, address: int, count: int = 1) -> Optional[list]:
        """Read values from input registers."""
        if not self._client or not self._client.connected:
            if not self.connect():
                return None

        try:
            result = self._client.read_input_registers(
                address=address,
                count=count,
                slave=self.slave_id,
            )

            if result.isError():
                logger.debug(f"Modbus read error at input register {address}: {result}")
                return None

            return result.registers

        except ModbusException as e:
            logger.debug(f"Modbus exception reading input register {address}: {e}")
            return None
        except Exception as e:
            logger.debug(f"Error reading input register {address}: {e}")
            return None

    def _read_holding_registers(self, address: int, count: int = 1) -> Optional[list]:
        """Read values from holding registers."""
        if not self._client or not self._client.connected:
            if not self.connect():
                return None

        try:
            result = self._client.read_holding_registers(
                address=address,
                count=count,
                slave=self.slave_id,
            )

            if result.isError():
                logger.debug(f"Modbus read error at holding register {address}: {result}")
                return None

            return result.registers

        except ModbusException as e:
            logger.debug(f"Modbus exception reading holding register {address}: {e}")
            return None
        except Exception as e:
            logger.debug(f"Error reading holding register {address}: {e}")
            return None

    def _write_holding_registers(self, address: int, values: list) -> bool:
        """Write values to holding registers."""
        if not self._client or not self._client.connected:
            if not self.connect():
                return False

        try:
            result = self._client.write_registers(
                address=address,
                values=values,
                slave=self.slave_id,
            )

            if result.isError():
                logger.error(f"Modbus write error at holding register {address}: {result}")
                return False

            return True

        except ModbusException as e:
            logger.error(f"Modbus exception writing holding register {address}: {e}")
            return False
        except Exception as e:
            logger.error(f"Error writing holding register {address}: {e}")
            return False

    def set_export_limit(self, limit_kw: float) -> bool:
        """Set the grid export limit.

        Args:
            limit_kw: Export limit in kW. Use 0 to disable export (curtailment).

        Returns:
            True on success, False on failure.
        """
        try:
            if not self.connect():
                return False

            # Convert kW to scaled value (gain 1000)
            limit_scaled = int(limit_kw * self.GAIN_POWER)

            # U32 requires 2 registers (high word, low word)
            high_word = (limit_scaled >> 16) & 0xFFFF
            low_word = limit_scaled & 0xFFFF

            success = self._write_holding_registers(self.REG_GRID_EXPORT_LIMIT, [high_word, low_word])

            if success:
                logger.info(f"Set Sigenergy export limit to {limit_kw} kW (scaled: {limit_scaled})")
            else:
                logger.error(f"Failed to set Sigenergy export limit")

            return success

        except Exception as e:
            logger.error(f"Error setting export limit: {e}")
            return False

        finally:
            self.disconnect()

    def restore_export_limit(self) -> bool:
        """Restore the export limit to unlimited (normal operation).

        Returns:
            True on success, False on failure.
        """
        try:
            if not self.connect():
                return False

            # Set to unlimited value (0xFFFFFFFE)
            high_word = (self.EXPORT_LIMIT_UNLIMITED >> 16) & 0xFFFF
            low_word = self.EXPORT_LIMIT_UNLIMITED & 0xFFFF

            success = self._write_holding_registers(self.REG_GRID_EXPORT_LIMIT, [high_word, low_word])

            if success:
                logger.info(f"Restored Sigenergy export limit to unlimited")
            else:
                logger.error(f"Failed to restore Sigenergy export limit")

            return success

        except Exception as e:
            logger.error(f"Error restoring export limit: {e}")
            return False

        finally:
            self.disconnect()

    def set_charge_rate_limit(self, limit_kw: float) -> bool:
        """Set the maximum battery charge rate.

        Args:
            limit_kw: Charge rate limit in kW (0 to disable charging, max ~10 kW).

        Returns:
            True on success, False on failure.
        """
        try:
            if not self.connect():
                return False

            # Convert kW to scaled value (gain 1000)
            limit_scaled = int(limit_kw * self.GAIN_POWER)

            # U32 requires 2 registers (high word, low word)
            high_word = (limit_scaled >> 16) & 0xFFFF
            low_word = limit_scaled & 0xFFFF

            success = self._write_holding_registers(self.REG_ESS_MAX_CHARGE_LIMIT, [high_word, low_word])

            if success:
                logger.info(f"Set Sigenergy charge rate limit to {limit_kw} kW")
            else:
                logger.error(f"Failed to set Sigenergy charge rate limit")

            return success

        except Exception as e:
            logger.error(f"Error setting charge rate limit: {e}")
            return False

        finally:
            self.disconnect()

    def set_discharge_rate_limit(self, limit_kw: float) -> bool:
        """Set the maximum battery discharge rate.

        Args:
            limit_kw: Discharge rate limit in kW (0 to disable discharging, max ~10 kW).

        Returns:
            True on success, False on failure.
        """
        try:
            if not self.connect():
                return False

            # Convert kW to scaled value (gain 1000)
            limit_scaled = int(limit_kw * self.GAIN_POWER)

            # U32 requires 2 registers (high word, low word)
            high_word = (limit_scaled >> 16) & 0xFFFF
            low_word = limit_scaled & 0xFFFF

            success = self._write_holding_registers(self.REG_ESS_MAX_DISCHARGE_LIMIT, [high_word, low_word])

            if success:
                logger.info(f"Set Sigenergy discharge rate limit to {limit_kw} kW")
            else:
                logger.error(f"Failed to set Sigenergy discharge rate limit")

            return success

        except Exception as e:
            logger.error(f"Error setting discharge rate limit: {e}")
            return False

        finally:
            self.disconnect()

    def get_current_limits(self) -> dict:
        """Get current charge/discharge/export limits.

        Returns:
            dict with current limits:
            - charge_rate_limit_kw: Current charge rate limit in kW
            - discharge_rate_limit_kw: Current discharge rate limit in kW
            - export_limit_kw: Current export limit in kW (None if unlimited)
        """
        try:
            if not self.connect():
                return {"error": "Failed to connect to Sigenergy"}

            result = {}

            # Read charge rate limit (U32, 2 registers)
            charge_regs = self._read_holding_registers(self.REG_ESS_MAX_CHARGE_LIMIT, 2)
            if charge_regs and len(charge_regs) >= 2:
                charge_limit = self._to_unsigned32(charge_regs[0], charge_regs[1])
                result['charge_rate_limit_kw'] = charge_limit / self.GAIN_POWER
            else:
                result['charge_rate_limit_kw'] = None

            # Read discharge rate limit (U32, 2 registers)
            discharge_regs = self._read_holding_registers(self.REG_ESS_MAX_DISCHARGE_LIMIT, 2)
            if discharge_regs and len(discharge_regs) >= 2:
                discharge_limit = self._to_unsigned32(discharge_regs[0], discharge_regs[1])
                result['discharge_rate_limit_kw'] = discharge_limit / self.GAIN_POWER
            else:
                result['discharge_rate_limit_kw'] = None

            # Read export limit (U32, 2 registers)
            export_regs = self._read_holding_registers(self.REG_GRID_EXPORT_LIMIT, 2)
            if export_regs and len(export_regs) >= 2:
                export_limit = self._to_unsigned32(export_regs[0], export_regs[1])
                if export_limit >= self.EXPORT_LIMIT_UNLIMITED:
                    result['export_limit_kw'] = None  # Unlimited
                else:
                    result['export_limit_kw'] = export_limit / self.GAIN_POWER
            else:
                result['export_limit_kw'] = None

            logger.debug(f"Sigenergy limits: charge={result['charge_rate_limit_kw']}kW "
                        f"discharge={result['discharge_rate_limit_kw']}kW "
                        f"export={result['export_limit_kw']}kW")

            return result

        except Exception as e:
            logger.error(f"Error getting current limits: {e}")
            return {"error": str(e)}

        finally:
            self.disconnect()

    def get_live_status(self) -> dict:
        """Get current power status from Sigenergy system.

        Returns:
            dict with power data matching Tesla status format:
            - solar_power: W (positive = generating)
            - battery_power: W (positive = discharging, negative = charging)
            - grid_power: W (positive = importing, negative = exporting)
            - load_power: W (home consumption)
            - percentage_charged: % (battery SOC)
            - is_curtailed: bool (export limit active)
        """
        try:
            if not self.connect():
                return {"error": "Failed to connect to Sigenergy"}

            result = {}

            # Read PV power (S32, 2 registers) - solar generation
            pv_regs = self._read_input_registers(self.REG_PV_POWER, 2)
            if pv_regs and len(pv_regs) >= 2:
                pv_power_kw = self._to_signed32(pv_regs[0], pv_regs[1]) / self.GAIN_POWER
                result['solar_power'] = pv_power_kw * 1000  # Convert to W
            else:
                result['solar_power'] = 0

            # Read grid sensor power (S32, 2 registers)
            # Positive = importing from grid, negative = exporting to grid
            grid_regs = self._read_input_registers(self.REG_GRID_SENSOR_POWER, 2)
            if grid_regs and len(grid_regs) >= 2:
                grid_power_kw = self._to_signed32(grid_regs[0], grid_regs[1]) / self.GAIN_POWER
                result['grid_power'] = grid_power_kw * 1000  # Convert to W
            else:
                result['grid_power'] = 0

            # Read battery SOC (U16)
            soc_regs = self._read_input_registers(self.REG_ESS_SOC, 1)
            if soc_regs:
                result['percentage_charged'] = soc_regs[0] / self.GAIN_SOC
            else:
                result['percentage_charged'] = 0

            # Read battery power (S32, 2 registers) - positive = discharging, negative = charging
            battery_regs = self._read_input_registers(self.REG_ESS_POWER, 2)
            if battery_regs and len(battery_regs) >= 2:
                battery_power_kw = self._to_signed32(battery_regs[0], battery_regs[1]) / self.GAIN_POWER
                result['battery_power'] = battery_power_kw * 1000  # Convert to W
            else:
                result['battery_power'] = 0

            # Calculate home load from energy balance:
            # Load = Solar + Grid + Battery (with proper signs)
            # Solar: positive = generating
            # Grid: positive = importing, negative = exporting
            # Battery: positive = discharging, negative = charging
            solar_w = result.get('solar_power', 0)
            grid_w = result.get('grid_power', 0)
            battery_w = result.get('battery_power', 0)
            result['load_power'] = solar_w + grid_w + battery_w

            # Check if export is curtailed (export limit set to 0 or very low)
            export_regs = self._read_holding_registers(self.REG_GRID_EXPORT_LIMIT, 2)
            if export_regs and len(export_regs) >= 2:
                export_limit = self._to_unsigned32(export_regs[0], export_regs[1])
                result['is_curtailed'] = export_limit < 100  # Less than 0.1 kW
                if export_limit < self.EXPORT_LIMIT_UNLIMITED:
                    result['export_limit_kw'] = export_limit / self.GAIN_POWER
            else:
                result['is_curtailed'] = False

            # Add metadata
            result['battery_system'] = 'sigenergy'
            result['host'] = self.host

            logger.debug(f"Sigenergy status: Solar={result['solar_power']}W Grid={result['grid_power']}W "
                        f"Battery={result['battery_power']}W Load={result['load_power']}W SOC={result['percentage_charged']}%")

            return result

        except Exception as e:
            logger.error(f"Error getting Sigenergy status: {e}")
            return {"error": str(e)}

        finally:
            self.disconnect()

    def get_battery_health(self) -> dict:
        """Get battery health information (SOH and capacity).

        Returns:
            dict with battery health data:
            - battery_soh: Battery State of Health (%)
            - battery_capacity_kwh: Rated battery capacity (kWh)
        """
        try:
            if not self.connect():
                return {"error": "Failed to connect to Sigenergy"}

            result = {}

            # Read battery SOH (U16, gain 10)
            soh_regs = self._read_input_registers(self.REG_ESS_SOH, 1)
            if soh_regs:
                result['battery_soh'] = round(soh_regs[0] / self.GAIN_SOC, 1)

            # Read rated capacity (U32, gain 100, kWh)
            capacity_regs = self._read_input_registers(self.REG_ESS_RATED_CAPACITY, 2)
            if capacity_regs and len(capacity_regs) >= 2:
                capacity_kwh = self._to_unsigned32(capacity_regs[0], capacity_regs[1]) / self.GAIN_ENERGY
                result['battery_capacity_kwh'] = round(capacity_kwh, 2)

            logger.debug(f"Sigenergy battery health: SOH={result.get('battery_soh')}%, capacity={result.get('battery_capacity_kwh')}kWh")

            return result

        except Exception as e:
            logger.error(f"Error getting battery health: {e}")
            return {"error": str(e)}

        finally:
            self.disconnect()

    def get_energy_summary(self) -> dict:
        """Get accumulated energy totals.

        Returns:
            dict with energy statistics in kWh:
            - total_pv_energy_kwh: Lifetime PV generation
            - daily_load_energy_kwh: Today's load consumption
            - total_load_energy_kwh: Lifetime load consumption
            - total_battery_charged_kwh: Lifetime battery charged
            - total_battery_discharged_kwh: Lifetime battery discharged
            - total_grid_import_kwh: Lifetime grid import
            - total_grid_export_kwh: Lifetime grid export
        """
        try:
            if not self.connect():
                return {"error": "Failed to connect to Sigenergy"}

            result = {}

            # Total PV generation (U64, 4 registers, gain 100)
            pv_regs = self._read_input_registers(self.REG_ACCUMULATED_PV_ENERGY, 4)
            if pv_regs and len(pv_regs) >= 4:
                result['total_pv_energy_kwh'] = round(self._to_unsigned64(pv_regs) / self.GAIN_ENERGY, 2)

            # Daily load consumption (U32, 2 registers, gain 100)
            daily_load_regs = self._read_input_registers(self.REG_DAILY_CONSUMED_ENERGY, 2)
            if daily_load_regs and len(daily_load_regs) >= 2:
                result['daily_load_energy_kwh'] = round(self._to_unsigned32(daily_load_regs[0], daily_load_regs[1]) / self.GAIN_ENERGY, 2)

            # Total load consumption (U64, 4 registers, gain 100)
            total_load_regs = self._read_input_registers(self.REG_ACCUMULATED_CONSUMED_ENERGY, 4)
            if total_load_regs and len(total_load_regs) >= 4:
                result['total_load_energy_kwh'] = round(self._to_unsigned64(total_load_regs) / self.GAIN_ENERGY, 2)

            # Total battery charged (U64, 4 registers, gain 100)
            charge_regs = self._read_input_registers(self.REG_ACCUMULATED_BATTERY_CHARGE, 4)
            if charge_regs and len(charge_regs) >= 4:
                result['total_battery_charged_kwh'] = round(self._to_unsigned64(charge_regs) / self.GAIN_ENERGY, 2)

            # Total battery discharged (U64, 4 registers, gain 100)
            discharge_regs = self._read_input_registers(self.REG_ACCUMULATED_BATTERY_DISCHARGE, 4)
            if discharge_regs and len(discharge_regs) >= 4:
                result['total_battery_discharged_kwh'] = round(self._to_unsigned64(discharge_regs) / self.GAIN_ENERGY, 2)

            # Total grid import (U64, 4 registers, gain 100)
            import_regs = self._read_input_registers(self.REG_ACCUMULATED_GRID_IMPORT, 4)
            if import_regs and len(import_regs) >= 4:
                result['total_grid_import_kwh'] = round(self._to_unsigned64(import_regs) / self.GAIN_ENERGY, 2)

            # Total grid export (U64, 4 registers, gain 100)
            export_regs = self._read_input_registers(self.REG_ACCUMULATED_GRID_EXPORT, 4)
            if export_regs and len(export_regs) >= 4:
                result['total_grid_export_kwh'] = round(self._to_unsigned64(export_regs) / self.GAIN_ENERGY, 2)

            logger.debug(f"Sigenergy energy summary: {result}")

            return result

        except Exception as e:
            logger.error(f"Error getting energy summary: {e}")
            return {"error": str(e)}

        finally:
            self.disconnect()


def get_sigenergy_modbus_client(user) -> Optional[SigenergyModbusClient]:
    """Create a SigenergyModbusClient from a user object.

    Args:
        user: User model instance with Sigenergy Modbus settings

    Returns:
        SigenergyModbusClient instance or None if not configured
    """
    if not user.sigenergy_modbus_host:
        return None

    return SigenergyModbusClient(
        host=user.sigenergy_modbus_host,
        port=user.sigenergy_modbus_port or 502,
        slave_id=user.sigenergy_modbus_slave_id or 1,
    )
