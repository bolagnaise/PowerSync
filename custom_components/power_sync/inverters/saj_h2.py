"""SAJ H2 / HS2 battery bridge via the upstream saj_h2_modbus integration.

PowerSync does not open a second Modbus connection here. Instead it discovers
the entities created by `stanus74/home-assistant-saj-h2-modbus` and controls
them through Home Assistant services.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)


# Maps internal slot → tuple of unique_id suffixes to try (first match wins).
# Fast-poll sensors are preferred over slow-poll for fresher readings.
# stanus74 integration uses camelCase keys: unique_id = f"{hub_name}_{key}" or f"{hub_name}_fast_{key}"
_SENSOR_KEYS: dict[str, tuple[str, ...]] = {
    "battery_level":               ("Bat1SOC", "batEnergyPercent"),
    "battery_power":               ("batteryPower",),
    "grid_power":                  ("gridPower", "totalgridPower", "CT_GridPowerWatt"),
    "solar_power":                 ("pvPower", "CT_PVPowerWatt"),
    "load_power":                  ("TotalLoadPower", "gridPower"),
    "battery_temperature":         ("BatTemp", "Bat1Temperature"),
    "app_mode":                    ("AppMode",),
    "battery_max_charge_power_w":  ("BatChargePower", "GridChargePower", "BatChaCurrLimit"),
    "battery_max_discharge_power_w": ("BatDischargePower", "GridDischargePower", "BatDisCurrLimit"),
    # Direction sensors — 1=discharging/export, -1=charging/import, 0=idle
    "direction_battery":           ("directionBattery",),
    "direction_grid":              ("directionGrid",),
}

# Maps internal slot → unique_id suffix for writable number entities.
# stanus74 constructs: f"{hub_name}_{key}_input" — so we search for endswith("_{key}_input")
_NUMBER_KEYS: dict[str, str] = {
    "charge_power":      "passive_battery_charge_power_input",
    # 0–1100 (% of rated power × 10): 0=locked/idle, 1100=full rate for force-discharge or self-consumption
    "discharge_power_pct": "passive_battery_discharge_power_input",
    # 0=disabled (inverter follows grid charge schedule), 2=passive self-consumption
    "passive_enable":    "passive_charge_enable_input",
}

# Maps internal slot → unique_id suffix for writable switch entities.
# stanus74 constructs: f"{hub_name}_{switch_type}{unique_id_suffix}"
# passive charge → ends with "_passive_charge_control", passive discharge → "_passive_discharge_control"
_SWITCH_KEYS: dict[str, str] = {
    "charge_switch":   "passive_charge_control",
    "discharge_switch": "passive_discharge_control",
}


class SajH2BatteryController:
    """Bridge controller for SAJ H2 entities exposed by saj_h2_modbus."""

    def __init__(
        self,
        hass: Any,
        saj_entry_id: str,
        battery_capacity_kwh: float = 10.0,
    ) -> None:
        self.hass = hass
        self._saj_entry_id = saj_entry_id
        self._battery_capacity_kwh = float(battery_capacity_kwh)
        self._entity_map: dict[str, str] = {}

    async def connect(self) -> bool:
        """Validate that the required SAJ entities exist."""
        self._discover_entities()
        required = (
            "battery_level",
            "battery_power",
            "grid_power",
            "solar_power",
            "load_power",
        )
        missing = [key for key in required if key not in self._entity_map]
        if missing:
            raise ValueError(f"saj_missing_entities:{','.join(missing)}")
        _LOGGER.info(
            "SAJ H2 entities validated via config entry %s — mapped: %s",
            self._saj_entry_id,
            {k: v for k, v in self._entity_map.items()},
        )
        control_missing = [k for k in ("charge_power", "discharge_power_pct", "charge_switch") if k not in self._entity_map]
        if control_missing:
            _LOGGER.warning(
                "SAJ H2: control entities not found (%s) — force charge/discharge will not work. "
                "Check that stanus74 exposes number/switch entities for passive mode.",
                control_missing,
            )
        return True

    def _discover_entities(self) -> None:
        """Discover entity IDs from the upstream config entry."""
        registry = er.async_get(self.hass)
        entries = er.async_entries_for_config_entry(registry, self._saj_entry_id)

        by_uid: dict[str, str] = {
            reg_entry.unique_id: reg_entry.entity_id
            for reg_entry in entries
            if reg_entry.unique_id and reg_entry.entity_id
        }

        _LOGGER.debug("SAJ H2 entity registry: %d entities found for entry %s", len(by_uid), self._saj_entry_id)

        for target, keys in _SENSOR_KEYS.items():
            if target in self._entity_map:
                continue
            for key in keys:
                # Prefer fast-poll over slow-poll for fresher readings
                fast = self._find_uid_suffix(by_uid, f"_fast_{key}")
                regular = self._find_uid_suffix(by_uid, f"_{key}", exclude=f"_fast_{key}")
                chosen = fast or regular
                if chosen:
                    self._entity_map[target] = chosen
                    _LOGGER.debug("SAJ H2: mapped %s → %s", target, chosen)
                    break
            else:
                _LOGGER.debug("SAJ H2: no entity found for sensor slot '%s' (tried: %s)", target, keys)

        for target, key in _NUMBER_KEYS.items():
            if target not in self._entity_map:
                entity_id = self._find_uid_suffix(by_uid, f"_{key}")
                if entity_id:
                    self._entity_map[target] = entity_id
                    _LOGGER.debug("SAJ H2: mapped %s → %s", target, entity_id)
                else:
                    _LOGGER.debug("SAJ H2: no number entity found for '%s' (suffix: _%s)", target, key)

        for target, suffix in _SWITCH_KEYS.items():
            if target not in self._entity_map:
                entity_id = self._find_uid_suffix(by_uid, suffix)
                if entity_id:
                    self._entity_map[target] = entity_id
                    _LOGGER.debug("SAJ H2: mapped %s → %s", target, entity_id)
                else:
                    _LOGGER.debug("SAJ H2: no switch entity found for '%s' (suffix: %s)", target, suffix)

    @staticmethod
    def _find_uid_suffix(
        uid_map: dict[str, str],
        suffix: str,
        exclude: str | None = None,
    ) -> str | None:
        for unique_id, entity_id in uid_map.items():
            if exclude and unique_id.endswith(exclude):
                continue
            if unique_id.endswith(suffix):
                return entity_id
        return None

    def _read_direction(self, key: str) -> str | None:
        """Return 'charging', 'discharging', 'idle', or None if unavailable."""
        entity_id = self._entity_map.get(key)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if not state or state.state in ("unavailable", "unknown", ""):
            return None
        val = state.state.lower().strip()
        # Numeric convention (SAJ): 1=discharging/export, -1=charging/import, 0=idle
        try:
            n = int(float(val))
            if n == 1:
                return "active_b"   # discharging / grid export
            if n == -1:
                return "active_a"   # charging / grid import
            return "idle"
        except (ValueError, TypeError):
            pass
        # Text convention
        if "discharg" in val or "export" in val or "output" in val:
            return "active_b"
        if "charg" in val or "import" in val or "input" in val:
            return "active_a"
        return "idle"

    def get_status(self) -> dict[str, Any]:
        """Read current SAJ state and return PowerSync-canonical fields."""
        # Battery power: SAJ typically reports absolute value + direction sensor
        battery_w_raw = self._read_float("battery_power") or 0.0
        dir_bat = self._read_direction("direction_battery")
        if dir_bat == "active_b":       # discharging → positive (convention: + = export from battery)
            battery_kw = abs(battery_w_raw) / 1000.0
        elif dir_bat == "active_a":     # charging → negative (convention: - = import to battery)
            battery_kw = -abs(battery_w_raw) / 1000.0
        else:
            # No direction info — trust the raw value's sign (some versions report signed)
            battery_kw = battery_w_raw / 1000.0

        # Grid power: positive = import, negative = export
        grid_w_raw = self._read_float("grid_power") or 0.0
        dir_grid = self._read_direction("direction_grid")
        if dir_grid == "active_b":      # exporting
            grid_kw = -abs(grid_w_raw) / 1000.0
        elif dir_grid == "active_a":    # importing
            grid_kw = abs(grid_w_raw) / 1000.0
        else:
            grid_kw = grid_w_raw / 1000.0

        return {
            "battery_level":              self._read_float("battery_level") or 0.0,
            "battery_power":              battery_kw,
            "grid_power":                 grid_kw,
            "solar_power":                max(0.0, (self._read_float("solar_power") or 0.0) / 1000.0),
            "load_power":                 max(0.0, (self._read_float("load_power") or 0.0) / 1000.0),
            "battery_temperature":        self._read_float("battery_temperature"),
            "app_mode":                   self._read_float("app_mode"),
            "battery_capacity_kwh":       self._battery_capacity_kwh,
            "battery_max_charge_power_w": self._read_float("battery_max_charge_power_w"),
            "battery_max_discharge_power_w": self._read_float("battery_max_discharge_power_w"),
        }

    async def force_charge(self, duration_minutes: int, power_w: int) -> bool:
        """Enable SAJ passive charge mode."""
        max_w = self._read_float("battery_max_charge_power_w")
        actual_w = min(float(power_w), max_w) if max_w and max_w > 0 else float(power_w)
        await self._set_number("charge_power", actual_w)
        await self._set_number("discharge_power_pct", 0)   # prevent discharge during forced charge
        await self._turn_off("discharge_switch")
        await self._turn_on("charge_switch")
        _LOGGER.info("SAJ H2 passive charge enabled at %.0fW", actual_w)
        return True

    async def force_discharge(self, duration_minutes: int, power_w: int) -> bool:
        """Enable SAJ passive discharge mode at full rate."""
        await self._set_number("discharge_power_pct", 1100)  # 110% of rated = full discharge rate
        await self._turn_off("charge_switch")
        await self._turn_on("discharge_switch")
        _LOGGER.info("SAJ H2 passive discharge enabled (full rate)")
        return True

    async def set_idle(self) -> bool:
        """Hold battery at current SOC — no discharge, no grid charge."""
        # discharge_power_pct=0 locks discharge; passive_enable=2 prevents grid charge schedule;
        # charge_switch must be on for passive_enable to take effect.
        await self._set_number("discharge_power_pct", 0)
        await self._set_number("passive_enable", 2)
        await self._turn_on("charge_switch")
        _LOGGER.info("SAJ H2 idle mode: battery locked at current SOC")
        return True

    async def restore_normal(self) -> bool:
        """Return to self-consumption — allow full charge/discharge in passive mode."""
        await self._set_number("charge_power", 0)
        await self._set_number("discharge_power_pct", 1100)  # re-enable full self-consumption discharge
        await self._turn_off("discharge_switch")
        # passive_enable must be set before turning on the switch — switch is ignored when enable=0
        await self._set_number("passive_enable", 2)
        await self._turn_on("charge_switch")
        _LOGGER.info("SAJ H2 restored to normal operation (passive_enable=2, discharge_pct=1100, charge_switch=on)")
        return True

    async def disconnect(self) -> None:
        """No persistent connection to close."""
        return None

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

    async def _set_number(self, key: str, value: float) -> None:
        entity_id = self._entity_map.get(key)
        if not entity_id:
            _LOGGER.warning("SAJ H2: cannot set %s — number entity not mapped", key)
            return
        await self.hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entity_id, "value": value},
            blocking=True,
        )

    async def _turn_on(self, key: str) -> None:
        entity_id = self._entity_map.get(key)
        if not entity_id:
            _LOGGER.debug("SAJ H2: cannot turn_on %s — switch entity not mapped", key)
            return
        await self.hass.services.async_call(
            "switch", "turn_on", {"entity_id": entity_id}, blocking=True,
        )

    async def _turn_off(self, key: str) -> None:
        entity_id = self._entity_map.get(key)
        if not entity_id:
            _LOGGER.debug("SAJ H2: cannot turn_off %s — switch entity not mapped", key)
            return
        await self.hass.services.async_call(
            "switch", "turn_off", {"entity_id": entity_id}, blocking=True,
        )
