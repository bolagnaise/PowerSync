"""Sigenergy EV charger control via Modbus TCP."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Optional

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException
import pymodbus

from .sigenergy_model import normalize_evdc_power_kw

_LOGGER = logging.getLogger(__name__)

try:
    _pymodbus_version = tuple(int(x) for x in pymodbus.__version__.split(".")[:2])
    _SLAVE_PARAM = "device_id" if _pymodbus_version >= (3, 9) else "slave"
except Exception:
    _SLAVE_PARAM = "slave"


SIGENERGY_CHARGER_EVAC = "evac"
SIGENERGY_CHARGER_EVDC = "evdc"


@dataclass(frozen=True)
class SigenergyChargerState:
    """Normalized Sigenergy EV charger state."""

    charger_type: str
    raw_state: int | None = None
    status: str = "unknown"
    is_connected: bool = False
    is_charging: bool = False
    is_discharging: bool = False
    power_kw: float | None = None
    energy_kwh: float | None = None
    current_a: float | None = None
    rated_current_a: float | None = None
    vehicle_soc: float | None = None


@dataclass(frozen=True)
class SigenergyChargerCapabilities:
    """Runtime control capabilities for a Sigenergy EV charger."""

    charger_type: str
    supports_start_stop: bool = True
    supports_rate_control: bool = False
    supports_restart_while_plugged: bool = False
    control_strategy: str = "one_shot"
    solar_control_strategy: str = "native_handoff"

    def as_dict(self) -> dict:
        """Return an API-friendly capability payload."""
        return {
            "charger_type": self.charger_type,
            "supports_start_stop": self.supports_start_stop,
            "supports_rate_control": self.supports_rate_control,
            "supports_restart_while_plugged": self.supports_restart_while_plugged,
            "control_strategy": self.control_strategy,
            "solar_control_strategy": self.solar_control_strategy,
        }


def sigenergy_charger_capabilities(
    charger_type: str | None,
) -> SigenergyChargerCapabilities:
    """Return conservative capability flags for a Sigenergy EV charger."""
    normalized = str(charger_type or SIGENERGY_CHARGER_EVAC).lower()
    if normalized == SIGENERGY_CHARGER_EVDC:
        return SigenergyChargerCapabilities(charger_type=SIGENERGY_CHARGER_EVDC)
    return SigenergyChargerCapabilities(
        charger_type=SIGENERGY_CHARGER_EVAC,
        supports_rate_control=True,
        supports_restart_while_plugged=True,
        control_strategy="dynamic_rate",
        solar_control_strategy="dynamic_rate",
    )


def sigenergy_charger_display_name(charger_type: str | None) -> str:
    """Return the user-facing charger name."""
    normalized = str(charger_type or SIGENERGY_CHARGER_EVAC).lower()
    if normalized == SIGENERGY_CHARGER_EVDC:
        return "Sigenergy EVDC"
    return "Sigenergy EVAC"


def _capabilities_payload(
    charger_type: str | None,
    capabilities: SigenergyChargerCapabilities | dict | None = None,
) -> dict:
    """Return capability payload, allowing config-aware callers to override defaults."""
    if capabilities is None:
        return sigenergy_charger_capabilities(charger_type).as_dict()
    if isinstance(capabilities, SigenergyChargerCapabilities):
        return capabilities.as_dict()
    return dict(capabilities)


def sigenergy_charger_charging_state(state: SigenergyChargerState) -> str:
    """Map normalized Modbus state to the app's EV charging-state labels."""
    if state.is_discharging:
        return "Discharging"
    if state.is_charging:
        return "Charging"
    if state.is_connected:
        return "Stopped"
    if state.status in ("fault", "alarm", "unavailable"):
        return state.status.capitalize()
    return "Disconnected"


def sigenergy_charger_state_to_vehicle(
    state: SigenergyChargerState,
    *,
    updated_at: str,
    online: bool = True,
    capabilities: SigenergyChargerCapabilities | dict | None = None,
) -> dict:
    """Convert Sigenergy charger telemetry to the mobile app vehicle shape."""
    capability_payload = _capabilities_payload(state.charger_type, capabilities)
    return {
        "id": "sigenergy_charger",
        "vehicle_id": "sigenergy_charger",
        "vin": None,
        "display_name": sigenergy_charger_display_name(state.charger_type),
        "model": state.charger_type.upper(),
        "battery_level": int(state.vehicle_soc) if state.vehicle_soc is not None else None,
        "charging_state": sigenergy_charger_charging_state(state),
        "charge_limit_soc": None,
        "is_plugged_in": state.is_connected,
        "charger_power": state.power_kw if state.power_kw is not None else 0.0,
        "is_discharging": state.is_discharging,
        "is_online": online,
        "data_updated_at": updated_at,
        "source": "sigenergy_charger",
        "brand": "sigenergy",
        "supports_rate_control": capability_payload.get("supports_rate_control", False),
        "supports_restart_while_plugged": capability_payload.get(
            "supports_restart_while_plugged",
            False,
        ),
        "control_strategy": capability_payload.get("control_strategy", "one_shot"),
        "solar_control_strategy": capability_payload.get(
            "solar_control_strategy",
            "native_handoff",
        ),
        "charger_capabilities": capability_payload,
    }


def sigenergy_charger_state_to_loadpoint_observation(
    state: SigenergyChargerState,
    *,
    capabilities: SigenergyChargerCapabilities | dict | None = None,
) -> dict:
    """Convert Sigenergy charger telemetry to a normalized loadpoint observation."""
    power_kw = state.power_kw or 0.0
    capability_payload = _capabilities_payload(state.charger_type, capabilities)
    is_charging = state.is_charging or power_kw > 0.05
    is_discharging = state.is_discharging or power_kw < -0.05
    return {
        "charger_id": "sigenergy_charger",
        "vehicle_id": "sigenergy_charger",
        "vehicle_name": sigenergy_charger_display_name(state.charger_type),
        "charger_type": "sigenergy",
        "ev_power_kw": power_kw,
        "ev_soc": int(state.vehicle_soc) if state.vehicle_soc is not None else None,
        "is_connected": state.is_connected,
        "is_charging": is_charging,
        "is_discharging": is_discharging,
        "current_amps": int(state.current_a or 0),
        "target_amps": int(state.current_a or 0),
        "blocking_reason": None if is_charging or is_discharging else state.status,
        "include_idle": True,
        "supports_rate_control": capability_payload.get("supports_rate_control", False),
        "supports_restart_while_plugged": capability_payload.get(
            "supports_restart_while_plugged",
            False,
        ),
        "control_strategy": capability_payload.get("control_strategy", "one_shot"),
        "solar_control_strategy": capability_payload.get(
            "solar_control_strategy",
            "native_handoff",
        ),
        "charger_capabilities": capability_payload,
    }


def sigenergy_charger_state_to_widget(
    state: SigenergyChargerState,
    *,
    surplus_kw: float = 0.0,
    capabilities: SigenergyChargerCapabilities | dict | None = None,
) -> dict:
    """Convert Sigenergy charger telemetry to the EV dashboard widget shape."""
    power_kw = state.power_kw or 0.0
    capability_payload = _capabilities_payload(state.charger_type, capabilities)
    source = "idle"
    is_charging = state.is_charging or power_kw > 0.05
    is_discharging = state.is_discharging or power_kw < -0.05
    if is_discharging:
        source = "v2x"
    elif is_charging:
        source = "solar" if surplus_kw >= power_kw * 0.8 else "grid"

    return {
        "vehicle_name": sigenergy_charger_display_name(state.charger_type),
        "is_charging": is_charging,
        "is_discharging": is_discharging,
        "is_connected": state.is_connected,
        "current_soc": int(state.vehicle_soc or 0),
        "target_soc": 80,
        "current_power_kw": round(power_kw, 2),
        "source": source,
        "eta_minutes": None,
        "surplus_kw": round(surplus_kw, 2),
        "supports_rate_control": capability_payload.get("supports_rate_control", False),
        "supports_restart_while_plugged": capability_payload.get(
            "supports_restart_while_plugged",
            False,
        ),
        "control_strategy": capability_payload.get("control_strategy", "one_shot"),
        "solar_control_strategy": capability_payload.get(
            "solar_control_strategy",
            "native_handoff",
        ),
    }


class SigenergyEVChargerController:
    """Control Sigenergy EVAC/EVDC chargers through the local Modbus protocol."""

    DEFAULT_PORT = 502
    DEFAULT_SLAVE_ID = 1
    TIMEOUT_SECONDS = 10.0

    EVAC_MIN_CURRENT_A = 6

    # EVAC input registers, valid only on the AC charger's slave address.
    REG_EVAC_SYSTEM_STATE = 32000
    REG_EVAC_TOTAL_ENERGY = 32001
    REG_EVAC_CHARGING_POWER = 32003
    REG_EVAC_RATED_CURRENT = 32007
    REG_EVAC_INPUT_BREAKER_CURRENT = 32010

    # EVAC holding registers.
    REG_EVAC_START_STOP = 42000
    REG_EVAC_OUTPUT_CURRENT = 42001
    EVAC_COMMAND_START = 0
    EVAC_COMMAND_STOP = 1

    # EVDC registers live on the hybrid inverter slave address.
    REG_EVDC_VEHICLE_VOLTAGE = 31500
    REG_EVDC_CHARGING_CURRENT = 31501
    REG_EVDC_OUTPUT_POWER = 31502
    REG_EVDC_VEHICLE_SOC = 31504
    REG_EVDC_CURRENT_CHARGING_ENERGY = 31505
    REG_EVDC_RUNNING_STATE = 31513

    REG_EVDC_START_STOP = 41000
    EVDC_COMMAND_START = 0
    EVDC_COMMAND_STOP = 1

    EVAC_STATES = {
        0x00: "initializing",
        0x01: "a1_a2",
        0x02: "b1",
        0x03: "b2",
        0x04: "c1",
        0x05: "c2",
        0x06: "f",
        0x07: "e",
    }
    EVAC_CONNECTED_STATES = {0x02, 0x03, 0x04, 0x05}
    EVAC_CHARGING_STATES = {0x04, 0x05}

    EVDC_STATES = {
        0x00: "idle",
        0x01: "occupied",
        0x02: "preparing",
        0x03: "charging",
        0x04: "fault",
        0x05: "scheduled",
        0x06: "ended",
        0x07: "unavailable",
        0x08: "discharging",
        0x09: "alarm",
        0x0A: "preparing_insulation_detection",
    }
    EVDC_CONNECTED_STATES = {0x01, 0x02, 0x03, 0x05, 0x08, 0x0A}
    EVDC_CHARGING_STATES = {0x03}

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        slave_id: int = DEFAULT_SLAVE_ID,
        charger_type: str = SIGENERGY_CHARGER_EVAC,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.slave_id = int(slave_id)
        self.charger_type = str(charger_type or SIGENERGY_CHARGER_EVAC).lower()
        self._client: Optional[AsyncModbusTcpClient] = None
        self._connected = False
        self._lock = asyncio.Lock()

    async def connect(self) -> bool:
        """Connect to the charger Modbus endpoint."""
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
                self._connected = bool(connected)
                if connected:
                    _LOGGER.info(
                        "Connected to Sigenergy %s charger at %s:%s slave=%s",
                        self.charger_type.upper(),
                        self.host,
                        self.port,
                        self.slave_id,
                    )
                else:
                    _LOGGER.error(
                        "Failed to connect to Sigenergy %s charger at %s:%s",
                        self.charger_type.upper(),
                        self.host,
                        self.port,
                    )
                return bool(connected)
            except Exception as err:
                self._connected = False
                _LOGGER.error("Error connecting to Sigenergy charger: %s", err)
                return False

    async def disconnect(self) -> None:
        """Disconnect from the Modbus endpoint."""
        async with self._lock:
            if self._client:
                self._client.close()
                self._client = None
            self._connected = False

    async def start_charging(self, amps: int | None = None) -> bool:
        """Start charging, optionally applying an EVAC current limit first."""
        if self.charger_type == SIGENERGY_CHARGER_EVAC:
            if amps is not None and not await self.set_charging_amps(amps):
                return False
            return await self._write_holding_registers(
                self.REG_EVAC_START_STOP,
                [self.EVAC_COMMAND_START],
            )

        if self.charger_type == SIGENERGY_CHARGER_EVDC:
            if amps is not None:
                _LOGGER.debug(
                    "Sigenergy EVDC current limit is not supported by protocol v2.8; starting without setting amps"
                )
            return await self._write_holding_registers(
                self.REG_EVDC_START_STOP,
                [self.EVDC_COMMAND_START],
            )

        _LOGGER.error("Unsupported Sigenergy charger type: %s", self.charger_type)
        return False

    async def stop_charging(self) -> bool:
        """Stop charging."""
        if self.charger_type == SIGENERGY_CHARGER_EVAC:
            return await self._write_holding_registers(
                self.REG_EVAC_START_STOP,
                [self.EVAC_COMMAND_STOP],
            )
        if self.charger_type == SIGENERGY_CHARGER_EVDC:
            return await self._write_holding_registers(
                self.REG_EVDC_START_STOP,
                [self.EVDC_COMMAND_STOP],
            )
        _LOGGER.error("Unsupported Sigenergy charger type: %s", self.charger_type)
        return False

    async def set_charging_amps(self, amps: int) -> bool:
        """Set the EVAC charger output current."""
        if self.charger_type != SIGENERGY_CHARGER_EVAC:
            _LOGGER.error("Sigenergy %s charger does not expose writable amps", self.charger_type.upper())
            return False

        target_amps = int(amps)
        if target_amps < self.EVAC_MIN_CURRENT_A:
            _LOGGER.error(
                "Sigenergy EVAC current %sA is below protocol minimum %sA",
                target_amps,
                self.EVAC_MIN_CURRENT_A,
            )
            return False

        max_current = await self._read_evac_current_limit()
        if max_current is not None and target_amps > max_current:
            _LOGGER.warning(
                "Clamping Sigenergy EVAC current from %sA to %.0fA",
                target_amps,
                max_current,
            )
            target_amps = int(max_current)

        scaled = target_amps * 100
        return await self._write_holding_registers(
            self.REG_EVAC_OUTPUT_CURRENT,
            self._from_unsigned32(scaled),
        )

    async def read_state(self) -> SigenergyChargerState | None:
        """Read normalized charger state."""
        if self.charger_type == SIGENERGY_CHARGER_EVAC:
            return await self._read_evac_state()
        if self.charger_type == SIGENERGY_CHARGER_EVDC:
            return await self._read_evdc_state()
        _LOGGER.error("Unsupported Sigenergy charger type: %s", self.charger_type)
        return None

    async def _read_evac_state(self) -> SigenergyChargerState | None:
        regs = await self._read_input_registers(self.REG_EVAC_SYSTEM_STATE, 12)
        if not regs:
            return None

        raw_state = regs[0]
        power_kw = self._to_signed32(regs[3], regs[4]) / 1000 if len(regs) >= 5 else None
        energy_kwh = self._to_unsigned32(regs[1], regs[2]) / 100 if len(regs) >= 3 else None
        rated_current = self._to_signed32(regs[7], regs[8]) / 100 if len(regs) >= 9 else None
        breaker_current = self._to_signed32(regs[10], regs[11]) / 100 if len(regs) >= 12 else None
        current_limit = min(
            value for value in (rated_current, breaker_current) if value and value > 0
        ) if any(value and value > 0 for value in (rated_current, breaker_current)) else None

        return SigenergyChargerState(
            charger_type=SIGENERGY_CHARGER_EVAC,
            raw_state=raw_state,
            status=self.EVAC_STATES.get(raw_state, "unknown"),
            is_connected=raw_state in self.EVAC_CONNECTED_STATES,
            is_charging=raw_state in self.EVAC_CHARGING_STATES or bool(power_kw and power_kw > 0.05),
            power_kw=power_kw,
            energy_kwh=energy_kwh,
            rated_current_a=current_limit,
        )

    async def _read_evdc_state(self) -> SigenergyChargerState | None:
        regs = await self._read_input_registers(self.REG_EVDC_VEHICLE_VOLTAGE, 14)
        if not regs:
            return None

        raw_state = regs[13] if len(regs) >= 14 else None
        power_kw = self._to_signed32(regs[2], regs[3]) / 1000 if len(regs) >= 4 else None
        power_kw = normalize_evdc_power_kw(power_kw, raw_state=raw_state)
        is_discharging = (
            raw_state == 0x08 if raw_state is not None else False
        ) or bool(power_kw and power_kw < -0.05)

        return SigenergyChargerState(
            charger_type=SIGENERGY_CHARGER_EVDC,
            raw_state=raw_state,
            status=self.EVDC_STATES.get(raw_state, "unknown") if raw_state is not None else "unknown",
            is_connected=raw_state in self.EVDC_CONNECTED_STATES if raw_state is not None else False,
            is_charging=(raw_state in self.EVDC_CHARGING_STATES if raw_state is not None else False)
            or bool(power_kw and power_kw > 0.05),
            is_discharging=is_discharging,
            power_kw=power_kw,
            energy_kwh=self._to_unsigned32(regs[5], regs[6]) / 100 if len(regs) >= 7 else None,
            current_a=regs[1] / 10 if len(regs) >= 2 else None,
            vehicle_soc=regs[4] / 10 if len(regs) >= 5 else None,
        )

    async def _read_evac_current_limit(self) -> float | None:
        regs = await self._read_input_registers(self.REG_EVAC_RATED_CURRENT, 5)
        if not regs or len(regs) < 5:
            return None

        rated_current = self._to_signed32(regs[0], regs[1]) / 100
        breaker_current = self._to_signed32(regs[3], regs[4]) / 100
        candidates = [value for value in (rated_current, breaker_current) if value > 0]
        return min(candidates) if candidates else None

    async def _write_holding_registers(self, address: int, values: list[int]) -> bool:
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
                _LOGGER.error(
                    "Sigenergy charger Modbus write error at %s [slave=%s]: %s",
                    address,
                    self.slave_id,
                    result,
                )
                return False
            return True
        except ModbusException as err:
            _LOGGER.error("Sigenergy charger Modbus exception writing %s: %s", address, err)
            return False
        except Exception as err:
            _LOGGER.error("Error writing Sigenergy charger register %s: %s", address, err)
            return False

    async def _read_input_registers(self, address: int, count: int) -> Optional[list[int]]:
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
                _LOGGER.debug(
                    "Sigenergy charger Modbus read error at %s [slave=%s]: %s",
                    address,
                    self.slave_id,
                    result,
                )
                return None
            return result.registers
        except ModbusException as err:
            _LOGGER.debug("Sigenergy charger Modbus exception reading %s: %s", address, err)
            return None
        except Exception as err:
            _LOGGER.debug("Error reading Sigenergy charger register %s: %s", address, err)
            return None

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
    def _from_unsigned32(value: int) -> list[int]:
        return [(value >> 16) & 0xFFFF, value & 0xFFFF]
