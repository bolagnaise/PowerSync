"""SAJ H2 / HS2 battery bridge via the upstream saj_h2_modbus integration.

PowerSync does not open a second Modbus connection here. Instead it discovers
the entities created by `stanus74/home-assistant-saj-h2-modbus` and controls
them through Home Assistant services.

Control model (verified against stanus74 discussion #105 and live testing):

  Modes (`number.saj_app_mode_input`):
    0 = Self-Use (normal)
    1 = TOU (Time-of-Use schedule)
    2 = Backup
    3 = Passive

  force_charge — uses TOU mode (AppMode=1) with charge slot 7:
    - Mirrors force_discharge, but drives charge slot 7 instead of discharge
      slot 7. Passive mode can leave AppMode in Self-Use on some firmware even
      when the passive switch reports ON, so force charge uses the same
      schedule-driven path as force discharge.
    - PowerSync owns slot 7 (00:00-23:59, days=127, power=100). One-time
      bootstrap on every force_charge call (idempotent). Toggle bit 6 of
      `charge_time_enable_input` to start/stop.
    - Sequence:
        text.saj_charge7_start_time_time = "00:00"
        text.saj_charge7_end_time_time   = "23:59"
        number.saj_charge7_day_mask_input = 127
        number.saj_charge7_power_percent_input = 100
        cache current discharge_time_enable bitmask, then clear it (so a
          user-configured discharge slot in AppMode=1 doesn't fight us)
        number.saj_charge_time_enable_input = current_bitmask | (1<<6)
        number.saj_app_mode_input = 1
    - Restore:
        number.saj_charge_time_enable_input = current_bitmask & ~(1<<6)
        number.saj_discharge_time_enable_input = cached_discharge_enable
        number.saj_app_mode_input = 0

  force_discharge — uses TOU mode (AppMode=1) with discharge slot 7:
    - Passive discharge is load-following with a small grid-push margin
      (~500-1100 W above home load). It cannot push the battery to grid at
      a fixed high rate. TOU discharge is "fixed % of rated, PV added on
      top" — exactly what the LP wants for AEMO spike export.
    - PowerSync owns slot 7 (00:00-23:59, days=127, power derived from the
      requested watts and configured inverter rating). One-time bootstrap on
      every force_discharge call (idempotent). Toggle bit 6 of
      `discharge_time_enable_input` to start/stop.
    - Sequence:
        text.saj_discharge7_start_time_time = "00:00"
        text.saj_discharge7_end_time_time   = "23:59"
        number.saj_discharge7_day_mask_input = 127
        number.saj_discharge7_power_percent_input = requested percent
        cache current charge_time_enable bitmask, then clear it (so a
          user-configured charge slot in AppMode=1 doesn't fight us)
        number.saj_discharge_time_enable_input = current_bitmask | (1<<6)
        number.saj_app_mode_input = 1
    - Restore:
        number.saj_discharge_time_enable_input = current_bitmask & ~(1<<6)
        number.saj_charge_time_enable_input = cached_charge_enable
        number.saj_app_mode_input = 0

  Passive grid_charge_power / grid_discharge_power are NOT written.

  charging_control / discharging_control switches are not used for
  force_charge or force_discharge. They are turned OFF in restore_normal
  as a defensive measure in case a previous version of this controller
  (or a manual toggle in the SAJ Modbus UI) left them on.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)


# Maps internal slot → tuple of unique_id suffixes to try (first match wins).
# Fast-poll sensors are preferred over slow-poll for fresher readings.
# stanus74 integration uses camelCase keys: unique_id = f"{hub_name}_{key}" or f"{hub_name}_fast_{key}"
_SENSOR_KEYS: dict[str, tuple[str, ...]] = {
    "battery_level":               ("Bat1SOC", "batEnergyPercent"),
    "battery_power":               ("batteryPower",),
    # gridPower is exposed as "Grid Load Power" in Home Assistant and is the
    # net grid import/export leg. CT_GridPowerWatt can be present but report 0W
    # on some H2 installs; totalgridPower includes inverter-side power and is
    # not a net-grid signal.
    "grid_power":                  ("gridPower", "gridLoadPower", "CT_GridPowerWatt"),
    "solar_power":                 ("CT_PVPowerWatt", "pvPower"),
    "load_power":                  ("TotalLoadPower", "gridPower"),
    "battery_temperature":         ("BatTemp", "Bat1Temperature"),
    "battery_soh":                 ("Bat1SOH",),
    "app_mode":                    ("AppMode",),
    "battery_max_charge_power_w":  ("BatChargePower", "GridChargePower", "BatChaCurrLimit"),
    "battery_max_discharge_power_w": ("BatDischargePower", "GridDischargePower", "BatDisCurrLimit"),
    # Direction sensors — 1=discharging/export, -1=charging/import, 0=idle
    "direction_battery":           ("directionBattery",),
    "direction_grid":              ("directionGrid",),
    "pv1_power":                   ("PV1Power", "PV1PowerWatt", "pv1Power", "pv1_power"),
    "pv2_power":                   ("PV2Power", "PV2PowerWatt", "pv2Power", "pv2_power"),
    "pv3_power":                   ("PV3Power", "PV3PowerWatt", "pv3Power", "pv3_power"),
    "daily_solar_energy":          ("todayenergy", "powerCurrentDay", "PowerCurrentDay", "power_current_day"),
    "daily_grid_import":           ("feedin_today_energy", "feedInTodayEnergy", "FeedInTodayEnergy", "feed_in_today_energy"),
    "daily_grid_export":           ("sell_today_energy", "sellTodayEnergy", "SellTodayEnergy"),
    # Engagement signals — distinguish "battery converter active" (mode 2, R-phase ~240V)
    # from "low-SOC lockout" (mode 4, R-phase 0V). Without these the controller silently
    # writes Modbus commands that go nowhere because the inverter's converter is offline.
    "inverter_working_mode":       ("mpvmode",),
    "inverter_voltage_r":          ("RInvVolt",),
    # Bitmask sensors — read at runtime to OR/AND our slot bit cleanly without
    # clobbering user-configured slots (the *_input number entities can be stale
    # versus the actual register, so we trust the sensor for current state).
    "discharge_time_enable_bitmask": ("discharge_time_enable",),
    "charge_time_enable_bitmask":    ("charge_time_enable",),
}

# Maps internal slot → unique_id suffix for writable number entities.
# stanus74 constructs unique_id as f"{hub_name}_{key}_input" for all number entities.
# app_mode_writable is used by restore_normal() to force AppMode=0 (Self-Use)
# after a force charge/discharge, and by force modes to enter AppMode=1 (TOU).
# charge7_*_input / discharge7_*_input and charge_time_enable /
# discharge_time_enable drive the TOU-mode force paths. PowerSync owns slot 7.
_NUMBER_KEYS: dict[str, str] = {
    "charge_power":            "passive_bat_charge_power_input",
    "discharge_power":         "passive_bat_discharge_power_input",
    "app_mode_writable":       "app_mode_input",
    "charge7_day_mask":        "charge7_day_mask_input",
    "charge7_power_percent":   "charge7_power_percent_input",
    "discharge7_day_mask":     "discharge7_day_mask_input",
    "discharge7_power_percent": "discharge7_power_percent_input",
    "discharge_time_enable":   "discharge_time_enable_input",
    "charge_time_enable":      "charge_time_enable_input",
}

# Maps internal slot → unique_id suffix for writable text entities.
# stanus74 exposes slot start/end times under the `text.` domain (not number)
# with unique_id = f"{hub_name}_charge{N}_start_time" / "_end_time".
# We only need slot 7 since PowerSync owns it for force modes.
_TEXT_KEYS: dict[str, str] = {
    "charge7_start_time":    "charge7_start_time",
    "charge7_end_time":      "charge7_end_time",
    "discharge7_start_time": "discharge7_start_time",
    "discharge7_end_time":   "discharge7_end_time",
}

# Slot 7 lives at bit 6 of the enable bitmask (slot N → bit N-1).
_POWERSYNC_TOU_SLOT = 7
_POWERSYNC_TOU_BIT = 1 << (_POWERSYNC_TOU_SLOT - 1)  # 0b1000000 = 64
_POWERSYNC_DISCHARGE_BIT = _POWERSYNC_TOU_BIT
_POWERSYNC_CHARGE_BIT = _POWERSYNC_TOU_BIT

# AppMode values
_APP_MODE_SELF_USE = 0
_APP_MODE_TOU = 1
_APP_MODE_PASSIVE = 3

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
    _SWITCH_VERIFY_DELAY_SEC = 0.5
    _APP_MODE_VERIFY_DELAY_SEC = 0.5

    def __init__(
        self,
        hass: Any,
        saj_entry_id: str,
        battery_capacity_kwh: float = 10.0,
        min_soc_pct: float = 5.0,
        inverter_rated_kw: float = 10.0,
    ) -> None:
        self.hass = hass
        self._saj_entry_id = saj_entry_id
        self._battery_capacity_kwh = float(battery_capacity_kwh)
        self._min_soc_pct = float(min_soc_pct)
        self._inverter_rated_w = float(inverter_rated_kw) * 1000.0
        self._entity_map: dict[str, str] = {}
        # Cache of the user's charge_time_enable bitmask captured on
        # force_discharge entry, so restore_normal can put it back.
        # None when not currently in force_discharge.
        self._cached_charge_enable: int | None = None
        self._cached_discharge_enable: int | None = None
        # Which _SENSOR_KEYS candidate won for the "load_power" slot
        # (eg "TotalLoadPower" or the "gridPower" fallback). Set once in
        # _discover_entities(); used by get_status() to decide whether the
        # load_power entity can be trusted directly or needs the balance-
        # formula fallback (see get_status() for why).
        self._load_power_source: str | None = None

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
        passive_missing = [
            k for k in ("charge_power", "discharge_power", "passive_charge_control")
            if k not in self._entity_map
        ]
        if passive_missing:
            _LOGGER.warning(
                "SAJ H2: passive-mode entities not found (%s) — set_idle will not work. "
                "Check that stanus74 exposes switch and number entities for passive mode.",
                passive_missing,
            )
        tou_missing = [
            k for k in (
                "charge7_day_mask", "charge7_power_percent",
                "charge_time_enable", "charge7_start_time", "charge7_end_time",
                "discharge7_day_mask", "discharge7_power_percent",
                "discharge_time_enable", "app_mode_writable",
                "discharge7_start_time", "discharge7_end_time",
            )
            if k not in self._entity_map
        ]
        if tou_missing:
            _LOGGER.warning(
                "SAJ H2: TOU-mode entities not found (%s) — force charge/discharge may not work. "
                "Requires saj_h2_modbus version exposing charge7_* and discharge7_* "
                "number/text entities.",
                tou_missing,
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
                    if target == "load_power":
                        self._load_power_source = key
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

        for target, suffix in _TEXT_KEYS.items():
            if target not in self._entity_map:
                entity_id = self._find_uid_suffix(by_uid, suffix)
                if entity_id:
                    self._entity_map[target] = entity_id
                    _LOGGER.debug("SAJ H2: mapped %s → %s", target, entity_id)
                else:
                    _LOGGER.debug("SAJ H2: no text entity found for '%s' (suffix: %s)", target, suffix)

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

        # battery_max_*_power_w: derived from inverter rated × user-configured
        # power_limit percentage. The stanus74 BatChargePower / BatDischargePower
        # sensors return a percentage (0-100), not watts — using them directly
        # gave the LP wrong values and broke discharge/charge rate math.
        max_charge_pct = self._read_float("battery_max_charge_power_w")
        max_discharge_pct = self._read_float("battery_max_discharge_power_w")
        max_charge_w = (
            self._inverter_rated_w * (max_charge_pct / 100.0)
            if max_charge_pct is not None and 0 < max_charge_pct <= 110
            else self._inverter_rated_w
        )
        max_discharge_w = (
            self._inverter_rated_w * (max_discharge_pct / 100.0)
            if max_discharge_pct is not None and 0 < max_discharge_pct <= 110
            else self._inverter_rated_w
        )
        pv1_w = self._read_float("pv1_power") or 0.0
        pv2_w = self._read_float("pv2_power") or 0.0
        pv3_w = self._read_float("pv3_power") or 0.0
        pv_total_w = pv1_w + pv2_w + pv3_w
        solar_total_w = self._read_float("solar_power")
        if solar_total_w is None or pv_total_w > solar_total_w:
            solar_total_w = pv_total_w
        solar_kw = max(0.0, (solar_total_w or 0.0) / 1000.0)

        if self._load_power_source == "TotalLoadPower":
            load_kw = max(0.0, (self._read_float("load_power") or 0.0) / 1000.0)
        elif self._entity_state_available("grid_power") and self._entity_state_available("battery_power"):
            # TotalLoadPower isn't exposed on this install, so the
            # "load_power" slot fell back to the raw gridPower sensor —
            # that's the net grid leg, not house consumption. Reading it
            # directly bakes battery-charge power into home_load during grid
            # charging and under-reports load to ~0 during self-consumption
            # (see the gridPower comment on _SENSOR_KEYS above). Derive load
            # from the power balance instead, using the SIGNED conventions
            # computed above (grid: + import / - export; battery:
            # + discharge / - charge; solar_kw always >= 0):
            #     load = solar + battery + grid
            # Battery-charge power is inherently netted out because it's
            # negative in battery_kw.
            load_kw = max(0.0, solar_kw + battery_kw + grid_kw)
        else:
            # Last resort: neither TotalLoadPower nor the grid/battery legs
            # needed for the balance formula are available — fall back to
            # whatever "load_power" resolved to (raw gridPower, or nothing).
            load_kw = max(0.0, (self._read_float("load_power") or 0.0) / 1000.0)

        return {
            "battery_level":              self._read_float("battery_level") or 0.0,
            "battery_power":              battery_kw,
            "grid_power":                 grid_kw,
            "solar_power":                solar_kw,
            "load_power":                 load_kw,
            "pv1_power":                  max(0.0, pv1_w / 1000.0),
            "pv2_power":                  max(0.0, pv2_w / 1000.0),
            "pv3_power":                  max(0.0, pv3_w / 1000.0),
            "daily_solar_energy_kwh":     self._read_float("daily_solar_energy"),
            "daily_grid_import_kwh":      self._read_float("daily_grid_import"),
            "daily_grid_export_kwh":      self._read_float("daily_grid_export"),
            "battery_temperature":        self._read_float("battery_temperature"),
            "battery_soh":                self._read_float("battery_soh"),
            "app_mode":                   self._read_float("app_mode"),
            "battery_capacity_kwh":       self._battery_capacity_kwh,
            "battery_max_charge_power_w": max_charge_w,
            "battery_max_discharge_power_w": max_discharge_w,
            "inverter_rated_w":           self._inverter_rated_w,
        }

    def _check_passive_control_entities(self, operation: str) -> bool:
        """Return False and log an error if passive-mode entities are not usable.

        Used by set_idle. Force charge/discharge have their own TOU checks.
        """
        required = ("charge_power", "discharge_power", "passive_charge_control")
        missing = [
            k for k in required
            if not self._entity_map.get(k)
        ]
        if missing:
            _LOGGER.error(
                "SAJ H2: %s aborted — passive-mode entities not mapped: %s. "
                "Check that stanus74 exposes switch and number entities for passive mode.",
                operation, missing,
            )
            return False
        unavailable = [
            k for k in required
            if not self._entity_state_available(k)
        ]
        if unavailable:
            _LOGGER.error(
                "SAJ H2: %s aborted — passive-mode entities unavailable: %s",
                operation, unavailable,
            )
            return False
        return True

    def _check_tou_charge_control_entities(self, operation: str) -> bool:
        """Return False and log an error if charge-slot TOU entities are not mapped."""
        missing = [
            k for k in (
                "charge7_day_mask", "charge7_power_percent",
                "charge_time_enable", "app_mode_writable",
                "charge7_start_time", "charge7_end_time",
            )
            if not self._entity_map.get(k)
        ]
        if missing:
            _LOGGER.error(
                "SAJ H2: %s aborted — TOU charge entities not mapped: %s. "
                "Requires saj_h2_modbus version exposing charge7_* number/text entities.",
                operation, missing,
            )
            return False
        return True

    def _check_tou_discharge_control_entities(self, operation: str) -> bool:
        """Return False and log an error if discharge-slot TOU entities are not mapped."""
        missing = [
            k for k in (
                "discharge7_day_mask", "discharge7_power_percent",
                "discharge_time_enable", "app_mode_writable",
                "discharge7_start_time", "discharge7_end_time",
            )
            if not self._entity_map.get(k)
        ]
        if missing:
            _LOGGER.error(
                "SAJ H2: %s aborted — TOU-mode entities not mapped: %s. "
                "Requires saj_h2_modbus version exposing discharge7_* number/text entities.",
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
        """Force battery to charge from grid via TOU charge slot 7.

        Mirrors force_discharge: bootstrap slot 7 to run all day at 100%, enable
        only PowerSync's charge slot bit, and enter TOU mode. ``power_w`` is
        intentionally ignored so manual and optimizer charge requests use the
        inverter's full configured charge rate, just like force_discharge does.
        """
        if not self._check_tou_charge_control_entities("force_charge"):
            return False
        if not self._check_engaged("force_charge"):
            return False
        try:
            await self._clear_switch_controls_for_tou("force_charge")

            # Bootstrap (idempotent): make sure charge slot 7 spans the whole day at 100%.
            await self._set_text("charge7_start_time", "00:00")
            await self._set_text("charge7_end_time", "23:59")
            await self._set_number("charge7_day_mask", 127)
            await self._set_number("charge7_power_percent", 100)

            # Capture & clear discharge_time_enable so user discharge slots don't
            # contend with us in AppMode=1. Skip if we already cached it
            # (force_charge called twice without restore in between).
            if self._cached_discharge_enable is None:
                cached = self._read_int_sensor("discharge_time_enable_bitmask")
                self._cached_discharge_enable = cached if cached is not None else 0
                if "discharge_time_enable" in self._entity_map:
                    await self._set_number("discharge_time_enable", 0)

            # Set slot 7 bit on the charge_time_enable bitmask.
            current = self._read_int_sensor("charge_time_enable_bitmask") or 0
            await self._set_number(
                "charge_time_enable",
                current | _POWERSYNC_CHARGE_BIT,
            )

            # Enter TOU mode.
            await self._set_number("app_mode_writable", _APP_MODE_TOU)
        except Exception:
            _LOGGER.exception("SAJ H2: force_charge failed mid-sequence — attempting restore_normal")
            await self.restore_normal()
            return False
        _LOGGER.info(
            "SAJ H2 force_charge: TOU mode, slot 7 at 100%% of %.0f W rated",
            self._inverter_rated_w,
        )
        return True

    async def force_discharge(self, duration_minutes: int, power_w: int) -> bool:
        """Push battery to grid via TOU mode (AppMode=1) with discharge slot 7.

        Passive discharge is load-following with a small grid-push margin and
        cannot dump battery to grid at a fixed high rate. TOU mode adds PV on
        top of the configured target — exactly what's needed for AEMO spike
        export. PowerSync owns slot 7 here:

            slot 7: 00:00–23:59, days=127, power=requested percent

        We toggle bit 6 of `discharge_time_enable_input` to start/stop the slot,
        and switch AppMode to 1 (TOU). Charge slots are temporarily disabled
        for the duration so a user-configured charge slot in AppMode=1 doesn't
        fight us — the original bitmask is captured and restored on stop.

        `power_w` is converted to a discharge percentage using the configured
        inverter rated power. If no target is supplied, slot 7 runs at 100%.
        """
        if not self._check_tou_discharge_control_entities("force_discharge"):
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
        try:
            await self._clear_switch_controls_for_tou("force_discharge")

            # Bootstrap (idempotent): make sure slot 7 spans the whole day at 100%.
            target_percent = self._tou_power_percent(power_w)
            await self._set_text("discharge7_start_time", "00:00")
            await self._set_text("discharge7_end_time", "23:59")
            await self._set_number("discharge7_day_mask", 127)
            await self._set_number("discharge7_power_percent", target_percent)

            # Capture & clear charge_time_enable so user charge slots don't
            # contend with us in AppMode=1. Skip if we already cached it
            # (force_discharge called twice without restore in between).
            if self._cached_charge_enable is None:
                cached = self._read_int_sensor("charge_time_enable_bitmask")
                self._cached_charge_enable = (cached if cached is not None else 0) & ~_POWERSYNC_CHARGE_BIT
                if "charge_time_enable" in self._entity_map:
                    await self._set_number("charge_time_enable", 0)

            # Set slot 7 bit on the discharge_time_enable bitmask.
            current = self._read_int_sensor("discharge_time_enable_bitmask") or 0
            await self._set_number(
                "discharge_time_enable",
                current | _POWERSYNC_DISCHARGE_BIT,
            )

            # Enter TOU mode.
            await self._set_number("app_mode_writable", _APP_MODE_TOU)
        except Exception:
            _LOGGER.exception("SAJ H2: force_discharge failed mid-sequence — attempting restore_normal")
            await self.restore_normal()
            return False
        _LOGGER.info(
            "SAJ H2 force_discharge: TOU mode, slot 7 at %d%% of %.0f W rated",
            target_percent,
            self._inverter_rated_w,
        )
        return True

    def _tou_power_percent(self, power_w: int | float) -> int:
        """Convert requested watts to a SAJ TOU slot percentage."""
        try:
            requested_w = float(power_w)
        except (TypeError, ValueError):
            requested_w = 0.0

        if requested_w <= 0 or self._inverter_rated_w <= 0:
            return 100

        percent = math.ceil((requested_w / self._inverter_rated_w) * 100.0)
        return max(1, min(100, percent))

    async def set_idle(self) -> bool:
        """Hold battery at current SOC — no charge or discharge, grid serves home load.

        Enters passive charge mode with charge power zeroed. AppMode=3 (set by
        turning on passive_charge_control) prevents the TOU schedule from driving
        discharge. Because passive mode subtracts PV from the fixed target, a
        zero charge target also prevents PV from charging the battery — surplus
        PV exports to grid instead. This is intentional: idle means hold SOC.
        """
        if not self._check_passive_control_entities("set_idle"):
            return False
        if not self._check_engaged("set_idle"):
            return False
        try:
            if not await self._set_number("discharge_power", 0):
                await self.restore_normal()
                return False
            if not await self._set_number("charge_power", 0):
                await self.restore_normal()
                return False
            if not await self._turn_on("passive_charge_control", verify=True):
                await self.restore_normal()
                return False
            if not await self._ensure_app_mode(_APP_MODE_PASSIVE, "set_idle"):
                await self.restore_normal()
                return False
        except Exception:
            _LOGGER.exception("SAJ H2: set_idle failed mid-sequence — attempting restore_normal")
            await self.restore_normal()
            return False
        _LOGGER.info("SAJ H2 idle: passive charge mode with zero power — battery held")
        return True

    async def restore_normal(self) -> bool:
        """Return to Self-Use mode regardless of which path got us here.

        Handles both:
          - Passive entry (set_idle): zeros passive numbers and
            turns off the passive switches so stanus74's _deactivate_passive_mode
            restores the pre-passive AppMode capture.
          - TOU entry (force_charge / force_discharge): clears slot 7's enable
            bit and restores the user's cached opposing enable bitmask.

        Then explicitly writes AppMode=0 (Self-Use) so the user always lands in
        Self-Use after a force operation, regardless of stanus74's AppMode capture.
        """
        # Passive-mode unwind
        await self._set_number("charge_power", 0)
        await self._set_number("discharge_power", 0)
        await self._turn_off("passive_charge_control")
        await self._turn_off("passive_discharge_control")
        await self._turn_off("charging_control")
        await self._turn_off("discharging_control")

        # TOU-mode unwind: clear PowerSync slot 7 bits and restore user slots
        if "charge_time_enable" in self._entity_map:
            current = self._read_int_sensor("charge_time_enable_bitmask") or 0
            await self._set_number(
                "charge_time_enable",
                current & ~_POWERSYNC_CHARGE_BIT,
            )
        if "discharge_time_enable" in self._entity_map:
            current = self._read_int_sensor("discharge_time_enable_bitmask") or 0
            await self._set_number(
                "discharge_time_enable",
                current & ~_POWERSYNC_DISCHARGE_BIT,
            )
        if self._cached_charge_enable is not None:
            if "charge_time_enable" in self._entity_map:
                await self._set_number("charge_time_enable", self._cached_charge_enable)
            self._cached_charge_enable = None
        if self._cached_discharge_enable is not None:
            if "discharge_time_enable" in self._entity_map:
                await self._set_number("discharge_time_enable", self._cached_discharge_enable)
            self._cached_discharge_enable = None

        # Final mode flip
        await self._set_number("app_mode_writable", _APP_MODE_SELF_USE)
        _LOGGER.info("SAJ H2 restored to Self-Use mode (AppMode=0)")
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

    def _read_int_sensor(self, key: str) -> int | None:
        """Read a sensor entity value as an int (e.g. enable bitmask)."""
        val = self._read_float(key)
        if val is None:
            return None
        return int(val)

    def _entity_state_available(self, key: str) -> bool:
        entity_id = self._entity_map.get(key)
        if not entity_id:
            return False
        state = self.hass.states.get(entity_id)
        if not state:
            return False
        value = str(state.state).lower().strip()
        return value not in ("unavailable", "unknown", "")

    def _switch_is_on(self, key: str) -> bool:
        entity_id = self._entity_map.get(key)
        if not entity_id:
            return False
        state = self.hass.states.get(entity_id)
        return bool(state and str(state.state).lower().strip() == "on")

    async def _clear_switch_controls_for_tou(self, operation: str) -> None:
        """Clear stale passive/manual controls before TOU slot control takes over."""
        for key in (
            "passive_charge_control",
            "passive_discharge_control",
            "charging_control",
            "discharging_control",
        ):
            if key in self._entity_map and self._switch_is_on(key):
                _LOGGER.debug("SAJ H2: %s turning off stale %s before TOU mode", operation, key)
                await self._turn_off(key)

    async def _ensure_app_mode(self, expected_mode: int, operation: str) -> bool:
        """Drive and verify the inverter AppMode when the upstream switch does not."""
        if "app_mode_writable" in self._entity_map:
            if not await self._set_number("app_mode_writable", expected_mode):
                return False
            await asyncio.sleep(self._APP_MODE_VERIFY_DELAY_SEC)

        current_mode = self._read_float("app_mode")
        if current_mode is None:
            if "app_mode_writable" not in self._entity_map:
                _LOGGER.warning(
                    "SAJ H2: %s cannot verify AppMode — no app_mode sensor or writable entity mapped",
                    operation,
                )
            return True

        if int(current_mode) == expected_mode:
            return True

        _LOGGER.error(
            "SAJ H2: %s did not enter expected AppMode %s; current AppMode is %s",
            operation, expected_mode, current_mode,
        )
        return False

    async def _set_number(self, key: str, value: float) -> bool:
        entity_id = self._entity_map.get(key)
        if not entity_id:
            _LOGGER.warning("SAJ H2: cannot set %s — number entity not mapped", key)
            return False
        await self.hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entity_id, "value": value},
            blocking=True,
        )
        if not self._entity_state_available(key):
            _LOGGER.error(
                "SAJ H2: number %s became unavailable after set_value",
                entity_id,
            )
            return False
        return True

    async def _set_text(self, key: str, value: str) -> None:
        entity_id = self._entity_map.get(key)
        if not entity_id:
            _LOGGER.warning("SAJ H2: cannot set %s — text entity not mapped", key)
            return
        await self.hass.services.async_call(
            "text",
            "set_value",
            {"entity_id": entity_id, "value": value},
            blocking=True,
        )

    async def _turn_on(self, key: str, verify: bool = False) -> bool:
        entity_id = self._entity_map.get(key)
        if not entity_id:
            _LOGGER.debug("SAJ H2: cannot turn_on %s — switch entity not mapped", key)
            return False
        await self.hass.services.async_call(
            "switch", "turn_on", {"entity_id": entity_id}, blocking=True,
        )
        if not verify:
            return True
        if self._switch_is_on(key):
            return True
        await asyncio.sleep(self._SWITCH_VERIFY_DELAY_SEC)
        if self._switch_is_on(key):
            return True
        _LOGGER.error(
            "SAJ H2: switch.turn_on for %s completed but state is not on",
            entity_id,
        )
        return False

    async def _turn_off(self, key: str) -> bool:
        entity_id = self._entity_map.get(key)
        if not entity_id:
            _LOGGER.debug("SAJ H2: cannot turn_off %s — switch entity not mapped", key)
            return False
        await self.hass.services.async_call(
            "switch", "turn_off", {"entity_id": entity_id}, blocking=True,
        )
        return True
