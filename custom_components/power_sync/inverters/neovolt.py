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
import time
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

_NORMAL_DISPATCH_MODE = "Normal"
_POWER_SYNC_FORCE_MODES = {"Force Charge", "Force Discharge"}
_SURPLUS_BALANCE_BASE_MODES = {"No Battery Charge", "Idle (No Dispatch)"}
_SURPLUS_BALANCE_MIN_W = 500.0
_SURPLUS_BALANCE_START_EXPORT_W = 500.0
_SURPLUS_BALANCE_STOP_IMPORT_W = 250.0
_SURPLUS_BALANCE_DURATION_MINUTES = 2
_SURPLUS_BALANCE_POWER_STEP_W = 100.0
_SURPLUS_BALANCE_ADJUST_THRESHOLD_W = 200.0
_SURPLUS_BALANCE_MAX_SOURCE_DISCHARGE_W = 250.0
_BATTERY_FIGHTING_POWER_W = 250.0
_SOC_FULL_PCT = 99.5
_SOC_CATCHUP_DONE_PCT = 99.0
_DUPLICATE_GRID_RELATIVE_TOLERANCE = 0.20
_DUPLICATE_GRID_ABSOLUTE_TOLERANCE_KW = 0.10
_SURPLUS_BALANCER_AUTO = "auto"
_SURPLUS_BALANCER_ENABLED = "enabled"
_SURPLUS_BALANCER_DISABLED = "disabled"
_SURPLUS_BALANCER_MODES = {
    _SURPLUS_BALANCER_AUTO,
    _SURPLUS_BALANCER_ENABLED,
    _SURPLUS_BALANCER_DISABLED,
}


class NeovoltBatteryController:
    """Bridge controller for Neovolt entities exposed by the HACS integration."""

    def __init__(
        self,
        hass: Any,
        neovolt_entry_id: str,
        max_charge_kw: float = 5.0,
        max_discharge_kw: float = 5.0,
        min_soc_pct: float = 10.0,
        battery_capacity_kwh: float | None = None,
    ) -> None:
        self.hass = hass
        self._neovolt_entry_id = neovolt_entry_id
        self._max_charge_kw = float(max_charge_kw)
        self._max_discharge_kw = float(max_discharge_kw)
        self._min_soc_pct = float(min_soc_pct)
        self._battery_capacity_kwh = self._normalize_capacity_kwh(battery_capacity_kwh)
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
            "battery_capacity_kwh": self._battery_capacity_kwh
            if self._battery_capacity_kwh is not None
            else self._read_float("battery_capacity_kwh"),
            "battery_soh": self._read_float("battery_soh"),
            "battery_max_charge_power_w": self._max_charge_kw * 1000.0,
            "battery_max_discharge_power_w": self._max_discharge_kw * 1000.0,
        }

    def get_dispatch_mode(self) -> str | None:
        """Return the current dispatch mode select state."""
        if not self._entity_map:
            self._discover_entities()
        entity_id = self._entity_map.get("dispatch_mode")
        state = self.hass.states.get(entity_id) if entity_id else None
        if not state or state.state in ("unavailable", "unknown", ""):
            return None
        return str(state.state)

    async def force_charge(
        self,
        duration_minutes: int,
        power_w: int | float,
        *,
        preserve_restore_modes: bool = False,
    ) -> bool:
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

    async def force_discharge(
        self,
        duration_minutes: int,
        power_w: int | float,
        *,
        preserve_restore_modes: bool = False,
    ) -> bool:
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

    async def restore_normal(self, target_mode: str | None = None) -> bool:
        """Return Neovolt dispatch mode to Normal or a saved baseline mode."""
        await self._ensure_connected()
        target_mode = target_mode or _NORMAL_DISPATCH_MODE
        if self.get_dispatch_mode() == target_mode:
            _LOGGER.info("Neovolt dispatch mode already %s", target_mode)
            return True
        try:
            await self._set_select("dispatch_mode", target_mode)
            _LOGGER.info("Neovolt restored to %s dispatch mode", target_mode)
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

    async def set_dispatch_mode(self, mode: str) -> bool:
        """Set the dispatch mode directly."""
        await self._ensure_connected()
        try:
            await self._set_select("dispatch_mode", mode)
            _LOGGER.info("Neovolt dispatch mode set to %s", mode)
            return True
        except Exception:
            _LOGGER.exception("Neovolt set_dispatch_mode failed")
            return False

    async def set_no_battery_charge(self) -> bool:
        """Park charging/discharging on systems that expose an anti-fighting mode."""
        options = self._dispatch_mode_options()
        for mode in ("Idle (No Dispatch)", "No Battery Charge"):
            if mode in options:
                return await self.set_dispatch_mode(mode)
        return await self.set_dispatch_mode("Idle (No Dispatch)")

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

    def snapshot_dispatch_settings(self) -> dict[str, float | str | None]:
        """Capture user-facing dispatch settings so short balancer bursts are reversible."""
        return {
            "mode": self.get_dispatch_mode(),
            "dispatch_power": self._read_float("dispatch_power"),
            "dispatch_duration": self._read_float("dispatch_duration"),
            "dispatch_charge_soc": self._read_float("dispatch_charge_soc"),
        }

    async def restore_dispatch_settings(self, snapshot: dict[str, float | str | None]) -> None:
        """Restore dispatch number entities changed by a temporary balancer command."""
        if snapshot.get("dispatch_power") is not None:
            await self._set_number("dispatch_power", snapshot["dispatch_power"])
        if snapshot.get("dispatch_duration") is not None:
            await self._set_number("dispatch_duration", int(snapshot["dispatch_duration"]))
        if snapshot.get("dispatch_charge_soc") is not None:
            await self._set_number("dispatch_charge_soc", int(snapshot["dispatch_charge_soc"]))

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

    def _entity_candidates(self) -> list[tuple[str, str | None, int]]:
        registry = er.async_get(self.hass)
        entries = er.async_entries_for_config_entry(registry, self._neovolt_entry_id)
        candidates: list[tuple[str, str | None, int]] = [
            (entry.entity_id, getattr(entry, "unique_id", None), 0)
            for entry in entries
            if getattr(entry, "entity_id", None)
        ]
        seen = {entity_id for entity_id, _unique_id, _priority in candidates}
        for state in self.hass.states.async_all():
            entity_id = state.entity_id
            if (
                entity_id.startswith(("sensor.", "number.", "select.", "button."))
                and entity_id not in seen
                and ".neovolt_" in entity_id
            ):
                candidates.append((entity_id, None, 1))
                seen.add(entity_id)
        return candidates

    def _resolve_entity_id(
        self,
        candidates: list[tuple[str, str | None, int]],
        patterns: tuple[tuple[str, str], ...],
    ) -> str | None:
        for domain, suffix in patterns:
            domain_prefix = f"{domain}."
            matches: list[tuple[str, int]] = []
            entity_tail = f"_{suffix}"
            unique_tail = f"_{suffix}"
            for entity_id, unique_id, priority in candidates:
                if not entity_id.startswith(domain_prefix):
                    continue
                object_id = entity_id.split(".", 1)[1]
                if (
                    object_id.endswith(entity_tail)
                    or (unique_id and unique_id.endswith(unique_tail))
                ):
                    matches.append((entity_id, priority))
            if not matches:
                continue
            matches = sorted(
                matches,
                key=lambda match: (
                    match[1],
                    0 if self.hass.states.get(match[0]) is not None else 1,
                    len(match[0]),
                    match[0],
                ),
            )
            return matches[0][0]
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

    def _dispatch_mode_options(self) -> list[str]:
        entity_id = self._entity_map.get("dispatch_mode")
        state = self.hass.states.get(entity_id) if entity_id else None
        options = (state.attributes or {}).get("options") if state else None
        return [str(option) for option in options or []]

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

    @staticmethod
    def _normalize_capacity_kwh(value: float | int | str | None) -> float | None:
        try:
            capacity = float(value)
        except (TypeError, ValueError):
            return None
        return capacity if capacity > 0 else None


class NeovoltFleetBatteryController:
    """Aggregate and control multiple Neovolt battery controllers as one system."""

    def __init__(
        self,
        hass: Any,
        neovolt_entry_ids: list[str],
        max_charge_kw: float = 5.0,
        max_discharge_kw: float = 5.0,
        min_soc_pct: float = 10.0,
        surplus_balancer_mode: str = _SURPLUS_BALANCER_AUTO,
        soc_balance_tolerance_pct: float = 5.0,
        battery_capacities_kwh: list[float | int | str | None] | None = None,
    ) -> None:
        if not neovolt_entry_ids:
            raise ValueError("neovolt_missing_entries")

        capacities = self._normalize_capacity_list(
            battery_capacities_kwh,
            len(neovolt_entry_ids),
        )
        self._controllers = [
            NeovoltBatteryController(
                hass,
                neovolt_entry_id=entry_id,
                max_charge_kw=max_charge_kw,
                max_discharge_kw=max_discharge_kw,
                min_soc_pct=min_soc_pct,
                battery_capacity_kwh=capacities[index],
            )
            for index, entry_id in enumerate(neovolt_entry_ids)
        ]
        self._restore_modes: list[str | None] | None = None
        self._surplus_balancer_mode = (
            surplus_balancer_mode
            if surplus_balancer_mode in _SURPLUS_BALANCER_MODES
            else _SURPLUS_BALANCER_AUTO
        )
        self._soc_balance_tolerance_pct = max(0.0, float(soc_balance_tolerance_pct))
        self._surplus_balance: dict[str, Any] = {
            "active_index": None,
            "target_index": None,
            "soc_parked_index": None,
            "soc_parked_base_mode": None,
            "base_mode": None,
            "settings": None,
            "last_power_w": 0.0,
            "last_command_ts": 0.0,
            "status": "idle",
            "mode": self._surplus_balancer_mode,
            "enabled": False,
            "controller_count": len(self._controllers),
            "soc_tolerance_percent": self._soc_balance_tolerance_pct,
        }

    def set_min_soc_pct(self, min_soc_pct: float) -> None:
        """Update the discharge cutoff used for force-discharge commands."""
        for controller in self._controllers:
            controller.set_min_soc_pct(min_soc_pct)

    async def connect(self) -> bool:
        """Validate all configured Neovolt controllers."""
        for controller in self._controllers:
            await controller.connect()
        return True

    async def disconnect(self) -> None:
        """Disconnect all child controllers."""
        for controller in self._controllers:
            await controller.disconnect()

    def get_status(self) -> dict[str, Any]:
        """Read and aggregate current Neovolt fleet state."""
        statuses = [controller.get_status() for controller in self._controllers]
        capacities = [status.get("battery_capacity_kwh") for status in statuses]
        total_capacity = sum(cap for cap in capacities if cap is not None)
        solar_power = sum(status.get("solar_power", 0.0) or 0.0 for status in statuses)
        grid_power = self._fleet_grid_power_kw(statuses)
        battery_power = sum(status.get("battery_power", 0.0) or 0.0 for status in statuses)
        reported_load = sum(status.get("load_power", 0.0) or 0.0 for status in statuses)
        balanced_load = max(0.0, solar_power + grid_power + battery_power)
        load_power = balanced_load if len(statuses) > 1 else reported_load

        return {
            "solar_power": solar_power,
            "grid_power": grid_power,
            "battery_power": battery_power,
            "load_power": load_power,
            "battery_level": self._weighted_average(statuses, "battery_level", capacities),
            "battery_capacity_kwh": total_capacity or None,
            "battery_soh": self._weighted_average(statuses, "battery_soh", capacities),
            "battery_max_charge_power_w": sum(
                status.get("battery_max_charge_power_w", 0.0) or 0.0
                for status in statuses
            ),
            "battery_max_discharge_power_w": sum(
                status.get("battery_max_discharge_power_w", 0.0) or 0.0
                for status in statuses
            ),
            "controller_statuses": statuses,
            "surplus_balancer": dict(self._surplus_balance),
        }

    async def balance_solar_surplus(self, status: dict[str, Any] | None = None) -> dict[str, Any]:
        """Use otherwise-exported solar to top up one anti-fighting NeoVolt stack.

        This is AC-side balancing, not PV routing. It only acts on multi-host
        setups where one stack is already parked in an anti-fighting mode.
        """
        statuses = status.get("controller_statuses") if status else None
        if not statuses:
            statuses = [controller.get_status() for controller in self._controllers]

        active_index = self._surplus_balance.get("active_index")
        modes = [controller.get_dispatch_mode() for controller in self._controllers]

        if len(self._controllers) < 2:
            return self._set_surplus_status("disabled_single_inverter", statuses, modes)

        if not self._surplus_balancer_enabled():
            if active_index is not None:
                return await self._stop_surplus_balance("disabled", statuses, modes)
            if self._surplus_balance.get("soc_parked_index") is not None:
                return await self._restore_soc_parked_stack("disabled", statuses, modes)
            return self._set_surplus_status("disabled", statuses, modes)

        if active_index is not None:
            if active_index >= len(self._controllers) or modes[active_index] != "Force Charge":
                self._surplus_balance.update(
                    {
                        "active_index": None,
                        "target_index": None,
                        "base_mode": None,
                        "settings": None,
                        "last_power_w": 0.0,
                        "last_command_ts": 0.0,
                    }
                )
                return self._set_surplus_status("external_mode_change", statuses, modes)

            return await self._maintain_surplus_balance(active_index, statuses, modes)

        soc_action = await self._manage_soc_balance_modes(statuses, modes)
        if soc_action is not None:
            return soc_action

        target_index, target_status = self._select_surplus_balance_target(statuses, modes)
        if target_index is None:
            return self._set_surplus_status(target_status, statuses, modes)

        grid_w = self._fleet_grid_w(statuses)
        export_w = max(0.0, -grid_w)
        if export_w < _SURPLUS_BALANCE_START_EXPORT_W:
            return self._set_surplus_status("waiting_for_export", statuses, modes, target_index)
        if self._other_stacks_discharging_w(statuses, target_index) > _SURPLUS_BALANCE_MAX_SOURCE_DISCHARGE_W:
            return self._set_surplus_status("blocked_source_discharging", statuses, modes, target_index)

        return await self._start_surplus_balance(target_index, statuses, export_w, modes[target_index])

    async def force_charge(
        self,
        duration_minutes: int,
        power_w: int | float,
        *,
        preserve_restore_modes: bool = False,
    ) -> bool:
        """Force all batteries to charge via their Neovolt dispatch controls."""
        await self._stop_surplus_balance("force_charge")
        if not preserve_restore_modes:
            self._capture_restore_modes()

        if self._surplus_balancer_enabled() and len(self._controllers) > 1:
            statuses = [controller.get_status() for controller in self._controllers]
            lowest_index, highest_index, delta = self._soc_balance(statuses)
            if (
                lowest_index is not None
                and highest_index is not None
                and lowest_index != highest_index
                and delta > self._soc_balance_tolerance_pct
            ):
                return await self._force_charge_low_soc_stack(
                    duration_minutes,
                    power_w,
                    statuses,
                    lowest_index,
                )

        self._surplus_balance["soc_parked_index"] = None
        self._surplus_balance["soc_parked_base_mode"] = None
        statuses = [controller.get_status() for controller in self._controllers]
        powers = self._split_power_w(power_w, "_max_charge_kw", statuses)
        results = [
            await controller.force_charge(duration_minutes, split_power)
            for controller, split_power in zip(self._controllers, powers)
        ]
        return all(results)

    async def force_discharge(
        self,
        duration_minutes: int,
        power_w: int | float,
        *,
        preserve_restore_modes: bool = False,
    ) -> bool:
        """Force all batteries to discharge via their Neovolt dispatch controls."""
        await self._stop_surplus_balance("force_discharge")
        if not preserve_restore_modes:
            self._capture_restore_modes()
        statuses = [controller.get_status() for controller in self._controllers]
        powers = self._split_power_w(power_w, "_max_discharge_kw", statuses)
        results = [
            await controller.force_discharge(duration_minutes, split_power)
            for controller, split_power in zip(self._controllers, powers)
        ]
        return all(results)

    async def restore_normal(self) -> bool:
        """Return all Neovolt dispatch modes to their saved baseline modes."""
        await self._stop_surplus_balance("restore_normal")
        targets = self._restore_targets()
        results = [
            await controller.restore_normal(target)
            for controller, target in zip(self._controllers, targets)
        ]
        self._restore_modes = None
        self._surplus_balance["soc_parked_index"] = None
        self._surplus_balance["soc_parked_base_mode"] = None
        return all(results)

    async def set_backup_reserve(self, percent: int) -> bool:
        """Set the default discharging cutoff SOC on all Neovolt controllers."""
        await self._stop_surplus_balance("set_backup_reserve")
        results = [
            await controller.set_backup_reserve(percent)
            for controller in self._controllers
        ]
        return all(results)

    async def get_backup_reserve(self) -> int | None:
        """Read the lowest configured default discharging cutoff SOC."""
        reserves = [
            await controller.get_backup_reserve()
            for controller in self._controllers
        ]
        known_reserves = [reserve for reserve in reserves if reserve is not None]
        return min(known_reserves) if known_reserves else None

    async def set_idle(self) -> bool:
        """Best-effort hold: raise each inverter's discharge cutoff to its SOC."""
        await self._stop_surplus_balance("set_idle")
        results = [await controller.set_idle() for controller in self._controllers]
        return all(results)

    def _select_surplus_balance_target(
        self,
        statuses: list[dict[str, Any]],
        modes: list[str | None],
    ) -> tuple[int | None, str]:
        lowest_index, _highest_index, _delta = self._soc_balance(statuses)
        candidates: list[tuple[float, int]] = []
        for index, (status, mode) in enumerate(zip(statuses, modes)):
            if mode not in _SURPLUS_BALANCE_BASE_MODES:
                continue
            if status.get("battery_level", 100.0) >= 99.0:
                continue
            battery_w = float(status.get("battery_power", 0.0) or 0.0) * 1000.0
            if battery_w < -250.0:
                continue
            candidates.append((float(status.get("battery_level", 100.0) or 100.0), index))

        if not candidates:
            return None, "idle"

        if lowest_index is not None:
            lowest_soc = float(statuses[lowest_index].get("battery_level", 100.0) or 100.0)
            balanced_candidates = [
                (soc, index)
                for soc, index in candidates
                if soc <= lowest_soc + self._soc_balance_tolerance_pct
            ]
            if not balanced_candidates:
                return None, "balancing_low_stack"
            candidates = balanced_candidates

        candidates.sort()
        return candidates[0][1], "idle"

    async def _manage_soc_balance_modes(
        self,
        statuses: list[dict[str, Any]],
        modes: list[str | None],
    ) -> dict[str, Any] | None:
        lowest_index, highest_index, delta = self._soc_balance(statuses)
        parked_index = self._surplus_balance.get("soc_parked_index")

        if parked_index is not None:
            if parked_index >= len(self._controllers):
                self._surplus_balance["soc_parked_index"] = None
                self._surplus_balance["soc_parked_base_mode"] = None
            elif not self._needs_soc_balance_parking(statuses, lowest_index, highest_index, delta):
                return await self._restore_soc_parked_stack("balanced", statuses, modes)
            elif modes[parked_index] not in _SURPLUS_BALANCE_BASE_MODES:
                self._surplus_balance["soc_parked_index"] = None
                self._surplus_balance["soc_parked_base_mode"] = None

        if (
            lowest_index is None
            or highest_index is None
            or lowest_index == highest_index
            or not self._needs_soc_balance_parking(statuses, lowest_index, highest_index, delta)
        ):
            return None

        if modes[lowest_index] in _SURPLUS_BALANCE_BASE_MODES:
            return None

        high_mode = modes[highest_index]
        if high_mode in _SURPLUS_BALANCE_BASE_MODES:
            return self._set_surplus_status(
                "balancing_low_stack",
                statuses,
                modes,
                highest_index,
            )

        if high_mode == _NORMAL_DISPATCH_MODE or self._is_battery_fighting(
            statuses,
            lowest_index,
            highest_index,
        ):
            if await self._controllers[highest_index].set_no_battery_charge():
                self._surplus_balance["soc_parked_index"] = highest_index
                self._surplus_balance["soc_parked_base_mode"] = (
                    self._stable_restore_mode(high_mode) or _NORMAL_DISPATCH_MODE
                )
                modes = [controller.get_dispatch_mode() for controller in self._controllers]
                _LOGGER.info(
                    "Neovolt SOC balancer parked stack %d while stack %d catches up (delta %.1f%%)",
                    highest_index + 1,
                    lowest_index + 1,
                    delta,
                )
                return self._set_surplus_status(
                    "balancing_low_stack",
                    statuses,
                    modes,
                    highest_index,
                )
            return self._set_surplus_status(
                "park_high_stack_failed",
                statuses,
                modes,
                highest_index,
            )

        return None

    async def _force_charge_low_soc_stack(
        self,
        duration_minutes: int,
        power_w: int | float,
        statuses: list[dict[str, Any]],
        lowest_index: int,
    ) -> bool:
        modes = [controller.get_dispatch_mode() for controller in self._controllers]
        lowest_soc = float(statuses[lowest_index].get("battery_level", 0.0) or 0.0)
        target_power_w = self._force_charge_target_power_w(lowest_index, power_w)
        results: list[bool] = []

        for index, controller in enumerate(self._controllers):
            stack_soc = float(statuses[index].get("battery_level", 0.0) or 0.0)
            if index == lowest_index or stack_soc <= lowest_soc + self._soc_balance_tolerance_pct:
                results.append(await controller.force_charge(duration_minutes, target_power_w))
                continue

            if modes[index] in _SURPLUS_BALANCE_BASE_MODES:
                results.append(True)
            else:
                results.append(await controller.set_no_battery_charge())
            self._surplus_balance["soc_parked_index"] = index
            self._surplus_balance["soc_parked_base_mode"] = (
                self._stable_restore_mode(modes[index]) or _NORMAL_DISPATCH_MODE
            )

        updated_modes = [controller.get_dispatch_mode() for controller in self._controllers]
        self._surplus_balance["active_index"] = None
        self._surplus_balance["target_index"] = lowest_index
        self._surplus_balance["last_power_w"] = target_power_w
        self._set_surplus_status("force_charging_low_stack", statuses, updated_modes, lowest_index)
        _LOGGER.info(
            "Neovolt force charge balancing: charging stack %d at %.0fW while parking higher-SOC stacks",
            lowest_index + 1,
            target_power_w,
        )
        return all(results)

    async def _restore_soc_parked_stack(
        self,
        reason: str,
        statuses: list[dict[str, Any]] | None = None,
        modes: list[str | None] | None = None,
    ) -> dict[str, Any]:
        index = self._surplus_balance.get("soc_parked_index")
        if index is None:
            return self._set_surplus_status(reason, statuses, modes)

        base_mode = self._surplus_balance.get("soc_parked_base_mode") or _NORMAL_DISPATCH_MODE
        try:
            await self._controllers[index].restore_normal(base_mode)
            _LOGGER.info("Neovolt SOC balancer restored stack %d to %s: %s", index + 1, base_mode, reason)
        finally:
            self._surplus_balance["soc_parked_index"] = None
            self._surplus_balance["soc_parked_base_mode"] = None

        modes = [controller.get_dispatch_mode() for controller in self._controllers]
        return self._set_surplus_status(f"soc_{reason}", statuses, modes)

    async def _start_surplus_balance(
        self,
        index: int,
        statuses: list[dict[str, Any]],
        export_w: float,
        base_mode: str | None,
    ) -> dict[str, Any]:
        target_w = self._surplus_target_power_w(index, export_w)
        if target_w < _SURPLUS_BALANCE_MIN_W:
            return self._set_surplus_status("waiting_for_export")

        controller = self._controllers[index]
        settings = controller.snapshot_dispatch_settings()
        mode = base_mode or settings.get("mode") or _NORMAL_DISPATCH_MODE

        if not await controller.force_charge(_SURPLUS_BALANCE_DURATION_MINUTES, target_w):
            return self._set_surplus_status("start_failed")

        self._surplus_balance.update(
            {
                "active_index": index,
                "target_index": index,
                "base_mode": mode,
                "settings": settings,
                "last_power_w": target_w,
                "last_command_ts": time.monotonic(),
            }
        )
        _LOGGER.info(
            "Neovolt surplus balancer: force charging stack %d at %.0fW from %.0fW export headroom",
            index + 1,
            target_w,
            export_w,
        )
        modes = [controller.get_dispatch_mode() for controller in self._controllers]
        return self._set_surplus_status("charging", statuses, modes, index)

    async def _maintain_surplus_balance(
        self,
        index: int,
        statuses: list[dict[str, Any]],
        modes: list[str | None],
    ) -> dict[str, Any]:
        grid_w = self._fleet_grid_w(statuses)
        target_status = statuses[index]
        active_charge_w = max(
            0.0,
            -float(target_status.get("battery_power", 0.0) or 0.0) * 1000.0,
        )

        if grid_w > _SURPLUS_BALANCE_STOP_IMPORT_W:
            return await self._stop_surplus_balance("stopped_importing", statuses, modes)

        if self._other_stacks_discharging_w(statuses, index) > _SURPLUS_BALANCE_MAX_SOURCE_DISCHARGE_W:
            return await self._stop_surplus_balance("stopped_source_discharging", statuses, modes)

        if float(target_status.get("battery_level", 100.0) or 100.0) >= 99.0:
            return await self._stop_surplus_balance("stopped_target_full", statuses, modes)

        if self._target_ahead_of_lowest(statuses, index):
            return await self._stop_surplus_balance("stopped_target_ahead", statuses, modes)

        headroom_w = max(0.0, -grid_w) + active_charge_w
        if headroom_w < (_SURPLUS_BALANCE_MIN_W * 0.5):
            return await self._stop_surplus_balance("stopped_no_surplus", statuses, modes)

        target_w = self._surplus_target_power_w(index, headroom_w)
        last_power_w = float(self._surplus_balance.get("last_power_w") or 0.0)
        should_refresh = (time.monotonic() - float(self._surplus_balance.get("last_command_ts") or 0.0)) > 45
        should_adjust = abs(target_w - last_power_w) >= _SURPLUS_BALANCE_ADJUST_THRESHOLD_W
        if should_refresh or should_adjust:
            if not await self._controllers[index].force_charge(
                _SURPLUS_BALANCE_DURATION_MINUTES,
                target_w,
            ):
                return await self._stop_surplus_balance("stopped_command_failed", statuses, modes)
            self._surplus_balance["last_power_w"] = target_w
            self._surplus_balance["last_command_ts"] = time.monotonic()

        return self._set_surplus_status("charging", statuses, modes, index)

    async def _stop_surplus_balance(
        self,
        reason: str,
        statuses: list[dict[str, Any]] | None = None,
        modes: list[str | None] | None = None,
    ) -> dict[str, Any]:
        index = self._surplus_balance.get("active_index")
        if index is None:
            return self._set_surplus_status(reason, statuses, modes)

        controller = self._controllers[index]
        base_mode = self._surplus_balance.get("base_mode") or _NORMAL_DISPATCH_MODE
        settings = self._surplus_balance.get("settings") or {}

        try:
            await controller.restore_normal(base_mode)
            await controller.restore_dispatch_settings(settings)
        finally:
            self._surplus_balance.update(
                {
                    "active_index": None,
                    "target_index": None,
                    "base_mode": None,
                    "settings": None,
                    "last_power_w": 0.0,
                    "last_command_ts": 0.0,
                }
            )
        _LOGGER.info("Neovolt surplus balancer stopped stack %d: %s", index + 1, reason)
        return self._set_surplus_status(reason, statuses, modes)

    def _set_surplus_status(
        self,
        status: str,
        statuses: list[dict[str, Any]] | None = None,
        modes: list[str | None] | None = None,
        target_index: int | None = None,
    ) -> dict[str, Any]:
        self._surplus_balance["status"] = status
        self._surplus_balance["mode"] = self._surplus_balancer_mode
        self._surplus_balance["enabled"] = self._surplus_balancer_enabled()
        self._surplus_balance["controller_count"] = len(self._controllers)
        self._surplus_balance["soc_tolerance_percent"] = self._soc_balance_tolerance_pct
        if target_index is not None:
            self._surplus_balance["target_index"] = target_index
        if statuses is not None:
            self._update_surplus_diagnostics(statuses, modes)
        return dict(self._surplus_balance)

    def _surplus_balancer_enabled(self) -> bool:
        if self._surplus_balancer_mode == _SURPLUS_BALANCER_DISABLED:
            return False
        if self._surplus_balancer_mode == _SURPLUS_BALANCER_ENABLED:
            return len(self._controllers) > 1
        return len(self._controllers) > 1

    def _update_surplus_diagnostics(
        self,
        statuses: list[dict[str, Any]],
        modes: list[str | None] | None = None,
    ) -> None:
        lowest_index, highest_index, delta = self._soc_balance(statuses)
        self._surplus_balance.update(
            {
                "lowest_soc_index": lowest_index,
                "highest_soc_index": highest_index,
                "soc_delta_percent": round(delta, 2),
                "stack_modes": list(modes) if modes is not None else None,
                "stack_soc": [
                    round(float(status.get("battery_level", 0.0) or 0.0), 2)
                    for status in statuses
                ],
                "stack_battery_power_w": [
                    round(float(status.get("battery_power", 0.0) or 0.0) * 1000.0, 1)
                    for status in statuses
                ],
            "stack_grid_power_w": [
                round(float(status.get("grid_power", 0.0) or 0.0) * 1000.0, 1)
                for status in statuses
            ],
            "stack_capacity_kwh": [
                round(float(capacity), 2) if capacity is not None else None
                for capacity in self._status_capacities(statuses)
            ],
            }
        )

    def _target_ahead_of_lowest(
        self,
        statuses: list[dict[str, Any]],
        target_index: int,
    ) -> bool:
        lowest_index, _highest_index, _delta = self._soc_balance(statuses)
        if lowest_index is None or lowest_index == target_index:
            return False
        target_soc = float(statuses[target_index].get("battery_level", 100.0) or 100.0)
        lowest_soc = float(statuses[lowest_index].get("battery_level", 100.0) or 100.0)
        return target_soc > lowest_soc + self._soc_balance_tolerance_pct

    @staticmethod
    def _soc_balance(statuses: list[dict[str, Any]]) -> tuple[int | None, int | None, float]:
        indexed_soc = [
            (index, float(status.get("battery_level", 0.0) or 0.0))
            for index, status in enumerate(statuses)
            if status.get("battery_level") is not None
        ]
        if not indexed_soc:
            return None, None, 0.0
        lowest_index, lowest_soc = min(indexed_soc, key=lambda item: item[1])
        highest_index, highest_soc = max(indexed_soc, key=lambda item: item[1])
        return lowest_index, highest_index, max(0.0, highest_soc - lowest_soc)

    def _surplus_target_power_w(self, index: int, available_w: float) -> float:
        limit_w = max(0.0, float(self._controllers[index]._max_charge_kw) * 1000.0)
        target_w = min(max(available_w, _SURPLUS_BALANCE_MIN_W), limit_w)
        stepped_w = int(target_w // _SURPLUS_BALANCE_POWER_STEP_W) * _SURPLUS_BALANCE_POWER_STEP_W
        return max(min(stepped_w, limit_w), min(_SURPLUS_BALANCE_MIN_W, limit_w))

    def _force_charge_target_power_w(self, index: int, requested_w: int | float) -> float:
        limit_w = max(0.0, float(self._controllers[index]._max_charge_kw) * 1000.0)
        if not requested_w or requested_w <= 0:
            return 0.0
        return min(float(requested_w), limit_w)

    @staticmethod
    def _fleet_grid_w(statuses: list[dict[str, Any]]) -> float:
        return NeovoltFleetBatteryController._fleet_grid_power_kw(statuses) * 1000.0

    @staticmethod
    def _fleet_grid_power_kw(statuses: list[dict[str, Any]]) -> float:
        readings = [
            float(status.get("grid_power", 0.0) or 0.0)
            for status in statuses
        ]
        non_zero = [
            reading
            for reading in readings
            if abs(reading) >= _DUPLICATE_GRID_ABSOLUTE_TOLERANCE_KW
        ]
        if len(non_zero) > 1:
            all_importing = all(reading > 0 for reading in non_zero)
            all_exporting = all(reading < 0 for reading in non_zero)
            magnitudes = [abs(reading) for reading in non_zero]
            largest = max(magnitudes)
            smallest = min(magnitudes)
            close_enough = (
                (largest - smallest) <= _DUPLICATE_GRID_ABSOLUTE_TOLERANCE_KW
                or (
                    smallest > 0
                    and (largest - smallest) / smallest <= _DUPLICATE_GRID_RELATIVE_TOLERANCE
                )
            )
            if (all_importing or all_exporting) and close_enough:
                # Multi-stack NeoVolt installs can expose the same site CT reading
                # on every inverter. Average matching readings instead of doubling
                # site import/export and the derived home load.
                return sum(non_zero) / len(non_zero)
        return sum(readings)

    def _needs_soc_balance_parking(
        self,
        statuses: list[dict[str, Any]],
        lowest_index: int | None,
        highest_index: int | None,
        delta: float,
    ) -> bool:
        if lowest_index is None or highest_index is None or lowest_index == highest_index:
            return False
        if delta > self._soc_balance_tolerance_pct:
            return True

        highest_soc = float(statuses[highest_index].get("battery_level", 0.0) or 0.0)
        lowest_soc = float(statuses[lowest_index].get("battery_level", 0.0) or 0.0)
        return highest_soc >= _SOC_FULL_PCT and lowest_soc < _SOC_CATCHUP_DONE_PCT

    @staticmethod
    def _is_battery_fighting(
        statuses: list[dict[str, Any]],
        lower_index: int,
        higher_index: int,
    ) -> bool:
        lower_w = float(statuses[lower_index].get("battery_power", 0.0) or 0.0) * 1000.0
        higher_w = float(statuses[higher_index].get("battery_power", 0.0) or 0.0) * 1000.0
        return (
            lower_w > _BATTERY_FIGHTING_POWER_W
            and higher_w < -_BATTERY_FIGHTING_POWER_W
        )

    @staticmethod
    def _other_stacks_discharging_w(
        statuses: list[dict[str, Any]],
        target_index: int,
    ) -> float:
        return sum(
            max(0.0, float(status.get("battery_power", 0.0) or 0.0) * 1000.0)
            for index, status in enumerate(statuses)
            if index != target_index
        )

    def _split_power_w(
        self,
        power_w: int | float,
        limit_attr: str,
        statuses: list[dict[str, Any]] | None = None,
    ) -> list[int | float]:
        if not power_w or power_w <= 0:
            return [0 for _controller in self._controllers]

        limits_kw = [
            max(0.0, float(getattr(controller, limit_attr, 0.0)))
            for controller in self._controllers
        ]
        limit_w = [limit_kw * 1000.0 for limit_kw in limits_kw]
        total_limit_w = sum(limit_w)
        if (
            limit_attr == "_max_discharge_kw"
            and total_limit_w > 0
            and float(power_w) >= total_limit_w
        ):
            return limit_w

        capacities = self._status_capacities(statuses or [])
        capacity_split = self._capacity_limited_power_split_w(
            float(power_w),
            limits_kw,
            capacities,
        )
        if capacity_split is not None:
            return capacity_split

        total_kw = sum(limits_kw)
        if total_kw <= 0:
            return [float(power_w) / len(self._controllers) for _controller in self._controllers]

        return [
            float(power_w) * (limit_kw / total_kw)
            for limit_kw in limits_kw
        ]

    @staticmethod
    def _normalize_capacity_list(
        values: list[float | int | str | None] | None,
        count: int,
    ) -> list[float | None]:
        capacities: list[float | None] = []
        for value in values or []:
            capacities.append(NeovoltBatteryController._normalize_capacity_kwh(value))
            if len(capacities) >= count:
                break
        while len(capacities) < count:
            capacities.append(None)
        return capacities

    @staticmethod
    def _status_capacities(statuses: list[dict[str, Any]]) -> list[float | None]:
        capacities: list[float | None] = []
        for status in statuses:
            capacities.append(
                NeovoltBatteryController._normalize_capacity_kwh(
                    status.get("battery_capacity_kwh")
                )
            )
        return capacities

    @staticmethod
    def _capacity_limited_power_split_w(
        power_w: float,
        limits_kw: list[float],
        capacities: list[float | None],
    ) -> list[float] | None:
        if len(capacities) != len(limits_kw) or any(
            capacity is None or capacity <= 0 for capacity in capacities
        ):
            return None

        total_capacity = sum(float(capacity) for capacity in capacities if capacity)
        if total_capacity <= 0:
            return None

        limit_w_by_capacity = [
            (limit_kw * 1000.0) / float(capacity)
            for limit_kw, capacity in zip(limits_kw, capacities)
            if capacity and limit_kw > 0
        ]
        if not limit_w_by_capacity:
            return None

        requested_w_by_capacity = power_w / total_capacity
        target_w_by_capacity = min(requested_w_by_capacity, min(limit_w_by_capacity))
        return [
            min(
                float(limit_kw) * 1000.0,
                target_w_by_capacity * float(capacity or 0.0),
            )
            for limit_kw, capacity in zip(limits_kw, capacities)
        ]

    def _capture_restore_modes(self) -> None:
        """Remember stable per-inverter dispatch modes before PowerSync takes over."""
        parked_index = self._surplus_balance.get("soc_parked_index")
        parked_base_mode = self._surplus_balance.get("soc_parked_base_mode")
        modes = []
        for index, controller in enumerate(self._controllers):
            if index == parked_index and parked_base_mode:
                modes.append(parked_base_mode)
            else:
                modes.append(self._stable_restore_mode(controller.get_dispatch_mode()))
        if any(modes):
            self._restore_modes = modes

    def _restore_targets(self) -> list[str | None]:
        if self._restore_modes and len(self._restore_modes) == len(self._controllers):
            return [
                mode or _NORMAL_DISPATCH_MODE
                for mode in self._restore_modes
            ]

        if len(self._controllers) == 1:
            return [_NORMAL_DISPATCH_MODE]

        return [
            self._stable_restore_mode(controller.get_dispatch_mode()) or _NORMAL_DISPATCH_MODE
            for controller in self._controllers
        ]

    @staticmethod
    def _stable_restore_mode(mode: str | None) -> str | None:
        if not mode or mode in _POWER_SYNC_FORCE_MODES:
            return None
        return mode

    @staticmethod
    def _weighted_average(
        statuses: list[dict[str, Any]],
        key: str,
        capacities: list[float | None],
    ) -> float:
        weighted_values = [
            (float(status[key]), float(capacity))
            for status, capacity in zip(statuses, capacities)
            if status.get(key) is not None and capacity is not None and capacity > 0
        ]
        total_capacity = sum(capacity for _value, capacity in weighted_values)
        if total_capacity > 0:
            return sum(value * capacity for value, capacity in weighted_values) / total_capacity

        values = [
            float(status[key])
            for status in statuses
            if status.get(key) is not None
        ]
        return sum(values) / len(values) if values else 0.0
