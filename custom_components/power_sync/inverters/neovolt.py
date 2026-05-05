"""Neovolt / Bytewatt battery bridge via the upstream Neovolt integration.

PowerSync does not open a Modbus connection here. The HACS Neovolt integration
owns the Modbus session; this controller discovers its Home Assistant entities
and writes the dispatch controls through HA services.

Sign conventions:
  PowerSync battery_power: positive = discharging, negative = charging
  Neovolt battery_power:  positive = discharging, negative = charging
  PowerSync grid_power:   positive = importing, negative = exporting
  Neovolt grid_power_total follows the same convention.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)


_READ_ENTITIES: dict[str, tuple[tuple[str, str], ...]] = {
    "battery_power": (
        ("sensor", "combined_battery_power"),
        ("sensor", "battery_power"),
    ),
    "battery_level": (
        ("sensor", "combined_battery_soc"),
        ("sensor", "battery_soc"),
    ),
    "battery_capacity_kwh": (
        ("sensor", "combined_battery_capacity"),
        ("sensor", "battery_capacity"),
    ),
    "load_power": (
        ("sensor", "combined_house_load"),
        ("sensor", "total_house_load"),
    ),
    "solar_power": (
        ("sensor", "combined_pv_power"),
        ("sensor", "pv_total_active_power"),
        ("sensor", "pv_power_total"),
    ),
    "grid_power": (
        ("sensor", "grid_total_active_power"),
        ("sensor", "grid_power_total"),
    ),
    "battery_soh": (
        ("sensor", "combined_battery_soh"),
        ("sensor", "battery_soh"),
    ),
}

_WRITE_ENTITIES: dict[str, tuple[tuple[str, str], ...]] = {
    "dispatch_mode": (("select", "dispatch_mode"),),
    "dispatch_power": (("number", "dispatch_power"),),
    "dispatch_duration": (("number", "dispatch_duration"),),
    "dispatch_charge_soc": (
        ("number", "dispatch_charge_target_soc"),
        ("number", "dispatch_charge_soc"),
    ),
    "dispatch_discharge_soc": (
        ("number", "dispatch_discharge_cutoff_soc"),
        ("number", "dispatch_discharge_soc"),
    ),
    "backup_reserve": (
        ("number", "discharging_cutoff_soc_default"),
        ("number", "discharging_cutoff_soc"),
    ),
    "stop_dispatch_button": (
        ("button", "stop_force_charge_discharge"),
        ("button", "stop_dispatch"),
    ),
}

_CONTROL_REQUIRED = (
    "dispatch_mode",
    "dispatch_power",
    "dispatch_duration",
    "dispatch_charge_soc",
    "dispatch_discharge_soc",
)

_READ_REQUIRED = (
    "battery_power",
    "battery_level",
    "grid_power",
    "load_power",
    "solar_power",
)


class NeovoltBatteryController:
    """Bridge controller for Neovolt entities exposed by the HACS integration."""

    def __init__(
        self,
        hass: Any,
        neovolt_entry_id: str,
        max_charge_kw: float = 5.0,
        max_discharge_kw: float = 5.0,
        min_soc_pct: float = 10.0,
    ) -> None:
        self.hass = hass
        self._neovolt_entry_id = neovolt_entry_id
        self._max_charge_kw = float(max_charge_kw)
        self._max_discharge_kw = float(max_discharge_kw)
        self._min_soc_pct = float(min_soc_pct)
        self._entity_map: dict[str, str] = {}

    def set_min_soc_pct(self, min_soc_pct: float) -> None:
        """Update the discharge cutoff used for force-discharge commands."""
        self._min_soc_pct = float(min_soc_pct)

    async def connect(self) -> bool:
        """Validate that required Neovolt entities exist."""
        self._discover_entities()

        missing = self._missing_keys(_READ_REQUIRED + _CONTROL_REQUIRED)
        if missing:
            missing_ids = [
                self._entity_map.get(key)
                or self._expected_entity_hint(key)
                or key
                for key in missing
            ]
            raise ValueError(f"neovolt_missing_entities:{','.join(missing_ids)}")

        _LOGGER.info(
            "Neovolt entities validated via config entry %s (%d mapped)",
            self._neovolt_entry_id,
            len(self._entity_map),
        )
        return True

    async def disconnect(self) -> None:
        """No persistent connection to close."""
        return None

    def get_status(self) -> dict[str, Any]:
        """Read current Neovolt state and return PowerSync-canonical fields."""
        if not self._entity_map:
            self._discover_entities()

        battery_w = self._read_float("battery_power") or 0.0
        grid_w = self._read_float("grid_power") or 0.0
        solar_w = self._read_float("solar_power") or 0.0
        load_w = self._read_float("load_power") or 0.0

        return {
            "solar_power": max(0.0, solar_w / 1000.0),
            "grid_power": grid_w / 1000.0,
            "battery_power": battery_w / 1000.0,
            "load_power": max(0.0, load_w / 1000.0),
            "battery_level": self._read_float("battery_level") or 0.0,
            "battery_capacity_kwh": self._read_float("battery_capacity_kwh"),
            "battery_soh": self._read_float("battery_soh"),
            "battery_max_charge_power_w": self._max_charge_kw * 1000.0,
            "battery_max_discharge_power_w": self._max_discharge_kw * 1000.0,
        }

    async def force_charge(self, duration_minutes: int, power_w: int | float) -> bool:
        """Force battery to charge via Neovolt dispatch controls."""
        await self._ensure_connected()
        power_kw = self._watts_to_kw(power_w, self._max_charge_kw)
        try:
            await self._set_number("dispatch_power", power_kw)
            await self._set_number("dispatch_duration", int(duration_minutes))
            await self._set_number("dispatch_charge_soc", 100)
            await self._set_select("dispatch_mode", "Force Charge")
        except Exception:
            _LOGGER.exception("Neovolt force_charge failed")
            return False

        _LOGGER.info(
            "Neovolt force_charge: %.1f kW for %d minutes",
            power_kw,
            duration_minutes,
        )
        return True

    async def force_discharge(self, duration_minutes: int, power_w: int | float) -> bool:
        """Force battery to discharge via Neovolt dispatch controls."""
        await self._ensure_connected()
        power_kw = self._watts_to_kw(power_w, self._max_discharge_kw)
        cutoff_soc = max(4, min(100, int(round(self._min_soc_pct))))
        try:
            await self._set_number("dispatch_power", power_kw)
            await self._set_number("dispatch_duration", int(duration_minutes))
            await self._set_number("dispatch_discharge_soc", cutoff_soc)
            await self._set_select("dispatch_mode", "Force Discharge")
        except Exception:
            _LOGGER.exception("Neovolt force_discharge failed")
            return False

        _LOGGER.info(
            "Neovolt force_discharge: %.1f kW for %d minutes, cutoff SOC %d%%",
            power_kw,
            duration_minutes,
            cutoff_soc,
        )
        return True

    async def restore_normal(self) -> bool:
        """Return Neovolt dispatch mode to Normal."""
        await self._ensure_connected()
        try:
            await self._set_select("dispatch_mode", "Normal")
            _LOGGER.info("Neovolt restored to Normal dispatch mode")
            return True
        except Exception:
            _LOGGER.exception("Neovolt restore_normal select failed")

        if self._entity_map.get("stop_dispatch_button"):
            try:
                await self._press_button("stop_dispatch_button")
                _LOGGER.info("Neovolt stop dispatch button pressed as recovery fallback")
                return True
            except Exception:
                _LOGGER.exception("Neovolt stop dispatch fallback failed")
        return False

    async def set_backup_reserve(self, percent: int) -> bool:
        """Set the default discharging cutoff SOC in the Neovolt integration."""
        await self._ensure_connected()
        self._min_soc_pct = float(percent)
        if not self._entity_exists("backup_reserve"):
            _LOGGER.warning("Neovolt backup reserve entity not found")
            return False
        clamped = max(4, min(100, int(percent)))
        await self._set_number("backup_reserve", clamped)
        _LOGGER.info("Neovolt discharging cutoff SOC set to %d%%", clamped)
        return True

    async def get_backup_reserve(self) -> int | None:
        """Read the current default discharging cutoff SOC."""
        await self._ensure_connected()
        reserve = self._read_float("backup_reserve")
        return int(reserve) if reserve is not None else None

    async def set_idle(self) -> bool:
        """Best-effort hold: raise the discharge cutoff to the current SOC."""
        status = self.get_status()
        current_soc = status.get("battery_level")
        if current_soc is None:
            return False
        return await self.set_backup_reserve(int(round(current_soc)))

    def _discover_entities(self) -> None:
        """Populate logical entity map from selected config entry and live states."""
        self._entity_map = {}
        candidates = self._entity_candidates()

        for key, patterns in _READ_ENTITIES.items():
            entity_id = self._resolve_entity_id(candidates, patterns)
            if entity_id:
                self._entity_map[key] = entity_id

        for key, patterns in _WRITE_ENTITIES.items():
            entity_id = self._resolve_entity_id(candidates, patterns)
            if entity_id:
                self._entity_map[key] = entity_id

    def _entity_candidates(self) -> list[tuple[str, str | None]]:
        registry = er.async_get(self.hass)
        entries = er.async_entries_for_config_entry(registry, self._neovolt_entry_id)
        candidates: list[tuple[str, str | None]] = [
            (entry.entity_id, getattr(entry, "unique_id", None))
            for entry in entries
            if getattr(entry, "entity_id", None)
        ]
        seen = {entity_id for entity_id, _unique_id in candidates}
        for state in self.hass.states.async_all():
            entity_id = state.entity_id
            if (
                entity_id.startswith(("sensor.", "number.", "select.", "button."))
                and entity_id not in seen
                and ".neovolt_" in entity_id
            ):
                candidates.append((entity_id, None))
                seen.add(entity_id)
        return candidates

    def _resolve_entity_id(
        self,
        candidates: list[tuple[str, str | None]],
        patterns: tuple[tuple[str, str], ...],
    ) -> str | None:
        for domain, suffix in patterns:
            domain_prefix = f"{domain}."
            matches: list[str] = []
            entity_tail = f"_{suffix}"
            unique_tail = f"_{suffix}"
            for entity_id, unique_id in candidates:
                if not entity_id.startswith(domain_prefix):
                    continue
                object_id = entity_id.split(".", 1)[1]
                if (
                    object_id.endswith(entity_tail)
                    or (unique_id and unique_id.endswith(unique_tail))
                ):
                    matches.append(entity_id)
            if not matches:
                continue
            matches = sorted(
                matches,
                key=lambda entity_id: (
                    0 if self.hass.states.get(entity_id) is not None else 1,
                    len(entity_id),
                    entity_id,
                ),
            )
            return matches[0]
        return None

    async def _ensure_connected(self) -> None:
        if not self._entity_map:
            await self.connect()

    def _expected_entity_hint(self, key: str) -> str | None:
        patterns = _READ_ENTITIES.get(key) or _WRITE_ENTITIES.get(key)
        if not patterns:
            return None
        domain, suffix = patterns[0]
        return f"{domain}.neovolt_1_{suffix}"

    def _missing_keys(self, keys: tuple[str, ...]) -> list[str]:
        return [
            key for key in keys
            if key not in self._entity_map
            or self.hass.states.get(self._entity_map.get(key, "")) is None
        ]

    def _entity_exists(self, key: str) -> bool:
        entity_id = self._entity_map.get(key)
        return bool(entity_id and self.hass.states.get(entity_id) is not None)

    def _read_float(self, key: str) -> float | None:
        entity_id = self._entity_map.get(key)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if not state or state.state in ("unavailable", "unknown", ""):
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    async def _set_number(self, key: str, value: float | int) -> None:
        entity_id = self._entity_map.get(key)
        if not entity_id:
            raise ValueError(f"Neovolt number entity not mapped: {key}")
        await self.hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entity_id, "value": value},
            blocking=True,
        )

    async def _set_select(self, key: str, option: str) -> None:
        entity_id = self._entity_map.get(key)
        if not entity_id:
            raise ValueError(f"Neovolt select entity not mapped: {key}")
        await self.hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": entity_id, "option": option},
            blocking=True,
        )

    async def _press_button(self, key: str) -> None:
        entity_id = self._entity_map.get(key)
        if not entity_id:
            raise ValueError(f"Neovolt button entity not mapped: {key}")
        await self.hass.services.async_call(
            "button",
            "press",
            {"entity_id": entity_id},
            blocking=True,
        )

    @staticmethod
    def _watts_to_kw(power_w: int | float, default_kw: float) -> float:
        if power_w and power_w > 0:
            return round(float(power_w) / 1000.0, 3)
        return float(default_kw)
