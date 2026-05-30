"""Fronius GEN24 storage bridge via the fronius_modbus integration.

PowerSync does not open a second Modbus connection here. It controls the
entities exposed by the upstream `fronius_modbus` integration through Home
Assistant services.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)


_READ_ENTITIES: dict[str, tuple[str, ...]] = {
    "battery_level": (
        "state_of_charge",
        "soc",
        "battery_storage_soc",
        "reserva_state_of_charge_2",
        "reserva_state_of_charge_3",
        "reserva_state_of_charge",
    ),
    "battery_charge_power": (
        "storage_charging_power",
        "reserva_storage_charging_power",
    ),
    "battery_discharge_power": (
        "storage_discharging_power",
        "reserva_storage_discharging_power",
    ),
    "grid_power": (
        "meter_200_power",
        "meter_power",
        "meter_1_power",
        "smart_meter_63a_1_meter_1_power",
        "smart_meter_power",
    ),
    "solar_power": (
        "pv_power_2",
        "pv_power",
    ),
    "load_power": (
        "load_2",
        "load",
    ),
    "battery_temperature": (
        "cell_temperature",
        "storage_temperature",
        "reserva_temperature",
        "reserva_cell_temperature",
    ),
    "battery_capacity_wh": (
        "capacity",
        "whrtg",
        "battery_storage_capacity",
        "reserva_capacity_2",
        "reserva_capacity",
        "reserva_designed_capacity",
        "reserva_maximum_capacity",
    ),
    "battery_max_charge_power_w": (
        "maximum_charge_rate",
        "max_charge_rate",
        "max_charge",
        "maxcharte",
        "battery_storage_maximum_charge_rate",
    ),
    "battery_max_discharge_power_w": (
        "maximum_discharge_rate",
        "max_discharge_rate",
        "maxdischarte",
        "battery_storage_maximum_discharge_rate",
    ),
    "backup_reserve_sensor": (
        "soc_minimum",
        "battery_storage_soc_minimum",
        "reserva_soc_minimum",
    ),
    "storage_control_mode_sensor": (
        "core_storage_control_mode",
        "storage_control_mode",
        "reserva_storage_control_mode_2",
        "reserva_storage_control_mode",
        "control_mode_2",
    ),
}

_WRITE_ENTITIES: dict[str, tuple[str, ...]] = {
    "battery_api_mode": (
        "battery_api_mode",
        "reserva_battery_api_mode",
    ),
    "storage_control_mode": (
        "storage_control_mode",
        "reserva_storage_control_mode_2",
        "reserva_storage_control_mode",
    ),
    "grid_charge_power": (
        "grid_charge_power",
        "reserva_grid_charge_power_2",
        "reserva_grid_charge_power",
    ),
    "grid_discharge_power": (
        "grid_discharge_power",
        "reserva_grid_discharge_power_2",
        "reserva_grid_discharge_power",
    ),
    "pv_charge_limit": (
        "pv_charge_limit",
        "charge_limit",
        "reserva_pv_charge_limit_2",
        "reserva_pv_charge_limit",
    ),
    "discharge_limit": (
        "discharge_limit",
        "reserva_discharge_limit_2",
        "reserva_discharge_limit",
    ),
    "backup_reserve": (
        "soc_minimum",
        "battery_storage_soc_minimum",
        "reserva_soc_minimum",
    ),
}

_MODE_AUTO = "Auto"
_MODE_MANUAL = "Manual"
_MODE_CHARGE_FROM_GRID = "Charge from Grid"
_MODE_DISCHARGE_TO_GRID = "Discharge to Grid"
_MODE_PV_AND_DISCHARGE_LIMIT = "PV Charge and Discharge Limit"
_MODE_BLOCK_CHARGING = "Block Charging"
_MODE_BLOCK_DISCHARGING = "Block Discharging"

_OPTION_WAIT_SECONDS = 12.0
_OPTION_WAIT_STEP_SECONDS = 0.5


class FroniusReservaBatteryController:
    """Bridge controller for Fronius GEN24 storage entities exposed by fronius_modbus."""

    def __init__(
        self,
        hass: Any,
        fronius_entry_id: str,
        battery_capacity_kwh: float = 9.6,
        max_charge_kw: float = 5.0,
        max_discharge_kw: float = 5.0,
    ) -> None:
        self.hass = hass
        self._fronius_entry_id = fronius_entry_id
        self._battery_capacity_kwh = float(battery_capacity_kwh)
        self._max_charge_w = float(max_charge_kw) * 1000.0
        self._max_discharge_w = float(max_discharge_kw) * 1000.0
        self._entity_map: dict[str, str] = {}

    async def connect(self) -> bool:
        """Validate that the required Fronius GEN24 storage entities exist."""
        self._discover_entities()
        required = (
            "battery_level",
            "battery_api_mode",
            "storage_control_mode",
            "grid_charge_power",
            "grid_discharge_power",
            "pv_charge_limit",
            "discharge_limit",
        )
        missing = [key for key in required if key not in self._entity_map]
        if missing:
            missing_ids = [self._expected_entity_hint(key) or key for key in missing]
            raise ValueError(f"fronius_reserva_missing_entities:{','.join(missing_ids)}")

        _LOGGER.info(
            "Fronius GEN24 storage entities validated via config entry %s — mapped: %s",
            self._fronius_entry_id,
            {k: v for k, v in self._entity_map.items()},
        )
        return True

    def _discover_entities(self) -> None:
        """Discover entity IDs from the upstream config entry, with state fallback."""
        self._entity_map = {}

        registry = er.async_get(self.hass)
        entries = er.async_entries_for_config_entry(registry, self._fronius_entry_id)
        entity_ids = [entry.entity_id for entry in entries if entry.entity_id]
        known = set(entity_ids)
        entity_ids.extend(
            state.entity_id
            for state in self.hass.states.async_all()
            if state.entity_id.startswith(("sensor.", "number.", "select."))
            and state.entity_id not in known
        )

        for key, suffixes in _READ_ENTITIES.items():
            entity_id = self._resolve_entity_id(entity_ids, "sensor", suffixes)
            if entity_id:
                self._entity_map[key] = entity_id

        for key, suffixes in _WRITE_ENTITIES.items():
            domain = "select" if key in ("battery_api_mode", "storage_control_mode") else "number"
            entity_id = self._resolve_entity_id(entity_ids, domain, suffixes)
            if entity_id:
                self._entity_map[key] = entity_id

    def _resolve_entity_id(
        self,
        entity_ids: list[str],
        domain: str,
        suffixes: tuple[str, ...],
    ) -> str | None:
        domain_prefix = f"{domain}."
        for suffix in suffixes:
            exact = f"{domain}.{suffix}"
            if self.hass.states.get(exact) is not None:
                return exact

            tail = f"_{suffix}"
            matches = [
                entity_id for entity_id in entity_ids
                if entity_id.startswith(domain_prefix)
                and (entity_id.endswith(tail) or entity_id.endswith(suffix))
            ]
            if not matches:
                continue

            matches = sorted(matches, key=lambda entity_id: (len(entity_id), entity_id))
            for entity_id in matches:
                if self.hass.states.get(entity_id) is not None:
                    return entity_id
            return matches[0]
        return None

    def _expected_entity_hint(self, key: str) -> str | None:
        suffixes = _READ_ENTITIES.get(key) or _WRITE_ENTITIES.get(key)
        if not suffixes:
            return None
        domain = "sensor"
        if key in _WRITE_ENTITIES:
            domain = "select" if key in ("battery_api_mode", "storage_control_mode") else "number"
        return f"{domain}.{suffixes[0]}"

    def get_status(self) -> dict[str, Any]:
        """Read current Fronius storage state and return PowerSync-canonical fields."""
        if not self._entity_map:
            self._discover_entities()

        charge_w = self._read_float("battery_charge_power") or 0.0
        discharge_w = self._read_float("battery_discharge_power") or 0.0
        battery_kw = (discharge_w - charge_w) / 1000.0
        grid_kw = (self._read_float("grid_power") or 0.0) / 1000.0
        solar_kw = max(0.0, (self._read_float("solar_power") or 0.0) / 1000.0)
        load_kw = max(0.0, (self._read_float("load_power") or 0.0) / 1000.0)

        if load_kw <= 0:
            balanced_load_kw = solar_kw + grid_kw + battery_kw
            if balanced_load_kw > 0:
                load_kw = balanced_load_kw

        reserve = self._read_float("backup_reserve") or self._read_float("backup_reserve_sensor")
        capacity_wh = self._read_float("battery_capacity_wh")
        max_charge_w = self._read_float("battery_max_charge_power_w") or self._max_charge_w
        max_discharge_w = self._read_float("battery_max_discharge_power_w") or self._max_discharge_w
        mode = self._read_state("storage_control_mode") or self._read_state("storage_control_mode_sensor")

        return {
            "battery_level": self._read_float("battery_level") or 0.0,
            "battery_power": battery_kw,
            "grid_power": grid_kw,
            "solar_power": solar_kw,
            "load_power": load_kw,
            "battery_temperature": self._read_float("battery_temperature"),
            "battery_capacity_kwh": (
                capacity_wh / 1000.0
                if capacity_wh is not None and capacity_wh > 0
                else self._battery_capacity_kwh
            ),
            "battery_max_charge_power_w": max_charge_w,
            "battery_max_discharge_power_w": max_discharge_w,
            "backup_reserve": reserve,
            "min_soc": reserve,
            "mode": mode,
        }

    async def force_charge(self, duration_minutes: int, power_w: int) -> bool:
        """Force charge from grid at the requested wattage."""
        await self._ensure_connected()
        target_w = self._target_power_w(power_w, self._max_charge_w)
        try:
            await self._set_select("battery_api_mode", _MODE_MANUAL)
            await self._set_select("storage_control_mode", _MODE_CHARGE_FROM_GRID)
            if not await self._wait_entity_available("grid_charge_power"):
                return False
            await self._set_number("grid_charge_power", target_w)
        except Exception:
            _LOGGER.exception("Fronius GEN24 storage force_charge failed")
            await self.restore_normal()
            return False
        _LOGGER.info(
            "Fronius GEN24 storage force_charge: %.0f W for %d min",
            target_w,
            duration_minutes,
        )
        return True

    async def force_discharge(self, duration_minutes: int, power_w: int) -> bool:
        """Force discharge/export to grid at the requested wattage."""
        await self._ensure_connected()
        target_w = self._target_power_w(power_w, self._max_discharge_w)
        try:
            await self._set_select("battery_api_mode", _MODE_MANUAL)
            await self._set_select("storage_control_mode", _MODE_DISCHARGE_TO_GRID)
            if not await self._wait_entity_available("grid_discharge_power"):
                return False
            await self._set_number("grid_discharge_power", target_w)
        except Exception:
            _LOGGER.exception("Fronius GEN24 storage force_discharge failed")
            await self.restore_normal()
            return False
        _LOGGER.info(
            "Fronius GEN24 storage force_discharge: %.0f W for %d min",
            target_w,
            duration_minutes,
        )
        return True

    async def set_idle(self) -> bool:
        """Hold battery SOC by zeroing PV-charge and discharge limits."""
        await self._ensure_connected()
        try:
            await self._set_select("battery_api_mode", _MODE_MANUAL)
            await self._set_select("storage_control_mode", _MODE_PV_AND_DISCHARGE_LIMIT)
            pv_available, discharge_available = await asyncio.gather(
                self._wait_entity_available("pv_charge_limit"),
                self._wait_entity_available("discharge_limit"),
            )
            if not pv_available or not discharge_available:
                return False
            await self._set_number("pv_charge_limit", 0)
            await self._set_number("discharge_limit", 0)
        except Exception:
            _LOGGER.exception("Fronius GEN24 storage set_idle failed")
            await self.restore_normal()
            return False
        _LOGGER.info("Fronius GEN24 storage idle: PV charge and discharge limits set to 0 W")
        return True

    async def block_charging(self) -> bool:
        """Block battery charging."""
        await self._ensure_connected()
        await self._set_select("battery_api_mode", _MODE_MANUAL)
        await self._set_select("storage_control_mode", _MODE_BLOCK_CHARGING)
        return True

    async def block_discharging(self) -> bool:
        """Block battery discharging."""
        await self._ensure_connected()
        await self._set_select("battery_api_mode", _MODE_MANUAL)
        await self._set_select("storage_control_mode", _MODE_BLOCK_DISCHARGING)
        return True

    async def restore_normal(self) -> bool:
        """Restore Fronius GEN24 storage to automatic control."""
        await self._ensure_connected()
        await self._set_select("storage_control_mode", _MODE_AUTO)
        _LOGGER.info("Fronius GEN24 storage restored to Auto storage control")
        return True

    async def set_backup_reserve(self, percent: int) -> bool:
        """Set Fronius storage minimum SOC."""
        await self._ensure_connected()
        if not self._entity_map.get("backup_reserve"):
            _LOGGER.warning("Fronius GEN24 storage backup reserve entity not found")
            return False
        clamped = max(5, min(100, int(percent)))
        await self._set_number("backup_reserve", clamped)
        _LOGGER.info("Fronius GEN24 storage SoC minimum set to %d%%", clamped)
        return True

    async def get_backup_reserve(self) -> int | None:
        """Read current Fronius storage minimum SOC."""
        await self._ensure_connected()
        reserve = self._read_float("backup_reserve") or self._read_float("backup_reserve_sensor")
        return int(reserve) if reserve is not None else None

    async def disconnect(self) -> None:
        """No persistent connection to close."""
        return None

    async def _ensure_connected(self) -> None:
        if not self._entity_map:
            await self.connect()

    @staticmethod
    def _target_power_w(requested_w: int | float, fallback_w: float) -> int:
        value = float(requested_w or 0)
        if value <= 0:
            value = fallback_w
        return int(max(0, round(value)))

    def _read_state(self, key: str) -> str | None:
        entity_id = self._entity_map.get(key)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if not state or state.state in ("unavailable", "unknown", ""):
            return None
        return str(state.state)

    def _read_float(self, key: str) -> float | None:
        state = self._read_state(key)
        if state is None:
            return None
        try:
            return float(state)
        except (TypeError, ValueError):
            return None

    def _entity_state_available(self, key: str) -> bool:
        entity_id = self._entity_map.get(key)
        if not entity_id:
            return False
        state = self.hass.states.get(entity_id)
        if not state:
            return False
        value = str(state.state).lower().strip()
        return value not in ("unavailable", "unknown", "")

    async def _wait_entity_available(self, key: str) -> bool:
        elapsed = 0.0
        while elapsed <= _OPTION_WAIT_SECONDS:
            if self._entity_state_available(key):
                return True
            await asyncio.sleep(_OPTION_WAIT_STEP_SECONDS)
            elapsed += _OPTION_WAIT_STEP_SECONDS
        _LOGGER.warning(
            "Fronius GEN24 storage: entity %s is still unavailable after mode switch; attempting write anyway",
            self._entity_map.get(key, key),
        )
        return True

    async def _set_select(self, key: str, option: str) -> None:
        entity_id = self._entity_map[key]
        _LOGGER.debug("Fronius GEN24 storage: selecting %s = %s", entity_id, option)
        await self.hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": entity_id, "option": option},
            blocking=True,
        )

    async def _set_number(self, key: str, value: int | float) -> None:
        entity_id = self._entity_map[key]
        _LOGGER.debug("Fronius GEN24 storage: setting %s = %s", entity_id, value)
        await self.hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entity_id, "value": value},
            blocking=True,
        )
