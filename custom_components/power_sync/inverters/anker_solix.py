"""Anker Solix battery controllers.

Supports the official local X1/Solarbank Modbus map directly and bridges the
official/unofficial Anker Solix Home Assistant integrations through their
entities when PowerSync is not the Modbus owner.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.helpers import entity_registry as er

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException
import pymodbus

_LOGGER = logging.getLogger(__name__)

try:
    _pymodbus_version = tuple(int(x) for x in pymodbus.__version__.split(".")[:2])
    _SLAVE_PARAM = "device_id" if _pymodbus_version >= (3, 9) else "slave"
except Exception:
    _SLAVE_PARAM = "slave"


_OFFICIAL_DOMAIN = "anker_solix_official"
_CLOUD_DOMAIN = "anker_solix"

_MODE_SELF_CONSUMPTION = 0
_MODE_THIRD_PARTY_CONTROL = 3


class AnkerSolixX1ModbusController:
    """Direct controller for Anker Solix X1/Solarbank local Modbus."""

    REG_BATTERY_STATUS = 10001
    REG_PV_POWER = 10002
    REG_THIRD_PARTY_PV_POWER = 10004
    REG_BATTERY_POWER = 10008
    REG_LOAD_POWER = 10010
    REG_GRID_POWER = 10012
    REG_BATTERY_SOC = 10014
    REG_PV_TOTAL_GENERATION = 10018
    REG_MAX_CHARGE_POWER = 10036
    REG_MAX_DISCHARGE_POWER = 10038
    REG_OPERATING_MODE = 10064
    REG_BATTERY_POWER_SETPOINT = 10071
    REG_RATED_ENERGY = 10250
    REG_EMS_MODE_MASK = 32774

    DEFAULT_TIMEOUT_SECONDS = 10.0

    def __init__(
        self,
        host: str,
        port: int = 502,
        slave_id: int = 1,
        *,
        battery_capacity_kwh: float | None = None,
        max_charge_kw: float = 5.0,
        max_discharge_kw: float = 5.0,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.slave_id = int(slave_id)
        self._configured_capacity_kwh = battery_capacity_kwh
        self._configured_max_charge_w = float(max_charge_kw) * 1000.0
        self._configured_max_discharge_w = float(max_discharge_kw) * 1000.0
        self._client: AsyncModbusTcpClient | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> bool:
        async with self._lock:
            if self._client and self._client.connected:
                return True
            self._client = AsyncModbusTcpClient(
                host=self.host,
                port=self.port,
                timeout=self.DEFAULT_TIMEOUT_SECONDS,
            )
            try:
                connected = await self._client.connect()
            except Exception as exc:
                _LOGGER.error("Anker Solix Modbus connect failed: %s", exc)
                connected = False
            return bool(connected)

    async def disconnect(self) -> None:
        async with self._lock:
            if self._client:
                self._client.close()
            self._client = None

    async def get_status(self) -> dict[str, Any]:
        """Read current status and return PowerSync-canonical values."""
        async with self._lock:
            if not self._client or not self._client.connected:
                if not await self._connect_unlocked():
                    raise ValueError("anker_solix_modbus_unavailable")

            pv_w = (await self._read_s32_input(self.REG_PV_POWER)) or 0
            third_party_pv_w = (
                await self._read_s32_input(self.REG_THIRD_PARTY_PV_POWER)
            ) or 0
            battery_w = (await self._read_s32_input(self.REG_BATTERY_POWER)) or 0
            load_w = (await self._read_s32_input(self.REG_LOAD_POWER)) or 0
            grid_w = (await self._read_s32_input(self.REG_GRID_POWER)) or 0
            soc = await self._read_u16_input(self.REG_BATTERY_SOC)
            rated_energy_raw = await self._read_u32_input(self.REG_RATED_ENERGY)
            max_charge_w = await self._read_s32_input(self.REG_MAX_CHARGE_POWER)
            max_discharge_w = await self._read_s32_input(self.REG_MAX_DISCHARGE_POWER)
            operating_mode = await self._read_u16_holding(self.REG_OPERATING_MODE)
            battery_status = await self._read_u16_input(self.REG_BATTERY_STATUS)
            ems_mode_mask = await self._read_u16_input(self.REG_EMS_MODE_MASK)

        capacity_kwh = (
            float(rated_energy_raw) / 10.0
            if rated_energy_raw is not None and rated_energy_raw > 0
            else self._configured_capacity_kwh
        )
        return {
            "solar_power": max(0.0, (float(pv_w) + float(third_party_pv_w)) / 1000.0),
            "grid_power": float(grid_w) / 1000.0,
            "battery_power": float(battery_w) / 1000.0,
            "load_power": max(0.0, float(load_w) / 1000.0),
            "battery_level": float(soc) if soc is not None else None,
            "battery_capacity_kwh": capacity_kwh,
            "battery_max_charge_power_w": abs(max_charge_w)
            if max_charge_w
            else self._configured_max_charge_w,
            "battery_max_discharge_power_w": abs(max_discharge_w)
            if max_discharge_w
            else self._configured_max_discharge_w,
            "operating_mode": operating_mode,
            "battery_status": battery_status,
            "ems_mode_mask": ems_mode_mask,
            "control_path": "direct_modbus",
        }

    async def force_charge(self, duration_minutes: int, power_w: int | float) -> bool:
        return await self._set_third_party_power(-abs(self._coerce_power(power_w, True)))

    async def force_discharge(
        self,
        duration_minutes: int,
        power_w: int | float,
    ) -> bool:
        return await self._set_third_party_power(abs(self._coerce_power(power_w, False)))

    async def restore_normal(self) -> bool:
        async with self._lock:
            if not self._client or not self._client.connected:
                if not await self._connect_unlocked():
                    return False
            ok_power = await self._write_s32_holding(self.REG_BATTERY_POWER_SETPOINT, 0)
            ok_mode = await self._write_u16_holding(
                self.REG_OPERATING_MODE,
                _MODE_SELF_CONSUMPTION,
            )
            return ok_power and ok_mode

    async def set_backup_mode(self) -> bool:
        return await self._set_third_party_power(0)

    async def restore_work_mode_from_idle(self) -> bool:
        return await self.restore_normal()

    async def set_self_consumption_mode(self) -> bool:
        return await self.restore_normal()

    async def set_backup_reserve(self, percent: int) -> bool:
        _LOGGER.info("Anker Solix backup reserve writes are not exposed by the current map")
        return False

    async def get_backup_reserve(self) -> int | None:
        return None

    def _coerce_power(self, power_w: int | float, charge: bool) -> int:
        configured = self._configured_max_charge_w if charge else self._configured_max_discharge_w
        value = float(power_w or configured or 5000.0)
        cap = configured or value
        return max(100, int(min(abs(value), cap)))

    async def _set_third_party_power(self, signed_power_w: int) -> bool:
        async with self._lock:
            if not self._client or not self._client.connected:
                if not await self._connect_unlocked():
                    return False
            ok_mode = await self._write_u16_holding(
                self.REG_OPERATING_MODE,
                _MODE_THIRD_PARTY_CONTROL,
            )
            ok_power = await self._write_s32_holding(
                self.REG_BATTERY_POWER_SETPOINT,
                int(signed_power_w),
            )
            if ok_mode and ok_power:
                _LOGGER.info("Anker Solix third-party power setpoint: %s W", signed_power_w)
            return ok_mode and ok_power

    async def _connect_unlocked(self) -> bool:
        self._client = AsyncModbusTcpClient(
            host=self.host,
            port=self.port,
            timeout=self.DEFAULT_TIMEOUT_SECONDS,
        )
        try:
            return bool(await self._client.connect())
        except Exception as exc:
            _LOGGER.debug("Anker Solix Modbus reconnect failed: %s", exc)
            return False

    async def _read_input(self, address: int, count: int) -> list[int] | None:
        try:
            result = await self._client.read_input_registers(
                address=address,
                count=count,
                **{_SLAVE_PARAM: self.slave_id},
            )
            if result.isError():
                return None
            return list(result.registers)
        except (ModbusException, Exception) as exc:
            _LOGGER.debug("Anker Solix read input 0x%04X failed: %s", address, exc)
            return None

    async def _read_holding(self, address: int, count: int) -> list[int] | None:
        try:
            result = await self._client.read_holding_registers(
                address=address,
                count=count,
                **{_SLAVE_PARAM: self.slave_id},
            )
            if result.isError():
                return None
            return list(result.registers)
        except (ModbusException, Exception) as exc:
            _LOGGER.debug("Anker Solix read holding 0x%04X failed: %s", address, exc)
            return None

    async def _write_holding(self, address: int, values: list[int]) -> bool:
        try:
            result = await self._client.write_registers(
                address=address,
                values=values,
                **{_SLAVE_PARAM: self.slave_id},
            )
            return not result.isError()
        except (ModbusException, Exception) as exc:
            _LOGGER.warning("Anker Solix write 0x%04X failed: %s", address, exc)
            return False

    async def _read_u16_input(self, address: int) -> int | None:
        regs = await self._read_input(address, 1)
        return regs[0] if regs else None

    async def _read_u16_holding(self, address: int) -> int | None:
        regs = await self._read_holding(address, 1)
        return regs[0] if regs else None

    async def _read_u32_input(self, address: int) -> int | None:
        regs = await self._read_input(address, 2)
        if not regs:
            return None
        return (regs[0] << 16) | regs[1]

    async def _read_s32_input(self, address: int) -> int | None:
        value = await self._read_u32_input(address)
        if value is None:
            return None
        if value & 0x80000000:
            value -= 0x100000000
        return value

    async def _write_u16_holding(self, address: int, value: int) -> bool:
        return await self._write_holding(address, [int(value) & 0xFFFF])

    async def _write_s32_holding(self, address: int, value: int) -> bool:
        raw = int(value) & 0xFFFFFFFF
        return await self._write_holding(address, [(raw >> 16) & 0xFFFF, raw & 0xFFFF])


class AnkerSolixEntityController:
    """Bridge controller for Anker Solix HA integrations."""

    _OFFICIAL_READ: dict[str, tuple[str, ...]] = {
        "battery_level": ("battery_soc",),
        "battery_charge_power": ("battery_charging_power",),
        "battery_discharge_power": ("battery_discharging_power",),
        "grid_import_power": ("grid_import_power",),
        "grid_export_power": ("grid_export_power",),
        "solar_power": ("pv_power",),
        "load_power": ("load_power",),
        "battery_capacity_kwh": ("rated_energy",),
        "battery_max_charge_power_w": ("max_charge_power",),
        "battery_max_discharge_power_w": ("max_discharge_power",),
        "mode": ("operating_mode",),
    }

    _OFFICIAL_WRITE: dict[str, tuple[tuple[str, str], ...]] = {
        "operating_mode": (("select", "operating_mode"),),
        "battery_power_direction": (("select", "battery_power_direction"),),
        "battery_power_setpoint": (("number", "battery_power_setpoint"),),
    }

    _CLOUD_READ: dict[str, tuple[str, ...]] = {
        "battery_level": ("solarbank_state_of_charge", "state_of_charge"),
        "battery_charge_power": ("charging_power", "solarbank_charging_power"),
        "battery_discharge_power": ("output_power", "solarbank_output_power"),
        "solar_power": ("input_power", "solarbank_input_power"),
        "load_power": ("home_load_power",),
        "battery_capacity_wh": ("battery_energy",),
        "mode": ("set_power_mode",),
    }

    _CLOUD_WRITE: dict[str, tuple[tuple[str, str], ...]] = {
        "preset_system_output_power": (("number", "preset_system_output_power"),),
        "preset_device_output_power": (("number", "preset_device_output_power"),),
        "preset_charge_priority": (("number", "preset_charge_priority"),),
        "preset_allow_export": (("switch", "preset_allow_export"),),
    }

    def __init__(
        self,
        hass: Any,
        *,
        integration_domain: str,
        config_entry_id: str | None = None,
        entity_prefix: str | None = None,
        battery_capacity_kwh: float | None = None,
        max_charge_kw: float = 5.0,
        max_discharge_kw: float = 5.0,
    ) -> None:
        self.hass = hass
        self.integration_domain = integration_domain
        self.config_entry_id = config_entry_id
        self.entity_prefix = (entity_prefix or "").strip()
        self._battery_capacity_kwh = battery_capacity_kwh
        self._max_charge_w = float(max_charge_kw) * 1000.0
        self._max_discharge_w = float(max_discharge_kw) * 1000.0
        self._entity_map: dict[str, str] = {}

    @property
    def is_official(self) -> bool:
        return self.integration_domain == _OFFICIAL_DOMAIN

    async def connect(self) -> bool:
        self._discover_entities()
        required = ("battery_level", "solar_power")
        missing = [key for key in required if key not in self._entity_map]
        if missing:
            raise ValueError(f"anker_solix_missing_entities:{','.join(missing)}")
        _LOGGER.info(
            "Anker Solix HA bridge validated (%s): %s",
            self.integration_domain,
            self._entity_map,
        )
        return True

    async def disconnect(self) -> None:
        return None

    def get_status(self) -> dict[str, Any]:
        if not self._entity_map:
            self._discover_entities()

        if self.is_official:
            charge_w = self._read_float("battery_charge_power") or 0.0
            discharge_w = self._read_float("battery_discharge_power") or 0.0
            battery_w = discharge_w - charge_w
            grid_w = (self._read_float("grid_import_power") or 0.0) - (
                self._read_float("grid_export_power") or 0.0
            )
            capacity_kwh = self._read_float("battery_capacity_kwh")
        else:
            charge_w = self._read_float("battery_charge_power") or 0.0
            output_w = self._read_float("battery_discharge_power")
            if output_w is None:
                output_w = self._read_float("preset_system_output_power")
            if output_w is None:
                output_w = self._read_float("preset_device_output_power") or 0.0
            battery_w = float(output_w or 0.0) - charge_w
            grid_w = 0.0
            capacity_wh = self._read_float("battery_capacity_wh")
            capacity_kwh = capacity_wh / 1000.0 if capacity_wh else None

        return {
            "solar_power": max(0.0, (self._read_float("solar_power") or 0.0) / 1000.0),
            "grid_power": grid_w / 1000.0,
            "battery_power": battery_w / 1000.0,
            "load_power": max(0.0, (self._read_float("load_power") or 0.0) / 1000.0),
            "battery_level": self._read_float("battery_level"),
            "battery_capacity_kwh": capacity_kwh or self._battery_capacity_kwh,
            "battery_max_charge_power_w": self._read_float("battery_max_charge_power_w")
            or self._max_charge_w,
            "battery_max_discharge_power_w": self._read_float("battery_max_discharge_power_w")
            or self._max_discharge_w,
            "mode": self._read_state("mode"),
            "control_path": self.integration_domain,
            "dispatch_supported": self.is_dispatch_supported(),
        }

    def is_dispatch_supported(self) -> bool:
        if self.is_official:
            return all(
                key in self._entity_map
                for key in ("operating_mode", "battery_power_direction", "battery_power_setpoint")
            )
        return "preset_system_output_power" in self._entity_map or "preset_device_output_power" in self._entity_map

    async def force_charge(self, duration_minutes: int, power_w: int | float) -> bool:
        if self.is_official:
            return await self._official_set_power("charge", power_w)
        if "preset_charge_priority" in self._entity_map:
            await self._set_number("preset_charge_priority", 100)
        if "preset_system_output_power" in self._entity_map:
            return await self._set_number("preset_system_output_power", 0)
        if "preset_device_output_power" in self._entity_map:
            return await self._set_number("preset_device_output_power", 0)
        return False

    async def force_discharge(self, duration_minutes: int, power_w: int | float) -> bool:
        if self.is_official:
            return await self._official_set_power("discharge", power_w)
        target = max(0, int(float(power_w or self._max_discharge_w)))
        if "preset_charge_priority" in self._entity_map:
            await self._set_number("preset_charge_priority", 0)
        if "preset_system_output_power" in self._entity_map:
            return await self._set_number("preset_system_output_power", target)
        if "preset_device_output_power" in self._entity_map:
            return await self._set_number("preset_device_output_power", target)
        return False

    async def restore_normal(self) -> bool:
        if self.is_official and "operating_mode" in self._entity_map:
            return await self._set_select("operating_mode", "self_consumption")
        if "preset_charge_priority" in self._entity_map:
            await self._set_number("preset_charge_priority", 80)
        return True

    async def set_self_consumption_mode(self) -> bool:
        return await self.restore_normal()

    async def set_backup_mode(self) -> bool:
        if self.is_official:
            return await self._official_set_power("charge", 0)
        return await self.force_charge(30, 0)

    async def restore_work_mode_from_idle(self) -> bool:
        return await self.restore_normal()

    async def set_backup_reserve(self, percent: int) -> bool:
        if "preset_charge_priority" in self._entity_map:
            return await self._set_number("preset_charge_priority", percent)
        return False

    async def get_backup_reserve(self) -> int | None:
        value = self._read_float("preset_charge_priority")
        return int(value) if value is not None else None

    async def _official_set_power(self, direction: str, power_w: int | float) -> bool:
        target = max(0, int(float(power_w or (self._max_charge_w if direction == "charge" else self._max_discharge_w))))
        if not await self._set_select("operating_mode", "third_party_control"):
            return False
        if not await self._set_select("battery_power_direction", direction):
            return False
        return await self._set_number("battery_power_setpoint", target)

    def _discover_entities(self) -> None:
        self._entity_map = {}
        registry = er.async_get(self.hass)
        entity_ids: list[str] = []
        if self.config_entry_id:
            entity_ids.extend(
                entry.entity_id
                for entry in er.async_entries_for_config_entry(registry, self.config_entry_id)
                if entry.entity_id
            )
        known = set(entity_ids)
        entity_ids.extend(
            state.entity_id
            for state in self.hass.states.async_all()
            if state.entity_id.startswith(("sensor.", "number.", "select.", "switch."))
            and state.entity_id not in known
        )

        read_map = self._OFFICIAL_READ if self.is_official else self._CLOUD_READ
        write_map = self._OFFICIAL_WRITE if self.is_official else self._CLOUD_WRITE
        for key, suffixes in read_map.items():
            entity_id = self._resolve_entity(entity_ids, "sensor", suffixes)
            if entity_id:
                self._entity_map[key] = entity_id
        for key, candidates in write_map.items():
            for domain, suffix in candidates:
                entity_id = self._resolve_entity(entity_ids, domain, (suffix,))
                if entity_id:
                    self._entity_map[key] = entity_id
                    break

    def _resolve_entity(
        self,
        entity_ids: list[str],
        domain: str,
        suffixes: tuple[str, ...],
    ) -> str | None:
        domain_prefix = f"{domain}."
        for suffix in suffixes:
            exacts = []
            if self.entity_prefix:
                exacts.append(f"{domain}.{self.entity_prefix}_{suffix}")
            exacts.append(f"{domain}.{suffix}")
            for exact in exacts:
                if self.hass.states.get(exact) is not None:
                    return exact

            tail = f"_{suffix}"
            matches = [
                entity_id
                for entity_id in entity_ids
                if entity_id.startswith(domain_prefix)
                and (entity_id.endswith(tail) or entity_id.endswith(suffix))
                and (
                    not self.entity_prefix
                    or entity_id.startswith(f"{domain}.{self.entity_prefix}_")
                )
                and self.hass.states.get(entity_id) is not None
            ]
            if matches:
                return sorted(matches, key=lambda item: (len(item), item))[0]
        return None

    def _read_state(self, key: str) -> str | None:
        entity_id = self._entity_map.get(key)
        state = self.hass.states.get(entity_id) if entity_id else None
        if not state or state.state in ("unknown", "unavailable", "none", ""):
            return None
        return str(state.state)

    def _read_float(self, key: str) -> float | None:
        text = self._read_state(key)
        if text is None:
            return None
        try:
            return float(text)
        except (TypeError, ValueError):
            return None

    async def _set_number(self, key: str, value: int | float) -> bool:
        entity_id = self._entity_map.get(key)
        if not entity_id:
            return False
        await self.hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entity_id, "value": float(value)},
            blocking=True,
        )
        return True

    async def _set_select(self, key: str, option: str) -> bool:
        entity_id = self._entity_map.get(key)
        if not entity_id:
            return False
        await self.hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": entity_id, "option": option},
            blocking=True,
        )
        return True
