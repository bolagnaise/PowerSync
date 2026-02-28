"""FoxESS inverter/battery controller via Modbus TCP or RS485 serial.

Supports FoxESS model families: H1, H3, H3-Pro, H3 Smart, KH (plus rebrands).
Control includes force charge/discharge, work mode switching, backup reserve, and solar curtailment.

Reference: https://github.com/nathanmarlor/foxess_modbus
"""
import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from .base import InverterController, InverterState, InverterStatus

_LOGGER = logging.getLogger(__name__)

# pymodbus 3.10+ renamed 'slave' to 'device_id'
def _detect_slave_kwarg() -> str:
    """Detect the correct keyword argument for the Modbus slave/device ID."""
    try:
        import inspect
        sig = inspect.signature(AsyncModbusTcpClient.read_input_registers)
        if "slave" in sig.parameters:
            return "slave"
    except (ValueError, TypeError):
        pass
    return "device_id"

_SLAVE_KWARG: str = _detect_slave_kwarg()

# Modbus scaling factors
GAIN_POWER = 1000       # kW × 0.001 → W (standard models)
GAIN_POWER_H3PRO = 10000  # kW × 0.0001 → W (H3-Pro models)
GAIN_SOC = 1            # % (no scaling)
GAIN_CURRENT = 10       # A × 0.1
GAIN_VOLTAGE = 10       # V × 0.1
GAIN_TEMPERATURE = 10   # °C × 0.1
GAIN_ENERGY = 10        # kWh × 0.1 (energy totals)

# Remote control re-send interval (seconds) — for future periodic resend if needed
REMOTE_CONTROL_RESEND_INTERVAL = 480  # 8 minutes


class FoxESSModelFamily(Enum):
    """FoxESS inverter model families."""
    H1 = "H1"
    H3 = "H3"
    H3_PRO = "H3-Pro"
    H3_SMART = "H3-Smart"
    KH = "KH"
    UNKNOWN = "unknown"


@dataclass
class FoxESSRegisterMap:
    """Model-specific Modbus register addresses for FoxESS inverters."""
    # Battery registers
    battery_soc: int
    battery_power: int          # Scaled by battery_pv_gain, signed (neg=charge, pos=discharge)
    battery_power_is_32bit: bool = False  # H3-Pro uses 32-bit battery power
    battery_voltage: int = 0
    battery_current: int = 0
    battery_temperature: int = 0

    # PV registers
    pv1_power: int = 0         # Scaled by battery_pv_gain
    pv2_power: int = 0         # Scaled by battery_pv_gain
    pv_power_is_32bit: bool = False  # H3-Pro/H3-Smart use 32-bit PV power

    # Grid registers
    grid_power: int = 0        # Scaled by power_gain, signed (neg=export, pos=import)
    grid_power_is_32bit: bool = False  # H3-Pro uses 32-bit grid power

    # CT2 meter registers (AC-coupled inverter measurement)
    ct2_power: int = 0             # Total CT2 power, signed
    ct2_power_is_32bit: bool = False

    # Load registers
    load_power: int = 0        # kW × 0.001

    # Control registers (holding — write)
    min_soc: int = 0           # % backup reserve
    max_charge_current: int = 0   # A × 0.1
    max_discharge_current: int = 0  # A × 0.1
    work_mode: int = 0         # Work mode register address

    # Work mode enum values (differ between register 41000 and 49203)
    # Register 41000 (H1/H3/KH): 0-based (0=Self Use, 1=Feed-in, 2=Backup)
    # Register 49203 (H3-Pro/Smart): 1-based (1=Self Use, 2=Feed-in, 3=Backup, 4=Peak Shaving)
    work_mode_self_use: int = 0
    work_mode_feed_in: int = 1
    work_mode_backup: int = 2

    # Remote control registers
    remote_enable: int = 0     # 0/1
    remote_timeout: int = 0    # seconds
    remote_active_power: int = 0  # W, signed
    remote_active_power_is_32bit: bool = False

    # Energy totals (daily)
    charge_energy_today: int = 0
    discharge_energy_today: int = 0
    energy_is_32bit: bool = False     # H3-Pro/Smart: 32-bit energy registers
    energy_gain: int = 10             # H1/H3/KH: 0.1 kWh (gain=10); H3-Pro/Smart: 0.01 kWh (gain=100)

    # Scaling factors for power registers (varies by model)
    # H1/H3/KH: all registers use gain=1000 (scale 0.001)
    # H3-Pro/H3-Smart: grid_ct uses gain=10000 (scale 0.0001),
    #   battery/PV/load use gain=1000 (scale 0.001)
    power_gain: int = 1000        # Grid power scaling (default 0.001)
    battery_pv_gain: int = 0      # Battery/PV scaling (0 = use power_gain)

    # Feature flags
    supports_bms: bool = True
    supports_energy_totals: bool = True
    supports_work_mode_rw: bool = True
    supports_charge_periods: bool = False
    # H3-Pro and H3 Smart use holding registers for ALL data (no input registers)
    all_holding: bool = False
    # H3-Pro/H3-Smart grid CT returns inverted sign (positive=export, negative=import)
    # compared to H1/H3/KH (positive=import, negative=export). Negate to normalize.
    grid_sign_inverted: bool = False

    def get_work_mode_names(self) -> dict[int, str]:
        """Return model-specific work mode value→name mapping."""
        return {
            self.work_mode_self_use: "Self Use",
            self.work_mode_feed_in: "Feed-in First",
            self.work_mode_backup: "Backup",
        }


# Register maps for each model family
# Reference: https://github.com/nathanmarlor/foxess_modbus/tree/main/custom_components/foxess_modbus/entities
REGISTER_MAPS: dict[FoxESSModelFamily, FoxESSRegisterMap] = {
    FoxESSModelFamily.H1: FoxESSRegisterMap(
        battery_soc=31024,
        battery_power=31022,
        battery_voltage=31020,
        battery_current=31021,
        battery_temperature=31023,
        pv1_power=31002,
        pv2_power=31003,
        grid_power=31008,
        load_power=31016,
        min_soc=41009,
        max_charge_current=41007,
        max_discharge_current=41008,
        work_mode=41000,
        remote_enable=44000,
        remote_timeout=44001,
        remote_active_power=44002,
        charge_energy_today=31088,
        discharge_energy_today=31089,
        supports_bms=False,          # H1 LAN = no BMS; RS485 = BMS
        supports_energy_totals=False,  # H1 LAN = no; RS485 = yes
        supports_work_mode_rw=False,   # H1 LAN = no; RS485 = yes
        supports_charge_periods=False,
    ),
    FoxESSModelFamily.KH: FoxESSRegisterMap(
        battery_soc=31024,
        battery_power=31022,
        battery_voltage=31020,
        battery_current=31021,
        battery_temperature=31023,
        pv1_power=31002,
        pv2_power=31003,
        grid_power=31008,
        load_power=31016,
        min_soc=41009,
        max_charge_current=41007,
        max_discharge_current=41008,
        work_mode=41000,
        remote_enable=44000,
        remote_timeout=44001,
        remote_active_power=44002,
        charge_energy_today=31088,
        discharge_energy_today=31089,
        supports_bms=True,
        supports_energy_totals=True,
        supports_work_mode_rw=True,
        supports_charge_periods=False,
    ),
    FoxESSModelFamily.H3: FoxESSRegisterMap(
        battery_soc=31038,
        battery_power=31036,
        battery_voltage=31034,
        battery_current=31035,
        battery_temperature=31037,
        pv1_power=31002,
        pv2_power=31003,
        grid_power=31008,
        load_power=31016,
        min_soc=41009,
        max_charge_current=41007,
        max_discharge_current=41008,
        work_mode=41000,
        remote_enable=44000,
        remote_timeout=44001,
        remote_active_power=44002,
        charge_energy_today=31088,
        discharge_energy_today=31089,
        supports_bms=True,
        supports_energy_totals=True,
        supports_work_mode_rw=True,
        supports_charge_periods=False,
    ),
    FoxESSModelFamily.H3_PRO: FoxESSRegisterMap(
        battery_soc=37612,
        battery_power=39238,      # 32-bit: 39237 (high) + 39238 (low), scale 0.001
        battery_power_is_32bit=True,
        battery_voltage=37610,
        battery_current=37611,
        battery_temperature=37613,
        pv1_power=39280,          # 32-bit: 39279 (high) + 39280 (low), scale 0.001
        pv2_power=39282,          # 32-bit: 39281 (high) + 39282 (low), scale 0.001
        pv_power_is_32bit=True,
        grid_power=38815,         # 32-bit: 38814 (high) + 38815 (low), scale 0.0001
        grid_power_is_32bit=True,
        ct2_power=38915,          # 32-bit: 38914 (high) + 38915 (low), scale 0.0001
        ct2_power_is_32bit=True,
        load_power=0,             # H3-Pro: calculated from pv + ct2 + grid + battery
        power_gain=10000,         # Grid CT uses 0.0001 kW scaling
        battery_pv_gain=1000,     # Battery/PV use 0.001 kW scaling
        min_soc=46609,
        max_charge_current=46607,
        max_discharge_current=46608,
        work_mode=49203,
        work_mode_self_use=1,     # Register 49203 uses 1-based indexing
        work_mode_feed_in=2,
        work_mode_backup=3,
        remote_enable=46001,
        remote_timeout=46002,
        remote_active_power=46003,  # 32-bit: 46003 + 46004
        remote_active_power_is_32bit=True,
        charge_energy_today=39608,    # 32-bit: [39608, 39607], scale 0.01
        discharge_energy_today=39612, # 32-bit: [39612, 39611], scale 0.01
        energy_is_32bit=True,
        energy_gain=100,              # 0.01 kWh resolution
        supports_bms=True,
        supports_energy_totals=True,
        supports_work_mode_rw=True,
        supports_charge_periods=False,
        all_holding=True,
        grid_sign_inverted=True,
    ),
    # H3 Smart shares the H3-Pro register address space.
    # Native WiFi Modbus TCP — no external adapter needed.
    # Ref: https://github.com/nathanmarlor/foxess_modbus (H3_SMART profile)
    FoxESSModelFamily.H3_SMART: FoxESSRegisterMap(
        battery_soc=37612,
        battery_power=39238,      # 32-bit: scale 0.001
        battery_power_is_32bit=True,
        battery_voltage=37610,
        battery_current=37611,
        battery_temperature=37613,
        pv1_power=39280,          # 32-bit: 39279 (high) + 39280 (low), scale 0.001
        pv2_power=39282,          # 32-bit: 39281 (high) + 39282 (low), scale 0.001
        pv_power_is_32bit=True,
        grid_power=38815,         # 32-bit: scale 0.0001
        grid_power_is_32bit=True,
        ct2_power=38915,          # 32-bit: 38914 (high) + 38915 (low), scale 0.0001
        ct2_power_is_32bit=True,
        load_power=0,             # Calculated from pv + ct2 + grid + battery
        power_gain=10000,         # Grid CT uses 0.0001 kW scaling
        battery_pv_gain=1000,     # Battery/PV use 0.001 kW scaling
        min_soc=46609,
        max_charge_current=46607,
        max_discharge_current=46608,
        work_mode=49203,
        work_mode_self_use=1,     # Register 49203 uses 1-based indexing
        work_mode_feed_in=2,
        work_mode_backup=3,
        remote_enable=46001,
        remote_timeout=46002,
        remote_active_power=46003,
        remote_active_power_is_32bit=True,
        charge_energy_today=39608,    # 32-bit: [39608, 39607], scale 0.01
        discharge_energy_today=39612, # 32-bit: [39612, 39611], scale 0.01
        energy_is_32bit=True,
        energy_gain=100,              # 0.01 kWh resolution
        supports_bms=True,
        supports_energy_totals=True,
        supports_work_mode_rw=True,
        supports_charge_periods=False,
        all_holding=True,
        grid_sign_inverted=True,
    ),
}


class FoxESSController(InverterController):
    """FoxESS inverter/battery controller via Modbus TCP or RS485 serial.

    Provides battery monitoring and control for FoxESS H1, H3, H3-Pro, H3 Smart,
    and KH model families. Supports force charge/discharge via work mode switching
    and remote control registers.

    H3 Smart models have native WiFi Modbus TCP (no external adapter needed).
    """

    def __init__(
        self,
        host: str,
        port: int = 502,
        slave_id: int = 247,
        model: Optional[str] = None,
        connection_type: str = "tcp",
        serial_port: Optional[str] = None,
        baudrate: int = 9600,
        model_family: Optional[str] = None,
    ):
        """Initialize FoxESS controller.

        Args:
            host: IP address for TCP connection
            port: Modbus TCP port (default: 502)
            slave_id: Modbus slave ID (default: 247)
            model: Model name string
            connection_type: "tcp" or "serial"
            serial_port: Serial device path (e.g., /dev/ttyUSB0)
            baudrate: Serial baud rate (default: 9600)
            model_family: Pre-detected model family (H1, H3, H3-Pro, H3-Smart, KH)
        """
        super().__init__(host=host, port=port, slave_id=slave_id, model=model)
        self._client: Optional[AsyncModbusTcpClient] = None
        self._lock = asyncio.Lock()
        self._connection_type = connection_type
        self._serial_port = serial_port
        self._baudrate = baudrate
        self._original_work_mode: Optional[int] = None
        self._original_min_soc: Optional[int] = None

        # Model detection
        if model_family:
            try:
                self._model_family = FoxESSModelFamily(model_family)
            except ValueError:
                self._model_family = FoxESSModelFamily.UNKNOWN
        else:
            self._model_family = FoxESSModelFamily.UNKNOWN

        self._register_map: Optional[FoxESSRegisterMap] = REGISTER_MAPS.get(self._model_family)

    async def connect(self) -> bool:
        """Establish Modbus connection."""
        try:
            async with self._lock:
                # Close existing connection if any
                if self._client:
                    try:
                        self._client.close()
                    except Exception:
                        pass
                    self._client = None
                    self._connected = False

                if self._connection_type == "serial" and self._serial_port:
                    from pymodbus.client import AsyncModbusSerialClient
                    self._client = AsyncModbusSerialClient(
                        port=self._serial_port,
                        baudrate=self._baudrate,
                        bytesize=8,
                        parity="N",
                        stopbits=1,
                        timeout=5,
                    )
                else:
                    self._client = AsyncModbusTcpClient(
                        host=self.host,
                        port=self.port,
                        timeout=5,
                    )

                connected = await self._client.connect()
                if connected:
                    self._connected = True
                    _LOGGER.info(
                        "FoxESS Modbus connected: %s (%s)",
                        self.host if self._connection_type == "tcp" else self._serial_port,
                        self._connection_type,
                    )
                else:
                    _LOGGER.error("FoxESS Modbus connection failed")
                return connected

        except Exception as e:
            _LOGGER.error("FoxESS connection error: %s", e)
            self._connected = False
            return False

    async def disconnect(self) -> None:
        """Close Modbus connection."""
        async with self._lock:
            if self._client:
                self._client.close()
                self._client = None
            self._connected = False

    # ---- Low-level Modbus operations ----

    async def _read_input_registers(self, address: int, count: int = 1) -> Optional[list[int]]:
        """Read input registers."""
        if not self._client or not self._connected:
            return None
        try:
            result = await self._client.read_input_registers(
                address=address, count=count, **{_SLAVE_KWARG: self.slave_id}
            )
            if result.isError():
                _LOGGER.debug("FoxESS read input register %d error: %s", address, result)
                return None
            return list(result.registers)
        except (ModbusException, Exception) as e:
            _LOGGER.debug("FoxESS read input register %d exception: %s", address, e)
            return None

    async def _read_holding_registers(self, address: int, count: int = 1) -> Optional[list[int]]:
        """Read holding registers."""
        if not self._client or not self._connected:
            return None
        try:
            result = await self._client.read_holding_registers(
                address=address, count=count, **{_SLAVE_KWARG: self.slave_id}
            )
            if result.isError():
                _LOGGER.debug("FoxESS read holding register %d error: %s", address, result)
                return None
            return list(result.registers)
        except (ModbusException, Exception) as e:
            _LOGGER.debug("FoxESS read holding register %d exception: %s", address, e)
            return None

    async def _read_data_register(self, address: int, count: int = 1) -> Optional[list[int]]:
        """Read a data register using the correct type for the detected model.

        H3-Pro and H3-Smart use holding registers for all data.
        Other models use input registers for read-only data.
        """
        if self._register_map and self._register_map.all_holding:
            return await self._read_holding_registers(address, count)
        return await self._read_input_registers(address, count)

    async def _write_holding_register(self, address: int, value: int) -> bool:
        """Write a single holding register."""
        if not self._client or not self._connected:
            return False
        try:
            result = await self._client.write_register(
                address=address, value=value, **{_SLAVE_KWARG: self.slave_id}
            )
            if result.isError():
                _LOGGER.error("FoxESS write register %d error: %s", address, result)
                return False
            return True
        except (ModbusException, Exception) as e:
            _LOGGER.error("FoxESS write register %d exception: %s", address, e)
            return False

    async def _write_holding_registers(self, address: int, values: list[int]) -> bool:
        """Write multiple holding registers."""
        if not self._client or not self._connected:
            return False
        try:
            result = await self._client.write_registers(
                address=address, values=values, **{_SLAVE_KWARG: self.slave_id}
            )
            if result.isError():
                _LOGGER.error("FoxESS write registers %d error: %s", address, result)
                return False
            return True
        except (ModbusException, Exception) as e:
            _LOGGER.error("FoxESS write registers %d exception: %s", address, e)
            return False

    @staticmethod
    def _to_signed16(value: int) -> int:
        """Convert unsigned 16-bit to signed."""
        if value >= 0x8000:
            return value - 0x10000
        return value

    @staticmethod
    def _to_signed32(high: int, low: int) -> int:
        """Convert two unsigned 16-bit registers to signed 32-bit."""
        value = (high << 16) | low
        if value >= 0x80000000:
            return value - 0x100000000
        return value

    # ---- Model detection ----

    async def _probe_register(self, address: int) -> bool:
        """Probe a register address, trying both input and holding types.

        H3-Pro and H3-Smart use holding registers for everything, while
        H1/H3/KH use input registers for read-only data. During detection
        we don't know the model yet, so try both.
        """
        result = await self._read_input_registers(address, 1)
        if result is not None:
            return True
        result = await self._read_holding_registers(address, 1)
        return result is not None

    async def detect_model(self) -> FoxESSModelFamily:
        """Auto-detect FoxESS model family by probing registers.

        Probe order: H3-Pro/Smart (37612) → H3 (31038) → H1/KH (31024).
        H1 vs KH is distinguished by feature availability.
        H3-Pro vs H3 Smart is distinguished by 41xxx register availability
        (H3 Smart's 41001-41006 are invalid per the Modbus protocol spec).

        Each probe tries both input and holding register types because
        H3-Pro/Smart use holding registers for all data while H1/H3/KH
        use input registers.

        Returns:
            Detected model family enum
        """
        # Try H3-Pro/Smart first (unique register 37612)
        if await self._probe_register(37612):
            # Both H3-Pro and H3 Smart respond on 37612.
            # Distinguish by probing holding register 41001, which is valid
            # on H3-Pro but invalid on H3 Smart (per FoxESS Modbus protocol
            # V1.05.03.00 — 41001-41006 are not specified for H3 Smart).
            h3pro_check = await self._read_holding_registers(41001, 1)
            if h3pro_check is not None:
                self._model_family = FoxESSModelFamily.H3_PRO
                _LOGGER.info("FoxESS model detected: H3-Pro (registers 37612 + 41001 responded)")
            else:
                self._model_family = FoxESSModelFamily.H3_SMART
                _LOGGER.info(
                    "FoxESS model detected: H3 Smart (register 37612 responded, "
                    "41001 invalid — native WiFi Modbus)"
                )
            self._register_map = REGISTER_MAPS[self._model_family]
            return self._model_family

        # Try H3 (register 31038)
        if await self._probe_register(31038):
            self._model_family = FoxESSModelFamily.H3
            self._register_map = REGISTER_MAPS[self._model_family]
            _LOGGER.info("FoxESS model detected: H3 (register 31038 responded)")
            return self._model_family

        # Try H1/KH (register 31024)
        if await self._probe_register(31024):
            # Try to distinguish H1 from KH by checking BMS register availability
            bms_check = await self._read_holding_registers(41009, 1)
            if bms_check is not None:
                self._model_family = FoxESSModelFamily.KH
                _LOGGER.info("FoxESS model detected: KH (holding register 41009 accessible)")
            else:
                self._model_family = FoxESSModelFamily.H1
                _LOGGER.info("FoxESS model detected: H1 (holding register 41009 not accessible)")
            self._register_map = REGISTER_MAPS[self._model_family]
            return self._model_family

        _LOGGER.warning("FoxESS model detection failed — no registers responded")
        self._model_family = FoxESSModelFamily.UNKNOWN
        return self._model_family

    # ---- Status reading ----

    async def get_status(self) -> InverterState:
        """Read current inverter/battery status."""
        if not self._register_map:
            return InverterState(
                status=InverterStatus.ERROR,
                is_curtailed=False,
                error_message="No register map — model not detected",
            )

        reg = self._register_map
        attrs: dict[str, Any] = {
            "model_family": self._model_family.value,
            "connection_type": self._connection_type,
            "host": self.host if self._connection_type == "tcp" else self._serial_port,
        }

        try:
            # Battery SOC
            soc_raw = await self._read_data_register(reg.battery_soc, 1)
            battery_soc = soc_raw[0] if soc_raw else None
            attrs["battery_soc"] = battery_soc

            # Scaling: grid CT may use a different gain than battery/PV
            # H3-Pro/H3-Smart: grid=0.0001 (gain 10000), battery/PV=0.001 (gain 1000)
            # H1/H3/KH: all use 0.001 (gain 1000)
            grid_gain = reg.power_gain
            bp_gain = reg.battery_pv_gain or reg.power_gain

            # Battery power
            if reg.battery_power_is_32bit and reg.battery_power:
                bp_raw = await self._read_holding_registers(reg.battery_power - 1, 2)
                if bp_raw and len(bp_raw) == 2:
                    battery_power_kw = self._to_signed32(bp_raw[0], bp_raw[1]) / bp_gain
                else:
                    battery_power_kw = None
            elif reg.battery_power:
                bp_raw = await self._read_data_register(reg.battery_power, 1)
                battery_power_kw = self._to_signed16(bp_raw[0]) / bp_gain if bp_raw else None
            else:
                battery_power_kw = None
            attrs["battery_power_kw"] = battery_power_kw
            attrs["battery_power_w"] = battery_power_kw * 1000 if battery_power_kw is not None else None

            # PV power
            pv1_kw = None
            pv2_kw = None
            pv1_raw = None
            pv2_raw = None
            if reg.pv_power_is_32bit and reg.pv1_power:
                pv1_raw = await self._read_holding_registers(reg.pv1_power - 1, 2)
                if pv1_raw and len(pv1_raw) == 2:
                    pv1_kw = ((pv1_raw[0] << 16) | pv1_raw[1]) / bp_gain
            elif reg.pv1_power:
                pv1_raw = await self._read_data_register(reg.pv1_power, 1)
                pv1_kw = pv1_raw[0] / bp_gain if pv1_raw else None
            if reg.pv_power_is_32bit and reg.pv2_power:
                pv2_raw = await self._read_holding_registers(reg.pv2_power - 1, 2)
                if pv2_raw and len(pv2_raw) == 2:
                    pv2_kw = ((pv2_raw[0] << 16) | pv2_raw[1]) / bp_gain
            elif reg.pv2_power:
                pv2_raw = await self._read_data_register(reg.pv2_power, 1)
                pv2_kw = pv2_raw[0] / bp_gain if pv2_raw else None
            total_pv_kw = (pv1_kw or 0) + (pv2_kw or 0)
            attrs["pv1_power_kw"] = pv1_kw
            attrs["pv2_power_kw"] = pv2_kw
            attrs["pv_power_kw"] = total_pv_kw
            attrs["pv_power_w"] = total_pv_kw * 1000

            _LOGGER.debug(
                "FoxESS PV raw: pv1_reg=%s pv1_raw=%s pv1=%.3f kW, pv2_reg=%s pv2_raw=%s pv2=%.3f kW, gain=%d, 32bit=%s",
                reg.pv1_power, list(pv1_raw) if pv1_raw else None, pv1_kw or 0,
                reg.pv2_power, list(pv2_raw) if pv2_raw else None, pv2_kw or 0,
                bp_gain, reg.pv_power_is_32bit,
            )

            # Grid power
            if reg.grid_power_is_32bit and reg.grid_power:
                gp_raw = await self._read_holding_registers(reg.grid_power - 1, 2)
                if gp_raw and len(gp_raw) == 2:
                    grid_power_kw = self._to_signed32(gp_raw[0], gp_raw[1]) / grid_gain
                else:
                    grid_power_kw = None
            elif reg.grid_power:
                gp_raw = await self._read_data_register(reg.grid_power, 1)
                grid_power_kw = self._to_signed16(gp_raw[0]) / grid_gain if gp_raw else None
            else:
                grid_power_kw = None
            # H3-Pro/H3-Smart grid CT has inverted sign (pos=export, neg=import).
            # Negate to normalize to our convention (pos=import, neg=export).
            if reg.grid_sign_inverted and grid_power_kw is not None:
                grid_power_kw = -grid_power_kw
            attrs["grid_power_kw"] = grid_power_kw

            # CT2 power (AC-coupled inverter meter)
            ct2_power_kw = 0.0
            if reg.ct2_power:
                ct2_raw = None
                if reg.ct2_power_is_32bit:
                    ct2_raw = await self._read_holding_registers(reg.ct2_power - 1, 2)
                    if ct2_raw and len(ct2_raw) == 2:
                        ct2_power_kw = self._to_signed32(ct2_raw[0], ct2_raw[1]) / grid_gain
                    else:
                        _LOGGER.debug("FoxESS CT2 read failed: reg=%d, raw=%s", reg.ct2_power, ct2_raw)
                else:
                    ct2_raw = await self._read_data_register(reg.ct2_power, 1)
                    if ct2_raw:
                        ct2_power_kw = self._to_signed16(ct2_raw[0]) / grid_gain
                _LOGGER.debug("FoxESS CT2 raw: reg=%d raw=%s ct2=%.3f kW, gain=%d, 32bit=%s",
                              reg.ct2_power, list(ct2_raw) if ct2_raw else None, ct2_power_kw,
                              grid_gain, reg.ct2_power_is_32bit)
                # CT2 measures AC-coupled inverter generation — positive = generating.
                # Do NOT apply grid_sign_inverted: grid CT sign is about import/export
                # direction, but CT2 is unidirectional generation measurement.
            attrs["ct2_power_kw"] = ct2_power_kw

            # Load/home power
            if reg.load_power:
                lp_raw = await self._read_data_register(reg.load_power, 1)
                load_power_kw = lp_raw[0] / bp_gain if lp_raw else None
            else:
                # H3-Pro/H3-Smart: no load register, calculate from energy balance
                # Sign convention: battery positive=discharge, grid positive=import
                # Load = PV_DC + CT2_AC + battery_discharge + grid_import
                if grid_power_kw is not None and battery_power_kw is not None:
                    load_power_kw = total_pv_kw + ct2_power_kw + grid_power_kw + battery_power_kw
                else:
                    load_power_kw = None
            attrs["load_power_kw"] = load_power_kw

            # Work mode (holding register)
            if reg.work_mode and reg.supports_work_mode_rw:
                wm_raw = await self._read_holding_registers(reg.work_mode, 1)
                work_mode = wm_raw[0] if wm_raw else None
            else:
                work_mode = None
            attrs["work_mode"] = work_mode
            wm_names = reg.get_work_mode_names()
            attrs["work_mode_name"] = wm_names.get(work_mode, f"Unknown ({work_mode})") if work_mode is not None else None

            # Min SOC / backup reserve
            if reg.min_soc and reg.supports_work_mode_rw:
                ms_raw = await self._read_holding_registers(reg.min_soc, 1)
                min_soc = ms_raw[0] if ms_raw else None
            else:
                min_soc = None
            attrs["min_soc"] = min_soc

            # Charge/discharge current limits
            if reg.max_charge_current and reg.supports_work_mode_rw:
                mc_raw = await self._read_holding_registers(reg.max_charge_current, 1)
                max_charge_a = mc_raw[0] / GAIN_CURRENT if mc_raw else None
            else:
                max_charge_a = None
            attrs["max_charge_current_a"] = max_charge_a

            if reg.max_discharge_current and reg.supports_work_mode_rw:
                md_raw = await self._read_holding_registers(reg.max_discharge_current, 1)
                max_discharge_a = md_raw[0] / GAIN_CURRENT if md_raw else None
            else:
                max_discharge_a = None
            attrs["max_discharge_current_a"] = max_discharge_a

            is_curtailed = False  # Determined by export limit state if tracked

            return InverterState(
                status=InverterStatus.ONLINE,
                is_curtailed=is_curtailed,
                power_output_w=total_pv_kw * 1000 if total_pv_kw else 0,
                power_limit_percent=100,
                attributes=attrs,
            )

        except Exception as e:
            _LOGGER.error("FoxESS get_status error: %s", e, exc_info=True)
            return InverterState(
                status=InverterStatus.ERROR,
                is_curtailed=False,
                error_message=str(e),
            )

    # ---- Battery control methods ----

    async def set_work_mode(self, mode: int) -> bool:
        """Set FoxESS work mode.

        Args:
            mode: Model-specific work mode register value.
                  H1/H3/KH (reg 41000): 0=Self Use, 1=Feed-in, 2=Backup
                  H3-Pro/Smart (reg 49203): 1=Self Use, 2=Feed-in, 3=Backup
        """
        if not self._register_map or not self._register_map.work_mode:
            _LOGGER.error("Work mode register not available for model %s", self._model_family.value)
            return False

        wm_names = self._register_map.get_work_mode_names()
        _LOGGER.info("FoxESS setting work mode to %d (%s)", mode, wm_names.get(mode, f"Unknown ({mode})"))

        return await self._write_holding_register(self._register_map.work_mode, mode)

    async def _write_remote_control(self, reg: 'FoxESSRegisterMap', power_val: int,
                                    duration_minutes: int, timeout_seconds: int,
                                    label: str) -> bool:
        """Write remote control registers and verify they took effect.

        Writes remote_enable, remote_timeout, and remote_active_power, then
        reads back remote_enable to confirm. Retries once on verification
        failure (covers silent Modbus collisions from concurrent integrations).
        """
        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            # Enable remote control
            if reg.remote_enable:
                await self._write_holding_register(reg.remote_enable, 1)
                if reg.remote_timeout:
                    await self._write_holding_register(reg.remote_timeout, timeout_seconds)

            # Write active power
            write_val = power_val
            if reg.remote_active_power_is_32bit:
                if write_val < 0:
                    write_val = write_val + 0x100000000
                high = (write_val >> 16) & 0xFFFF
                low = write_val & 0xFFFF
                success = await self._write_holding_registers(reg.remote_active_power, [high, low])
            else:
                raw = write_val & 0xFFFF
                success = await self._write_holding_register(reg.remote_active_power, raw)

            if not success:
                _LOGGER.warning("FoxESS %s write failed on attempt %d/%d", label, attempt, max_attempts)
                if attempt < max_attempts:
                    await asyncio.sleep(1)
                    continue
                return False

            # Verify: read back remote_enable to confirm the inverter accepted it
            if reg.remote_enable:
                await asyncio.sleep(0.5)
                verify = await self._read_holding_registers(reg.remote_enable, 1)
                if verify and verify[0] == 1:
                    _LOGGER.info(
                        "FoxESS %s activated for %d minutes (timeout %ds)%s",
                        label, duration_minutes, timeout_seconds,
                        "" if attempt == 1 else f" (attempt {attempt})",
                    )
                    return True
                else:
                    _LOGGER.warning(
                        "FoxESS %s verify failed: remote_enable=%s (attempt %d/%d)",
                        label, verify, attempt, max_attempts,
                    )
                    if attempt < max_attempts:
                        await asyncio.sleep(1)
                        continue
                    return False
            else:
                # No remote_enable register to verify — trust the write
                _LOGGER.info("FoxESS %s activated for %d minutes (timeout %ds)", label, duration_minutes, timeout_seconds)
                return True

        return False

    async def force_charge(self, duration_minutes: int = 60, power_w: float = 5000) -> bool:
        """Force battery to charge from grid via remote control registers."""
        if not self._register_map:
            return False

        reg = self._register_map

        # Save current work mode for restore
        if reg.work_mode and reg.supports_work_mode_rw:
            wm_raw = await self._read_holding_registers(reg.work_mode, 1)
            if wm_raw:
                self._original_work_mode = wm_raw[0]

        # Save current min_soc for restore
        if reg.min_soc and reg.supports_work_mode_rw:
            ms_raw = await self._read_holding_registers(reg.min_soc, 1)
            if ms_raw:
                self._original_min_soc = ms_raw[0]

        # Set work mode to Backup before enabling remote control — the inverter
        # requires a compatible work mode for remote control to take effect
        if reg.work_mode and reg.supports_work_mode_rw:
            await self.set_work_mode(reg.work_mode_backup)

        timeout_seconds = max(duration_minutes * 60, 600)
        power_val = -int(abs(power_w))
        return await self._write_remote_control(reg, power_val, duration_minutes, timeout_seconds, "force charge")

    async def force_discharge(self, duration_minutes: int = 60, power_w: float = 5000) -> bool:
        """Force battery to discharge/export via remote control registers."""
        if not self._register_map:
            return False

        reg = self._register_map

        # Save current work mode for restore
        if reg.work_mode and reg.supports_work_mode_rw:
            wm_raw = await self._read_holding_registers(reg.work_mode, 1)
            if wm_raw:
                self._original_work_mode = wm_raw[0]

        # Save current min_soc for restore
        if reg.min_soc and reg.supports_work_mode_rw:
            ms_raw = await self._read_holding_registers(reg.min_soc, 1)
            if ms_raw:
                self._original_min_soc = ms_raw[0]

        # Set work mode to Feed-in before enabling remote control — the inverter
        # requires a compatible work mode for remote control to take effect
        if reg.work_mode and reg.supports_work_mode_rw:
            await self.set_work_mode(reg.work_mode_feed_in)

        timeout_seconds = max(duration_minutes * 60, 600)
        power_val = int(abs(power_w))
        return await self._write_remote_control(reg, power_val, duration_minutes, timeout_seconds, "force discharge")

    async def restore_normal(self) -> bool:
        """Restore normal operation (Self Use mode)."""
        if not self._register_map:
            return False

        reg = self._register_map

        # Restore to saved mode or default Self Use (model-specific value)
        target_mode = self._original_work_mode if self._original_work_mode is not None else reg.work_mode_self_use
        success = await self.set_work_mode(target_mode)

        # Restore original min_soc if saved
        if self._original_min_soc is not None and self._register_map.min_soc:
            await self._write_holding_register(self._register_map.min_soc, self._original_min_soc)
            self._original_min_soc = None

        # Disable remote control override
        if self._register_map.remote_enable:
            await self._write_holding_register(self._register_map.remote_enable, 0)

        self._original_work_mode = None

        if success:
            _LOGGER.info("FoxESS restored to normal operation (mode %d)", target_mode)

        return success

    async def set_backup_mode(self) -> bool:
        """Set FoxESS to Backup mode for IDLE (prevents self-consumption discharge).

        In Self Use mode, min_soc is only a passive floor — the battery still
        discharges to serve home load until it reaches min_soc. In Backup mode,
        the battery does NOT discharge for self-consumption at all (only for
        grid outages), making it the FoxESS equivalent of Tesla's autonomous
        mode for holding SOC.

        Disables remote control first (if active from a prior force_charge/
        force_discharge), then saves current state for later restoration.
        """
        if not self._register_map:
            return False

        reg = self._register_map

        # Disable remote control if previously enabled — the inverter may
        # reject work_mode and min_soc writes while remote control is active.
        # Matches nathanmarlor/foxess_modbus: _disable_remote_control(BACK_UP).
        if reg.remote_enable:
            await self._write_holding_register(reg.remote_enable, 0)

        # Save current work mode for restore (only on first call, not re-entry)
        if self._original_work_mode is None and reg.work_mode and reg.supports_work_mode_rw:
            wm_raw = await self._read_holding_registers(reg.work_mode, 1)
            if wm_raw:
                self._original_work_mode = wm_raw[0]

        # Save current min_soc for restore (only on first call)
        if self._original_min_soc is None and reg.min_soc and reg.supports_work_mode_rw:
            ms_raw = await self._read_holding_registers(reg.min_soc, 1)
            if ms_raw:
                self._original_min_soc = ms_raw[0]

        success = await self.set_work_mode(reg.work_mode_backup)
        if success:
            _LOGGER.info("FoxESS set to Backup mode (IDLE hold, remote control disabled)")
        return success

    async def restore_work_mode_from_idle(self) -> bool:
        """Restore work mode to Self Use after IDLE Backup mode.

        Unlike restore_normal(), this only changes work mode and does not
        touch remote control registers. The optimizer manages min_soc
        separately via set_backup_reserve.
        """
        if not self._register_map:
            return False

        target_mode = (
            self._original_work_mode
            if self._original_work_mode is not None
            else self._register_map.work_mode_self_use
        )
        success = await self.set_work_mode(target_mode)

        # Clear saved state
        self._original_work_mode = None
        self._original_min_soc = None

        if success:
            _LOGGER.info("FoxESS restored from IDLE Backup mode (mode %d)", target_mode)
        return success

    async def set_backup_reserve(self, percent: int) -> bool:
        """Set minimum SOC (backup reserve).

        Args:
            percent: Minimum SOC percentage (0-100)
        """
        if not self._register_map or not self._register_map.min_soc:
            _LOGGER.error("Min SOC register not available for model %s", self._model_family.value)
            return False

        percent = max(0, min(100, percent))
        _LOGGER.info("FoxESS setting min SOC to %d%%", percent)
        return await self._write_holding_register(self._register_map.min_soc, percent)

    async def get_backup_reserve(self) -> Optional[int]:
        """Read current minimum SOC (backup reserve)."""
        if not self._register_map or not self._register_map.min_soc:
            return None

        result = await self._read_holding_registers(self._register_map.min_soc, 1)
        return result[0] if result else None

    async def set_charge_rate_limit(self, amps: float) -> bool:
        """Set maximum charge current.

        Args:
            amps: Maximum charge current in amps
        """
        if not self._register_map or not self._register_map.max_charge_current:
            return False

        raw_value = int(amps * GAIN_CURRENT)
        _LOGGER.info("FoxESS setting max charge current to %.1f A (raw=%d)", amps, raw_value)
        return await self._write_holding_register(self._register_map.max_charge_current, raw_value)

    async def set_discharge_rate_limit(self, amps: float) -> bool:
        """Set maximum discharge current.

        Args:
            amps: Maximum discharge current in amps
        """
        if not self._register_map or not self._register_map.max_discharge_current:
            return False

        raw_value = int(amps * GAIN_CURRENT)
        _LOGGER.info("FoxESS setting max discharge current to %.1f A (raw=%d)", amps, raw_value)
        return await self._write_holding_register(self._register_map.max_discharge_current, raw_value)

    # ---- Solar curtailment ----

    async def curtail(
        self,
        home_load_w: Optional[float] = None,
        rated_capacity_w: Optional[float] = None,
    ) -> bool:
        """Curtail solar export by enabling remote control with limited export power.

        If home_load_w is provided, limits export to match home load (load-following).
        Otherwise sets remote active power to 0 (zero export).
        """
        if not self._register_map:
            return False

        reg = self._register_map

        # Enable remote control
        if reg.remote_enable:
            await self._write_holding_register(reg.remote_enable, 1)
            if reg.remote_timeout:
                await self._write_holding_register(reg.remote_timeout, 600)

        # Set remote active power
        power_w = int(home_load_w) if home_load_w is not None and home_load_w > 0 else 0

        if reg.remote_active_power:
            if reg.remote_active_power_is_32bit:
                high = (power_w >> 16) & 0xFFFF
                low = power_w & 0xFFFF
                await self._write_holding_registers(reg.remote_active_power, [high, low])
            else:
                await self._write_holding_register(reg.remote_active_power, power_w)

        if power_w > 0:
            _LOGGER.info(f"FoxESS solar export curtailed (load-following: {power_w}W)")
        else:
            _LOGGER.info("FoxESS solar export curtailed (remote power = 0)")
        return True

    async def restore(self) -> bool:
        """Restore normal solar export by disabling remote control."""
        if not self._register_map:
            return False

        # Disable remote control — inverter returns to normal autonomous operation
        if self._register_map.remote_enable:
            await self._write_holding_register(self._register_map.remote_enable, 0)

        _LOGGER.info("FoxESS solar export restored (remote control disabled)")
        return True

    # ---- Energy summary ----

    async def get_energy_summary(self) -> Optional[dict[str, float]]:
        """Read daily energy totals."""
        if not self._register_map or not self._register_map.supports_energy_totals:
            return None

        reg = self._register_map
        gain = reg.energy_gain  # Per-model gain (10 for H1/H3/KH, 100 for H3-Pro/Smart)
        result: dict[str, float] = {}

        if reg.charge_energy_today:
            if reg.energy_is_32bit:
                # 32-bit: low word at register address, high word at address-1
                raw = await self._read_holding_registers(reg.charge_energy_today - 1, 2)
                if raw and len(raw) == 2:
                    val = (raw[0] << 16) | raw[1]  # unsigned 32-bit
                    result["charge_today_kwh"] = val / gain
            else:
                raw = await self._read_data_register(reg.charge_energy_today, 1)
                if raw:
                    result["charge_today_kwh"] = raw[0] / gain

        if reg.discharge_energy_today:
            if reg.energy_is_32bit:
                raw = await self._read_holding_registers(reg.discharge_energy_today - 1, 2)
                if raw and len(raw) == 2:
                    val = (raw[0] << 16) | raw[1]  # unsigned 32-bit
                    result["discharge_today_kwh"] = val / gain
            else:
                raw = await self._read_data_register(reg.discharge_energy_today, 1)
                if raw:
                    result["discharge_today_kwh"] = raw[0] / gain

        _LOGGER.debug("FoxESS energy summary: %s (gain=%d, 32bit=%s)", result, gain, reg.energy_is_32bit)
        return result if result else None

    # ---- Battery data for connection test ----

    async def get_battery_data(self) -> Optional[dict[str, Any]]:
        """Read battery SOC (used for connection testing)."""
        if not self._register_map:
            # Try to detect model first
            await self.detect_model()

        if not self._register_map:
            return None

        soc_raw = await self._read_data_register(self._register_map.battery_soc, 1)
        if soc_raw is not None:
            return {
                "battery_soc": soc_raw[0],
                "model_family": self._model_family.value,
            }
        return None

    # ---- Async context manager ----

    async def __aenter__(self):
        """Enter async context."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit async context."""
        await self.disconnect()
