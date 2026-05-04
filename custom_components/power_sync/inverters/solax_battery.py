"""Solax hybrid battery controller using wills106/homeassistant-solax-modbus entities.

Requires the solax_modbus integration (HACS) to be installed and running.
PowerSync reads sensor states and writes number/select entities via HA service calls
- no direct Modbus connection is opened here (avoids the one-master-at-a-time
restriction of the Solax PocketWiFi dongle).

Supported: Gen4, Gen5, Gen6 Hybrid and AC Retro-Fit (X1 and X3 families).
Gen2/Gen3 use a different control model (Force Time Use windows) and are
handled when those entities are present instead of the Gen4+ manual-mode
entities.

Sign conventions (PowerSync internal):
  grid_power_kw   : positive = importing, negative = exporting
  battery_power_kw: positive = discharging, negative = charging
  solar_power_kw  : always >= 0

Solax entity conventions (wills106):
  sensor.*_measured_power      : positive = exporting, negative = importing
  sensor.*_battery_power_charge: positive = charging (OPPOSITE -> negate)
"""

from datetime import timedelta
import logging
import re
from typing import Any

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)


# wills106 entity suffixes keyed by role. Some installs expose inverter-prefixed
# variants, and older firmware/plugin combinations may still surface
# `manual_mode` rather than `manual_mode_select`.
_READ_ENTITIES = {
    "battery_level": (
        "battery_capacity",
        "battery_1_capacity",
        "bms_battery_capacity",
    ),                                                  # %
    "battery_power_raw": (
        "battery_power_charge",
        "total_battery_power_charge",
        "battery_1_power_charge",
        "energy_dashboard_solax_battery_power",
    ),                                                  # W, +charge/-discharge
    "grid_power": (
        "measured_power",
        "meter_2_measured_power",
        "energy_dashboard_solax_grid_power",
    ),                                                  # W, measured_power: +export/-import
    "pv1_power": (
        "pv_power_1",
        "energy_dashboard_solax_pv_power_1",
    ),                                                  # W
    "pv2_power": (
        "pv_power_2",
        "energy_dashboard_solax_pv_power_2",
    ),                                                  # W
    "pv3_power": (
        "pv_power_3",
        "energy_dashboard_solax_pv_power_3",
    ),                                                  # W
    "pv1_voltage": ("pv_voltage_1",),                  # V
    "pv2_voltage": ("pv_voltage_2",),                  # V
    "pv3_voltage": ("pv_voltage_3",),                  # V
    "pv1_current": ("pv_current_1",),                  # A
    "pv2_current": ("pv_current_2",),                  # A
    "pv3_current": ("pv_current_3",),                  # A
    "solar_power": (
        "solar_power",
        "pv_power_total",
        "pv_total_power",
        "energy_dashboard_solax_solar_power",
    ),                                                  # W, Energy Dashboard / AC Retro-Fit
    "load_power": (
        "house_load",
        "house_load_alt",
        "energy_dashboard_solax_home_consumption_power",
        "inverter_power",
    ),                                                  # W
    "battery_temp": ("battery_temperature",),          # C
}

_WRITE_ENTITIES = {
    "charger_use_mode": (
        "charger_use_mode",
        "inverter_charger_use_mode",
    ),
    "manual_mode_select": (
        "manual_mode_select",
        "manual_mode",
        "inverter_manual_mode_select",
        "inverter_manual_mode",
    ),
    "manual_mode_control": (
        "manual_mode_control",
        "inverter_manual_mode_control",
    ),
    "remotecontrol_power_control": (
        "remotecontrol_power_control",
        "remotecontrol_power_control_mode_1",
        "remote_control_power_control",
        "inverter_remotecontrol_power_control",
        "inverter_remotecontrol_power_control_mode_1",
        "inverter_remote_control_power_control",
    ),
    "remotecontrol_set_type": (
        "remotecontrol_set_type",
        "remotecontrol_set_type_mode_1",
        "remotecontrol_set_type_mode_1_9",
        "remote_control_set_type",
        "inverter_remotecontrol_set_type",
        "inverter_remotecontrol_set_type_mode_1",
        "inverter_remotecontrol_set_type_mode_1_9",
        "inverter_remote_control_set_type",
    ),
    "remotecontrol_active_power": (
        "remotecontrol_active_power",
        "remotecontrol_active_power_mode_1",
        "remotecontrol_active_power_mode_1_direct",
        "remote_control_active_power",
        "inverter_remotecontrol_active_power",
        "inverter_remotecontrol_active_power_mode_1",
        "inverter_remotecontrol_active_power_mode_1_direct",
        "inverter_remote_control_active_power",
    ),
    "remotecontrol_reactive_power": (
        "remotecontrol_reactive_power",
        "remotecontrol_reactive_power_mode_1",
        "remotecontrol_reactive_power_mode_1_direct",
        "remote_control_reactive_power",
        "inverter_remotecontrol_reactive_power",
        "inverter_remotecontrol_reactive_power_mode_1",
        "inverter_remotecontrol_reactive_power_mode_1_direct",
        "inverter_remote_control_reactive_power",
    ),
    "remotecontrol_duration": (
        "remotecontrol_duration",
        "remotecontrol_duration_mode_1",
        "remotecontrol_duration_mode_1_8",
        "remote_control_duration",
        "inverter_remotecontrol_duration",
        "inverter_remotecontrol_duration_mode_1",
        "inverter_remotecontrol_duration_mode_1_8",
        "inverter_remote_control_duration",
        "remotecontrol_duration_mode_1_9",
        "inverter_remotecontrol_duration_mode_1_9",
        "remotecontrol_duration_mode_1_direct",
        "inverter_remotecontrol_duration_mode_1_direct",
    ),
    "remotecontrol_autorepeat_duration": (
        "remotecontrol_autorepeat_duration",
        "remotecontrol_autorepeat_duration_mode_1",
        "remote_control_autorepeat_duration",
        "inverter_remotecontrol_autorepeat_duration",
        "inverter_remotecontrol_autorepeat_duration_mode_1",
        "inverter_remote_control_autorepeat_duration",
        "remotecontrol_autorepeat_duration_mode_1_9",
        "inverter_remotecontrol_autorepeat_duration_mode_1_9",
    ),
    "remotecontrol_trigger": (
        "remotecontrol_trigger",
        "remotecontrol_trigger_mode_1",
        "remotecontrol_trigger_mode_1_7",
        "remote_control_trigger",
        "inverter_remotecontrol_trigger",
        "inverter_remotecontrol_trigger_mode_1",
        "inverter_remotecontrol_trigger_mode_1_7",
        "inverter_remote_control_trigger",
    ),
    "allow_grid_charge": ("allow_grid_charge",),
    "charge_start_1": ("charge_start_1",),
    "charge_end_1": ("charge_end_1",),
    "charge_current": ("battery_charge_max_current",),                             # number, A
    "discharge_current": ("battery_discharge_max_current",),                       # number, A
    "backup_reserve": (
        "battery_minimum_capacity",       # older wills106 naming
        "battery_minimum_capacity_grid_tied",  # Gen2/Gen3 grid-tied floor SOC
        "selfuse_discharge_min_soc",      # Gen4/Gen5/Gen6: self-use mode floor SOC
        "selfuse_backup_soc",             # Gen4/Gen5/Gen6: self-use backup reservation
    ),
    "export_limit": ("export_control_user_limit",),                                # number, W
    "export_duration": ("export_duration",),
    "grid_export_button": ("grid_export",),
    "grid_export_limit": ("grid_export_limit",),                                   # number, W, Gen3 export
    "grid_tied_min_soc": ("battery_minimum_capacity_grid_tied",),
    "forcetime_period_1_max_capacity": ("forcetime_period_1_max_capacity",),
}


# Expected select options (wills106 label strings for Gen4/Gen5/Gen6)
_MODE_SELF_USE = "Self Use Mode"
_MODE_FEEDIN = "Feedin Priority Mode"
_MODE_BACKUP = "Back Up Mode"
_MODE_MANUAL = "Manual Mode"
_MODE_SMART = "Smart Schedule"
_MODE_FORCE_TIME_CANDIDATES = ("Force Time Use", "Force Time Use Mode", "Force Time")

_MANUAL_STOP = "Stop Charge and Discharge"
_MANUAL_CHARGE = "Force Charge"
_MANUAL_DISCHARGE = "Force Discharge"
_MANUAL_CONTROL_ON = "On"
_MANUAL_CONTROL_OFF = "Off"
_REMOTE_CONTROL_BATTERY = ("Enabled Battery Control",)
_REMOTE_CONTROL_SELF_USE = ("Enabled Self Use",)
_REMOTE_CONTROL_DISABLED = ("Disabled", "Disable", "Off")
_REMOTE_CONTROL_SET = ("Set", "Update")
_REMOTE_CONTROL_SLOT_SECONDS = 20
_ALLOW_GRID_PERIOD_1_CANDIDATES = (
    "Period 1 Allowed",
    "Period 1 Enabled",
    "Period 1 Enable",
)
_EXPORT_DURATION_DEFAULT_CANDIDATES = ("Default", "Disable", "Disabled", "Off")


# PowerSync operation-mode -> Solax charger_use_mode option
_OP_MODE_MAP = {
    "self_consumption": _MODE_SELF_USE,
    "autonomous": _MODE_SMART,
    "backup": _MODE_BACKUP,
    "feed_in": _MODE_FEEDIN,
}


class SolaxBatteryController:
    """Battery controller for Solax Hybrid via homeassistant-solax-modbus."""

    def __init__(
        self,
        hass: Any,
        entity_prefix: str = "solax",
        solax_entry_id: str | None = None,
        battery_nominal_v: float = 51.2,
        max_charge_current_a: float = 25.0,
        max_discharge_current_a: float = 25.0,
    ) -> None:
        self.hass = hass
        self._prefix = entity_prefix.strip()
        self._solax_entry_id = (solax_entry_id or "").strip()
        self._nominal_v = battery_nominal_v
        self._max_charge_a = max_charge_current_a
        self._max_discharge_a = max_discharge_current_a
        self._timer_cancel = None
        self._entity_map: dict[str, str] = {}
        self._control_profile = "unknown"
        self._saved_force_time_states: dict[str, str] | None = None

    async def _ensure_connected(self) -> None:
        """Lazily discover entities before runtime service calls."""
        if not self._entity_map or self._control_profile == "unknown":
            await self.connect()

    def _ensure_entity_map(self) -> None:
        """Populate read mappings for status reads even before connect()."""
        if not self._entity_map:
            self._discover_entities()

    # -- Entity ID helpers -------------------------------------------------

    def _sensor(self, suffix: str) -> str:
        return f"sensor.{self._prefix}_{suffix}"

    def _number(self, suffix: str) -> str:
        return f"number.{self._prefix}_{suffix}"

    def _select(self, suffix: str) -> str:
        return f"select.{self._prefix}_{suffix}"

    @staticmethod
    def _find_entity_by_suffix(
        entity_ids: list[str],
        domain: str,
        suffixes: tuple[str, ...],
    ) -> str | None:
        domain_prefix = f"{domain}."
        for suffix in suffixes:
            tail = f"_{suffix}"
            for entity_id in entity_ids:
                if entity_id.startswith(domain_prefix) and entity_id.endswith(tail):
                    return entity_id
        return None

    # -- Prefix discovery --------------------------------------------------

    @staticmethod
    def discover_prefixes(hass: Any) -> list[str]:
        """Scan HA states for likely wills106 hybrid inverter prefixes."""
        batt_suffix = f"_{_READ_ENTITIES['battery_level'][0]}"
        mode_suffixes = [f"_{v}" for v in _WRITE_ENTITIES["charger_use_mode"]]

        mode_prefixes = set()
        for state in hass.states.async_all("select"):
            eid = state.entity_id
            for mode_suffix in mode_suffixes:
                if eid.endswith(mode_suffix):
                    prefix = eid[len("select."):-len(mode_suffix)]
                    if prefix:
                        mode_prefixes.add(prefix)
                    break

        prefixes = []
        for prefix in mode_prefixes:
            if hass.states.get(f"sensor.{prefix}{batt_suffix}") is not None:
                prefixes.append(prefix)
        return sorted(prefixes)

    # -- Connect / validate ------------------------------------------------

    async def connect(self) -> bool:
        """Validate that required Solax entities exist."""
        self._discover_entities()

        base_required = (
            "battery_level",
            "battery_power_raw",
            "grid_power",
            "charger_use_mode",
            "charge_current",
            "discharge_current",
        )
        manual_required = ("manual_mode_select",)
        force_time_required = (
            "allow_grid_charge",
            "charge_start_1",
            "charge_end_1",
            "export_duration",
            "grid_export_button",
            "grid_export_limit",
            "grid_tied_min_soc",
        )
        remote_control_required = (
            "remotecontrol_power_control",
            "remotecontrol_active_power",
            "remotecontrol_autorepeat_duration",
            "remotecontrol_trigger",
        )

        base_missing = self._missing_keys(base_required)
        manual_missing = self._missing_keys(manual_required)
        force_time_missing = self._missing_keys(force_time_required)
        remote_control_missing = self._missing_keys(remote_control_required)

        if not base_missing and not remote_control_missing:
            self._control_profile = "remote_control"
            missing = []
        elif not base_missing and not manual_missing:
            self._control_profile = "manual"
            missing = []
        elif not base_missing and not force_time_missing:
            self._control_profile = "force_time"
            missing = []
        else:
            self._control_profile = "unknown"
            profile_missing = min(
                (manual_missing, remote_control_missing, force_time_missing),
                key=len,
            )
            missing = [*base_missing, *profile_missing]

        if missing:
            missing_ids = [
                self._entity_map.get(key)
                or self._expected_entity_hint(key)
                or key
                for key in missing
            ]
            raise ValueError(f"solax_missing_entities:{','.join(missing_ids)}")

        _LOGGER.info(
            "Solax entities validated (%s, %s profile, %d mapped)",
            (
                f"config_entry={self._solax_entry_id}"
                if self._solax_entry_id
                else f"prefix={self._prefix}"
            ),
            self._control_profile,
            len(self._entity_map),
        )
        return True

    # -- Status ------------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        """Read current inverter state and return PowerSync-canonical dict."""
        self._ensure_entity_map()
        soc = self._read_float("battery_level") or 0.0
        bat_w_raw = self._read_float("battery_power_raw") or 0.0
        grid_w = self._read_float("grid_power") or 0.0
        pv1_w = self._read_float("pv1_power") or 0.0
        pv2_w = self._read_float("pv2_power") or 0.0
        pv3_w = self._read_float("pv3_power") or 0.0
        pv1_v = self._read_float("pv1_voltage")
        pv2_v = self._read_float("pv2_voltage")
        pv3_v = self._read_float("pv3_voltage")
        pv1_a = self._read_float("pv1_current")
        pv2_a = self._read_float("pv2_current")
        pv3_a = self._read_float("pv3_current")
        solar_total_w = self._read_float("solar_power")
        load_w = self._read_float("load_power") or 0.0
        bat_temp = self._read_float("battery_temp")

        # wills106 battery_power_charge: +charge, -discharge
        # PowerSync battery_power_kw:    +discharge, -charge -> negate
        battery_kw = -(bat_w_raw / 1000.0)

        grid_entity = self._entity_map.get("grid_power", "")
        # wills106 measured_power: +export, -import.
        # PowerSync grid_power:    +import, -export.
        grid_kw = grid_w / 1000.0
        if "measured_power" in grid_entity:
            grid_kw = -grid_kw

        pv_total_w = pv1_w + pv2_w + pv3_w
        if solar_total_w is None or (solar_total_w == 0 and pv_total_w > 0):
            solar_total_w = pv_total_w
        solar_kw = max(0.0, solar_total_w / 1000.0)

        if load_w <= 0:
            balanced_load_w = solar_total_w + (grid_kw * 1000.0) + (battery_kw * 1000.0)
            if balanced_load_w > 0:
                load_w = balanced_load_w
        load_kw = max(0.0, load_w / 1000.0)

        pv1_kw = max(0.0, pv1_w / 1000.0)
        pv2_kw = max(0.0, pv2_w / 1000.0)
        pv3_kw = max(0.0, pv3_w / 1000.0)

        mode_state = self.hass.states.get(self._entity_map.get("charger_use_mode", ""))
        mode = mode_state.state if mode_state else None

        return {
            "battery_level": soc,
            "battery_power": battery_kw,
            "grid_power": grid_kw,
            "solar_power": solar_kw,
            "load_power": load_kw,
            "pv1_power": pv1_kw,
            "pv2_power": pv2_kw,
            "pv3_power": pv3_kw,
            "pv1_voltage": pv1_v,
            "pv2_voltage": pv2_v,
            "pv3_voltage": pv3_v,
            "pv1_current": pv1_a,
            "pv2_current": pv2_a,
            "pv3_current": pv3_a,
            "battery_temperature": bat_temp,
            "mode": mode,
        }

    # -- Force charge / discharge -----------------------------------------

    async def force_charge(self, duration_minutes: int, power_w: int) -> bool:
        """Force charge from grid for duration_minutes at approximately power_w."""
        from homeassistant.helpers.event import async_call_later

        await self._ensure_connected()

        effective_power_w = power_w or int(self._max_charge_a * self._nominal_v)
        amps = min(effective_power_w / max(self._nominal_v, 1.0), self._max_charge_a)
        amps = max(0.0, amps)

        _LOGGER.info(
            "Solax force charge: %.1f A (%.0f W / %.1f V) for %d min",
            amps, effective_power_w, self._nominal_v, duration_minutes,
        )

        if self._control_profile == "force_time":
            await self._force_time_charge(duration_minutes, amps)
            self._cancel_timer()
            self._timer_cancel = async_call_later(
                self.hass, duration_minutes * 60, self._timer_restore
            )
            return True
        if self._control_profile == "remote_control":
            await self._remote_control_power(duration_minutes, effective_power_w)
            self._cancel_timer()
            self._timer_cancel = async_call_later(
                self.hass, duration_minutes * 60, self._timer_restore
            )
            return True

        await self._set_number("charge_current", amps)
        await self._set_select("charger_use_mode", _MODE_MANUAL)
        await self._set_select("manual_mode_select", _MANUAL_CHARGE)
        await self._set_manual_mode_control(_MANUAL_CONTROL_ON)

        self._cancel_timer()
        self._timer_cancel = async_call_later(
            self.hass, duration_minutes * 60, self._timer_restore
        )
        return True

    async def force_discharge(self, duration_minutes: int, power_w: int) -> bool:
        """Force discharge to grid for duration_minutes at approximately power_w."""
        from homeassistant.helpers.event import async_call_later

        await self._ensure_connected()

        effective_power_w = power_w or int(self._max_discharge_a * self._nominal_v)
        amps = min(effective_power_w / max(self._nominal_v, 1.0), self._max_discharge_a)
        amps = max(0.0, amps)

        _LOGGER.info(
            "Solax force discharge: %.1f A (%.0f W / %.1f V) for %d min",
            amps, effective_power_w, self._nominal_v, duration_minutes,
        )

        if self._control_profile == "force_time":
            await self._force_time_export(duration_minutes, effective_power_w, amps)
            self._cancel_timer()
            self._timer_cancel = async_call_later(
                self.hass, duration_minutes * 60, self._timer_restore
            )
            return True
        if self._control_profile == "remote_control":
            await self._remote_control_power(duration_minutes, -abs(effective_power_w))
            self._cancel_timer()
            self._timer_cancel = async_call_later(
                self.hass, duration_minutes * 60, self._timer_restore
            )
            return True

        await self._set_number("discharge_current", amps)
        await self._set_select("charger_use_mode", _MODE_MANUAL)
        await self._set_select("manual_mode_select", _MANUAL_DISCHARGE)
        await self._set_manual_mode_control(_MANUAL_CONTROL_ON)

        self._cancel_timer()
        self._timer_cancel = async_call_later(
            self.hass, duration_minutes * 60, self._timer_restore
        )
        return True

    async def restore_normal(self) -> bool:
        """Restore to Self Use / stop manual mode."""
        await self._ensure_connected()
        self._cancel_timer()
        if self._control_profile == "force_time":
            await self._restore_force_time_states()
            if self._entity_exists("charger_use_mode"):
                await self._set_select("charger_use_mode", _MODE_SELF_USE)
            _LOGGER.info("Solax restored to Self Use mode")
            return True
        if self._control_profile == "remote_control":
            await self._remote_control_stop()
            _LOGGER.info("Solax Mode1 remote control stopped")
            return True

        await self._set_select("manual_mode_select", _MANUAL_STOP)
        await self._set_manual_mode_control(_MANUAL_CONTROL_OFF)
        await self._set_select("charger_use_mode", _MODE_SELF_USE)
        _LOGGER.info("Solax restored to Self Use mode")
        return True

    # -- Reserve / mode / export ------------------------------------------

    async def set_backup_reserve(self, percent: int) -> bool:
        """Set backup reserve (minimum SOC). Clamped to [15, 100]."""
        await self._ensure_connected()
        if not self._entity_exists("backup_reserve"):
            _LOGGER.warning(
                "Solax backup reserve entity not found; expected one of: %s",
                ", ".join(_WRITE_ENTITIES["backup_reserve"]),
            )
            return False
        clamped = max(15, min(100, int(percent)))
        await self._set_number("backup_reserve", clamped)
        if self._control_profile == "force_time" and self._entity_exists("grid_tied_min_soc"):
            await self._set_number("grid_tied_min_soc", clamped)
        _LOGGER.info("Solax backup reserve set to %d%%", clamped)
        return True

    async def set_operation_mode(self, mode: str) -> bool:
        """Map PowerSync operation mode to Solax charger_use_mode."""
        await self._ensure_connected()
        option = _OP_MODE_MAP.get(mode)
        if not option:
            _LOGGER.warning("Solax: unknown operation mode '%s'", mode)
            return False
        if self._control_profile == "remote_control" and mode == "self_consumption":
            await self._remote_control_self_use()
            _LOGGER.info("Solax Mode1 self-use emulation enabled (%s)", mode)
            return True
        await self._set_select("charger_use_mode", option)
        _LOGGER.info("Solax operation mode set to '%s' (%s)", option, mode)
        return True

    async def set_grid_export_limit(self, watts: int) -> bool:
        """Set grid export limit in watts."""
        await self._ensure_connected()
        entity_id = self._entity_map.get("export_limit")
        if not entity_id or self.hass.states.get(entity_id) is None:
            _LOGGER.debug("Solax: export_control_user_limit entity not found, skipping")
            return False
        await self._set_number("export_limit", max(0, watts))
        return True

    async def curtail(self, home_load_w: int | None = None) -> bool:
        """Apply load-following curtailment or zero-export."""
        limit_w = max(0, home_load_w or 0)
        return await self.set_grid_export_limit(limit_w)

    async def restore(self) -> bool:
        """Remove export limit (99999 W effectively disables it)."""
        return await self.set_grid_export_limit(99999)

    async def disconnect(self) -> None:
        """No persistent connection to close."""
        self._cancel_timer()

    # -- Internals ---------------------------------------------------------

    def _discover_entities(self) -> None:
        """Populate the logical entity map from config entry or legacy prefix."""
        self._entity_map = {}

        if self._solax_entry_id:
            registry = er.async_get(self.hass)
            entries = er.async_entries_for_config_entry(registry, self._solax_entry_id)
            entity_ids = [entry.entity_id for entry in entries if entry.entity_id]
            entity_ids.extend(
                state.entity_id
                for state in self.hass.states.async_all()
                if state.entity_id.startswith(("sensor.", "number.", "select.", "time.", "button."))
                and state.entity_id not in entity_ids
            )
            self._discover_entities_from_ids(
                entity_ids,
                legacy_prefix=self._prefix or None,
            )
            return

        entity_ids = [
            state.entity_id
            for state in self.hass.states.async_all()
            if state.entity_id.startswith(("sensor.", "number.", "select.", "time.", "button."))
        ]
        self._discover_entities_from_ids(entity_ids, legacy_prefix=self._prefix)

    def _discover_entities_from_ids(
        self,
        entity_ids: list[str],
        legacy_prefix: str | None = None,
    ) -> None:
        """Resolve logical keys to concrete entity IDs."""
        for key, suffixes in _READ_ENTITIES.items():
            entity_id = self._resolve_entity_id(entity_ids, "sensor", suffixes, legacy_prefix)
            if entity_id:
                self._entity_map[key] = entity_id

        for key, suffixes in _WRITE_ENTITIES.items():
            domain = self._write_domain(key)
            entity_id = self._resolve_entity_id(entity_ids, domain, suffixes, legacy_prefix)
            if entity_id:
                self._entity_map[key] = entity_id

        self._update_prefix_from_map()

    def _resolve_entity_id(
        self,
        entity_ids: list[str],
        domain: str,
        suffixes: tuple[str, ...],
        legacy_prefix: str | None,
    ) -> str | None:
        """Find a matching entity by suffix, optionally constrained to a prefix."""
        if legacy_prefix:
            for suffix in suffixes:
                candidate = f"{domain}.{legacy_prefix}_{suffix}"
                if self.hass.states.get(candidate) is not None:
                    return candidate

        domain_prefix = f"{domain}."
        for suffix in suffixes:
            matches: list[str] = []
            tail = f"_{suffix}"
            for entity_id in entity_ids:
                if entity_id.startswith(domain_prefix) and entity_id.endswith(tail):
                    matches.append(entity_id)
            if not matches:
                continue

            matches = sorted(matches, key=lambda entity_id: (len(entity_id), entity_id))
            for entity_id in matches:
                if self.hass.states.get(entity_id) is not None:
                    return entity_id
            return matches[0]
        return None

    def _expected_entity_hint(self, key: str) -> str | None:
        """Return a likely entity-id hint for error reporting."""
        suffixes = _READ_ENTITIES.get(key) or _WRITE_ENTITIES.get(key)
        if not suffixes:
            return None
        domain = "sensor"
        if key in _WRITE_ENTITIES:
            domain = self._write_domain(key)
        if self._prefix:
            return f"{domain}.{self._prefix}_{suffixes[0]}"
        return None

    def _missing_keys(self, keys: tuple[str, ...]) -> list[str]:
        """Return mapped keys that are missing from HA state."""
        return [
            key for key in keys
            if key not in self._entity_map
            or self.hass.states.get(self._entity_map.get(key, "")) is None
        ]

    def _entity_exists(self, key: str) -> bool:
        entity_id = self._entity_map.get(key)
        return bool(entity_id and self.hass.states.get(entity_id) is not None)

    @staticmethod
    def _write_domain(key: str) -> str:
        """Return the HA domain for a write entity key."""
        if key in ("charge_start_1", "charge_end_1"):
            return "time"
        if key in ("grid_export_button", "remotecontrol_trigger"):
            return "button"
        if key in (
            "charge_current",
            "discharge_current",
            "backup_reserve",
            "export_limit",
            "grid_export_limit",
            "grid_tied_min_soc",
            "forcetime_period_1_max_capacity",
            "remotecontrol_active_power",
            "remotecontrol_reactive_power",
            "remotecontrol_duration",
            "remotecontrol_autorepeat_duration",
        ):
            return "number"
        return "select"

    def _update_prefix_from_map(self) -> None:
        """Use a resolved control entity for better diagnostics."""
        for key in ("charger_use_mode", "battery_level", "grid_power"):
            entity_id = self._entity_map.get(key)
            suffixes = _READ_ENTITIES.get(key) or _WRITE_ENTITIES.get(key)
            if not entity_id or not suffixes or "." not in entity_id:
                continue
            object_id = entity_id.split(".", 1)[1]
            for suffix in suffixes:
                tail = f"_{suffix}"
                if object_id.endswith(tail):
                    self._prefix = object_id[:-len(tail)]
                    return

    def _read_float(self, key: str) -> float | None:
        entity_id = self._entity_map.get(key)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if not state or state.state in ("unavailable", "unknown", ""):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    async def _set_number(self, key: str, value: float) -> None:
        entity_id = self._entity_map.get(key)
        if not entity_id:
            raise ValueError(f"Missing Solax number entity for {key}")
        await self.hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entity_id, "value": value},
            blocking=True,
        )

    async def _set_time(self, key: str, value: str) -> None:
        entity_id = self._entity_map.get(key)
        if not entity_id:
            raise ValueError(f"Missing Solax time entity for {key}")
        await self.hass.services.async_call(
            "time",
            "set_value",
            {"entity_id": entity_id, "time": value},
            blocking=True,
        )

    async def _set_select(self, key: str, option: str) -> None:
        entity_id = self._entity_map.get(key)
        if not entity_id:
            raise ValueError(f"Missing Solax select entity for {key}")
        try:
            await self.hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": entity_id, "option": option},
                blocking=True,
            )
        except Exception as exc:
            _LOGGER.warning(
                "Solax: could not set %s to '%s': %s - check wills106 entity labels",
                entity_id,
                option,
                exc,
            )

    async def _set_select_first_match(self, key: str, candidates: tuple[str, ...]) -> None:
        option = self._resolve_select_option(key, candidates)
        await self._set_select(key, option)

    def _resolve_select_option(self, key: str, candidates: tuple[str, ...]) -> str:
        entity_id = self._entity_map.get(key)
        state = self.hass.states.get(entity_id) if entity_id else None
        options = list(state.attributes.get("options", [])) if state else []
        for candidate in candidates:
            if candidate in options:
                return candidate

        normalised = {
            str(option).strip().lower(): option
            for option in options
        }
        for candidate in candidates:
            match = normalised.get(candidate.strip().lower())
            if match:
                return match

        return candidates[0]

    async def _press_button(self, key: str) -> None:
        entity_id = self._entity_map.get(key)
        if not entity_id:
            raise ValueError(f"Missing Solax button entity for {key}")
        await self.hass.services.async_call(
            "button",
            "press",
            {"entity_id": entity_id},
            blocking=True,
        )

    async def _set_manual_mode_control(self, option: str) -> None:
        """Toggle manual mode where wills106 exposes a dedicated control select."""
        entity_id = self._entity_map.get("manual_mode_control")
        if not entity_id or self.hass.states.get(entity_id) is None:
            return
        await self._set_select("manual_mode_control", option)

    async def _remote_control_power(self, duration_minutes: int, power_w: int) -> None:
        """Use Solax Mode1 remotecontrol entities for non-EEPROM power control."""
        duration_seconds = max(60, int(duration_minutes) * 60)
        if self._entity_exists("remotecontrol_set_type"):
            await self._set_select_first_match("remotecontrol_set_type", _REMOTE_CONTROL_SET)
        if self._entity_exists("remotecontrol_duration"):
            await self._set_number("remotecontrol_duration", _REMOTE_CONTROL_SLOT_SECONDS)
        if self._entity_exists("remotecontrol_reactive_power"):
            await self._set_number("remotecontrol_reactive_power", 0)
        await self._set_number("remotecontrol_active_power", int(power_w))
        await self._set_select_first_match("remotecontrol_power_control", _REMOTE_CONTROL_BATTERY)
        await self._set_number("remotecontrol_autorepeat_duration", duration_seconds)
        await self._press_button("remotecontrol_trigger")
        _LOGGER.info(
            "Solax Mode1 remote control: %dW battery target for %ds",
            int(power_w),
            duration_seconds,
        )

    async def _remote_control_self_use(self) -> None:
        """Enable Mode1 self-use emulation where available."""
        if self._entity_exists("remotecontrol_set_type"):
            await self._set_select_first_match("remotecontrol_set_type", _REMOTE_CONTROL_SET)
        if self._entity_exists("remotecontrol_duration"):
            await self._set_number("remotecontrol_duration", _REMOTE_CONTROL_SLOT_SECONDS)
        await self._set_number("remotecontrol_active_power", 0)
        await self._set_select_first_match("remotecontrol_power_control", _REMOTE_CONTROL_SELF_USE)
        await self._set_number("remotecontrol_autorepeat_duration", 0)
        await self._press_button("remotecontrol_trigger")

    async def _remote_control_stop(self) -> None:
        """Stop Mode1 autorepeat and let the inverter return to its normal mode."""
        if self._entity_exists("remotecontrol_set_type"):
            await self._set_select_first_match("remotecontrol_set_type", _REMOTE_CONTROL_SET)
        if self._entity_exists("remotecontrol_duration"):
            await self._set_number("remotecontrol_duration", _REMOTE_CONTROL_SLOT_SECONDS)
        if self._entity_exists("remotecontrol_active_power"):
            await self._set_number("remotecontrol_active_power", 0)
        if self._entity_exists("remotecontrol_power_control"):
            await self._set_select_first_match("remotecontrol_power_control", _REMOTE_CONTROL_DISABLED)
        await self._set_number("remotecontrol_autorepeat_duration", 0)
        await self._press_button("remotecontrol_trigger")

    async def _force_time_charge(self, duration_minutes: int, amps: float) -> None:
        """Use Gen2/Gen3 Force Time entities to start grid charging."""
        self._save_force_time_states((
            "charger_use_mode",
            "allow_grid_charge",
            "charge_start_1",
            "charge_end_1",
            "charge_current",
            "forcetime_period_1_max_capacity",
        ))

        now = dt_util.now()
        end = now + timedelta(minutes=max(1, int(duration_minutes)))
        await self._set_number("charge_current", amps)
        if self._entity_exists("forcetime_period_1_max_capacity"):
            await self._set_number("forcetime_period_1_max_capacity", 100)
        await self._set_time("charge_start_1", now.strftime("%H:%M:%S"))
        await self._set_time("charge_end_1", end.strftime("%H:%M:%S"))
        await self._set_select_first_match("allow_grid_charge", _ALLOW_GRID_PERIOD_1_CANDIDATES)
        await self._set_select_first_match("charger_use_mode", _MODE_FORCE_TIME_CANDIDATES)
        _LOGGER.info("Solax Force Time charge enabled until %s", end.strftime("%H:%M"))

    async def _force_time_export(self, duration_minutes: int, power_w: int, amps: float) -> None:
        """Use Gen3 Grid Export entities to force discharge."""
        self._save_force_time_states((
            "export_duration",
            "grid_export_limit",
            "grid_tied_min_soc",
            "discharge_current",
        ))

        await self._set_number("discharge_current", amps)
        floor = self._read_float("backup_reserve")
        if floor is not None:
            await self._set_number("grid_tied_min_soc", max(15, min(100, int(floor))))
        export_w = abs(power_w) or (self._max_discharge_a * self._nominal_v)
        await self._set_export_duration(duration_minutes)
        await self._press_button("grid_export_button")
        await self._set_number("grid_export_limit", -export_w)
        _LOGGER.info("Solax Gen3 grid export enabled at %.0fW for %d min", export_w, duration_minutes)

    async def _set_export_duration(self, duration_minutes: int) -> None:
        """Select the closest Gen3 grid-export duration option."""
        entity_id = self._entity_map.get("export_duration")
        state = self.hass.states.get(entity_id) if entity_id else None
        options = list(state.attributes.get("options", [])) if state else []
        if not options:
            await self._set_select("export_duration", str(duration_minutes))
            return

        best_option = None
        best_delta = None
        for option in options:
            text = str(option)
            if text.strip().lower() in {"default", "disable", "disabled", "off"}:
                continue
            match = re.search(r"\d+", text)
            if not match:
                continue
            minutes = int(match.group(0))
            delta = abs(minutes - duration_minutes)
            if best_delta is None or delta < best_delta:
                best_option = option
                best_delta = delta

        await self._set_select("export_duration", best_option or options[0])

    def _save_force_time_states(self, keys: tuple[str, ...]) -> None:
        """Remember Gen2/Gen3 entities so restore_normal can unwind cleanly."""
        saved: dict[str, str] = {}
        for key in keys:
            entity_id = self._entity_map.get(key)
            state = self.hass.states.get(entity_id) if entity_id else None
            if state and state.state not in ("unknown", "unavailable", ""):
                saved[key] = state.state
        self._saved_force_time_states = saved

    async def _restore_force_time_states(self) -> None:
        saved = self._saved_force_time_states or {}
        self._saved_force_time_states = None
        for key, value in saved.items():
            try:
                domain = self._write_domain(key)
                if domain == "number":
                    await self._set_number(key, float(value))
                elif domain == "time":
                    await self._set_time(key, value)
                elif domain == "select":
                    await self._set_select(key, value)
            except Exception as exc:
                _LOGGER.debug("Solax: failed to restore %s=%s: %s", key, value, exc)

        if not saved and self._entity_exists("export_duration"):
            await self._set_select_first_match(
                "export_duration",
                _EXPORT_DURATION_DEFAULT_CANDIDATES,
            )

    def _cancel_timer(self) -> None:
        if self._timer_cancel:
            self._timer_cancel()
            self._timer_cancel = None

    async def _timer_restore(self, _now: Any = None) -> None:
        """Auto-restore after force-mode duration expires."""
        _LOGGER.info("Solax force-mode timer expired - restoring Self Use")
        self._timer_cancel = None
        try:
            await self.restore_normal()
        except Exception as exc:
            _LOGGER.error("Solax timer restore failed: %s", exc)
