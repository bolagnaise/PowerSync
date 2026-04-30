"""SAJ H2 / HS2 battery bridge via the upstream saj_h2_modbus integration.

PowerSync does not open a second Modbus connection here. Instead it discovers
the entities created by `stanus74/home-assistant-saj-h2-modbus` and controls
them through Home Assistant services.

Control model (verified against stanus74 source and live testing):
  - Normal mode: AppMode=0, charging_control=OFF, discharging_control=OFF,
    passive_charge_control=OFF, passive_discharge_control=OFF.
  - Passive mode is entered/exited via the switch entities, NOT the number
    entities for passive_charge_enable or app_mode directly.
    Reason: stanus74's switch entities call _activate_passive_mode() /
    _deactivate_passive_mode() which capture/restore AppMode and write AppMode=3
    atomically. Writing the passive_charge_enable number entity directly does NOT
    touch AppMode — the inverter would receive passive_enable before AppMode=3.
    Also, the "app_mode" key in _SENSOR_KEYS maps the sensor entity first, so any
    attempt to write via a same-keyed number entity silently hits the sensor entity_id
    and is ignored by HA's number.set_value service.
  - passive_bat_charge_power / passive_bat_discharge_power set the power target
    on a 0–1000 scale where 1000 = 100% of the inverter's rated capacity
    (e.g. 150 = 15% = 1500 W on a 10 kW-rated inverter). 1100 = inverter hardware
    max (stanus74 sentinel — bypasses the percentage cap).
  - passive_grid_charge_power has NO effect on charging behavior (confirmed by
    stanus74 author and independent testers). It is not written.
  - passive_grid_discharge_power DOES limit grid export during passive discharge
    mode. Set to the same scale value as the battery discharge target.
  - Correct sequence:
      Enter: set power number entities → turn ON passive_charge/discharge_control switch
      Exit:  zero power number entities → turn OFF both passive switches
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
    "solar_power":                 ("CT_PVPowerWatt", "pvPower"),
    "load_power":                  ("TotalLoadPower", "gridPower"),
    "battery_temperature":         ("BatTemp", "Bat1Temperature"),
    "app_mode":                    ("AppMode",),
    "battery_max_charge_power_w":  ("BatChargePower", "GridChargePower", "BatChaCurrLimit"),
    "battery_max_discharge_power_w": ("BatDischargePower", "GridDischargePower", "BatDisCurrLimit"),
    # Direction sensors — 1=discharging/export, -1=charging/import, 0=idle
    "direction_battery":           ("directionBattery",),
    "direction_grid":              ("directionGrid",),
    # Engagement signals — distinguish "battery converter active" (mode 2, R-phase ~240V)
    # from "low-SOC lockout" (mode 4, R-phase 0V). Without these the controller silently
    # writes Modbus commands that go nowhere because the inverter's converter is offline.
    "inverter_working_mode":       ("mpvmode",),
    "inverter_voltage_r":          ("RInvVolt",),
}

# Maps internal slot → unique_id suffix for writable number entities.
# stanus74 constructs unique_id as f"{hub_name}_{key}_input" for all number entities.
# NOTE: passive_charge_enable and app_mode are intentionally absent — passive mode
# entry/exit is managed via the switch entities below, which handle AppMode internally.
# passive_grid_charge_power is absent — confirmed no effect on charging behavior.
_NUMBER_KEYS: dict[str, str] = {
    "charge_power":         "passive_bat_charge_power_input",
    "discharge_power":      "passive_bat_discharge_power_input",
    "grid_discharge_power": "passive_grid_discharge_power_input",
}

# Maps internal slot → unique_id suffix for writable switch entities.
# stanus74 constructs unique_id as f"{hub_name}_{switch_type}{unique_id_suffix}".
# passive_charge_control ON  → hub.set_passive_mode(2) → AppMode=3 + passive_enable=2
# passive_charge_control OFF → hub.set_passive_mode(0) → passive_enable=0 + AppMode restored
# passive_discharge_control ON  → hub.set_passive_mode(1) → AppMode=3 + passive_enable=1
# passive_discharge_control OFF → hub.set_passive_mode(0) → passive_enable=0 + AppMode restored
_SWITCH_KEYS: dict[str, str] = {
    "charging_control":         "charging_control",
    "discharging_control":      "discharging_control",
    "passive_charge_control":   "passive_charge_control",
    "passive_discharge_control": "passive_discharge_control",
}


class SajH2BatteryController:
    """Bridge controller for SAJ H2 entities exposed by saj_h2_modbus."""

    # SOC band guarding force_discharge. The SAJ inverter's discharge_depth register
    # cannot be written reliably from the stanus74 integration (writes silently
    # ignored on tested firmware), so the user-facing min_soc must be enforced in
    # software here. The +1% buffer keeps us off the inverter's own low-SOC lockout
    # which trips at the register floor (typically 5%) and requires a power-cycle.
    _MIN_SOC_BUFFER_PCT = 1.0

    # Minimum R-phase inverter voltage that indicates the battery DC-DC converter
    # is actually engaged. When the converter is offline the register reads 0V even
    # though the on-grid pass-through is at 235V — so we test the inverter (battery)
    # leg specifically, not the grid leg.
    _MIN_ENGAGED_INV_VOLTAGE = 50.0

    def __init__(
        self,
        hass: Any,
        saj_entry_id: str,
        battery_capacity_kwh: float = 10.0,
        min_soc_pct: float = 5.0,
    ) -> None:
        self.hass = hass
        self._saj_entry_id = saj_entry_id
        self._battery_capacity_kwh = float(battery_capacity_kwh)
        self._min_soc_pct = float(min_soc_pct)
        self._entity_map: dict[str, str] = {}

    def set_min_soc_pct(self, min_soc_pct: float) -> None:
        """Update the software-enforced discharge floor (called when user changes it)."""
        self._min_soc_pct = float(min_soc_pct)

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
        control_missing = [
            k for k in ("charge_power", "discharge_power", "passive_charge_control", "passive_discharge_control")
            if k not in self._entity_map
        ]
        if control_missing:
            _LOGGER.warning(
                "SAJ H2: control entities not found (%s) — force charge/discharge will not work. "
                "Check that stanus74 exposes switch and number entities for passive mode.",
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
        """Return 'active_b' (discharge/export), 'active_a' (charge/import), 'idle', or None."""
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

    def _check_passive_control_entities(self, operation: str) -> bool:
        """Return False and log an error if the passive mode switch entities are not mapped."""
        missing = [
            k for k in ("charge_power", "passive_charge_control", "passive_discharge_control")
            if not self._entity_map.get(k)
        ]
        if missing:
            _LOGGER.error(
                "SAJ H2: %s aborted — control entities not mapped: %s. "
                "Check that stanus74 exposes switch and number entities for passive mode.",
                operation, missing,
            )
            return False
        return True

    def _check_engaged(self, operation: str) -> bool:
        """Refuse Modbus commands when the inverter's battery converter is offline.

        Real low-SOC lockout: working_mode goes to 4 AND R-phase inverter voltage
        drops to 0V together. The DC-DC converter is offline and the only fix is a
        physical power-cycle.

        Either signal alone is unreliable: working_mode oscillates 2↔4 every minute
        or two on healthy systems, and stanus74's RInvVolt register sometimes reads
        0V even while the inverter is exporting normally on certain firmwares.
        Refuse only when BOTH signals say lockout.
        """
        wm = self._read_float("inverter_working_mode")
        rv = self._read_float("inverter_voltage_r")
        mode_ok = wm is None or int(wm) == 2
        voltage_ok = rv is None or rv >= self._MIN_ENGAGED_INV_VOLTAGE
        if mode_ok or voltage_ok:
            return True
        _LOGGER.error(
            "SAJ H2: %s refused — inverter working_mode=%s and R-phase voltage %.1fV "
            "(need ≥%.0fV). Battery converter offline (low-SOC lockout). Power-cycle required.",
            operation, int(wm), rv, self._MIN_ENGAGED_INV_VOLTAGE,
        )
        return False

    async def force_charge(self, duration_minutes: int, power_w: int) -> bool:
        """Force battery to charge from grid.

        Passive charge mode: sets charge power target then turns on the
        passive_charge_control switch, which triggers stanus74's
        _activate_passive_mode() — capturing the current AppMode and setting
        AppMode=3 atomically.
        PV power is counted toward the fixed target, reducing grid draw proportionally.
        passive_grid_charge_power is not written — it has no effect on charging
        behavior (confirmed stanus74 discussions/105).
        """
        if not self._check_passive_control_entities("force_charge"):
            return False
        if not self._check_engaged("force_charge"):
            return False
        pct = self._power_to_scaled_percent(power_w, self._read_float("battery_max_charge_power_w"))
        try:
            await self._set_number("discharge_power", 0)
            await self._set_number("grid_discharge_power", 0)
            await self._set_number("charge_power", pct)
            await self._turn_on("passive_charge_control")
        except Exception:
            _LOGGER.exception("SAJ H2: force_charge failed mid-sequence — attempting restore_normal")
            await self.restore_normal()
            return False
        _LOGGER.info("SAJ H2 force charge: passive charge mode at %d/1000", pct)
        return True

    async def force_discharge(self, duration_minutes: int, power_w: int) -> bool:
        """Enable passive discharge mode.

        Sets discharge and grid_discharge power targets then turns on the
        passive_discharge_control switch, which triggers stanus74's
        _activate_passive_mode() — setting AppMode=3 atomically.
        SAJ H2 passive discharge is load-following — the battery covers home
        load first and exports surplus. It does not unconditionally push maximum
        power to grid.
        """
        if not self._check_passive_control_entities("force_discharge"):
            return False
        if not self._check_engaged("force_discharge"):
            return False
        soc = self._read_float("battery_level")
        floor = self._min_soc_pct + self._MIN_SOC_BUFFER_PCT
        if soc is not None and soc <= floor:
            _LOGGER.warning(
                "SAJ H2: force_discharge refused — SOC %.1f%% at/below software floor %.1f%% "
                "(min_soc=%.1f%% + %.1f%% buffer). Holding battery to avoid low-SOC lockout.",
                soc, floor, self._min_soc_pct, self._MIN_SOC_BUFFER_PCT,
            )
            return False
        pct = self._power_to_scaled_percent(power_w, self._read_float("battery_max_discharge_power_w"))
        try:
            await self._set_number("charge_power", 0)
            await self._set_number("discharge_power", pct)
            await self._set_number("grid_discharge_power", pct)
            await self._turn_on("passive_discharge_control")
        except Exception:
            _LOGGER.exception("SAJ H2: force_discharge failed mid-sequence — attempting restore_normal")
            await self.restore_normal()
            return False
        _LOGGER.info("SAJ H2 force discharge: passive discharge mode at %d/1000", pct)
        return True

    async def set_idle(self) -> bool:
        """Hold battery at current SOC — no charge or discharge, grid serves home load.

        Enters passive charge mode with charge power zeroed. AppMode=3 (set by
        turning on passive_charge_control) prevents the TOU schedule from driving
        discharge. Because passive mode counts PV toward the fixed power target,
        a zero charge target also prevents PV from charging the battery — surplus
        PV exports to grid instead. This is intentional: idle means hold SOC.
        """
        if not self._check_passive_control_entities("set_idle"):
            return False
        if not self._check_engaged("set_idle"):
            return False
        try:
            await self._set_number("discharge_power", 0)
            await self._set_number("grid_discharge_power", 0)
            await self._set_number("charge_power", 0)
            await self._turn_on("passive_charge_control")
        except Exception:
            _LOGGER.exception("SAJ H2: set_idle failed mid-sequence — attempting restore_normal")
            await self.restore_normal()
            return False
        _LOGGER.info("SAJ H2 idle: passive charge mode with zero power — battery held")
        return True

    async def restore_normal(self) -> bool:
        """Return to normal self-consumption mode.

        Zeros passive power registers, turns off both passive switches (stanus74's
        _deactivate_passive_mode() restores the pre-passive AppMode automatically),
        and ensures charging_control and discharging_control are off.
        """
        await self._set_number("charge_power", 0)
        await self._set_number("discharge_power", 0)
        await self._set_number("grid_discharge_power", 0)
        await self._turn_off("passive_charge_control")
        await self._turn_off("passive_discharge_control")
        await self._turn_off("charging_control")
        await self._turn_off("discharging_control")
        _LOGGER.info("SAJ H2 restored to normal operation")
        return True

    async def disconnect(self) -> None:
        """No persistent connection to close."""
        return None

    @staticmethod
    def _power_to_scaled_percent(requested_w: int | float, max_w: float | None) -> int:
        """Convert watts to SAJ's 0–1100 scale.

        0–1000 = 0–100% of the inverter's rated capacity (percentage × 10).
        1100   = stanus74 sentinel "no explicit limit" — inverter runs at its
                 own hardware ceiling. This is the safe default; field-confirmed
                 that owners have run at 1100 long-term without trips.
        """
        if requested_w and requested_w > 0 and max_w and max_w > 0:
            return max(0, min(1100, int(round((requested_w / max_w) * 1000))))
        return 1100

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
