"""FoxESS controller backed by nathanmarlor/foxess_modbus entities.

This bridge intentionally does not open a Modbus connection. It reads the
entities published by the FoxESS Modbus integration and sends control commands
through Home Assistant services, avoiding contention with that integration's
single Modbus session.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)


_UNAVAILABLE = {"", "unknown", "unavailable", "none", "None"}
_FOXESS_REMOTE_CONTROL_DISABLE = 0
_FOXESS_REMOTE_CONTROL_ENABLE = 1
_FOXESS_REMOTE_CONTROL_GRID = 0x0009
_FOXESS_REMOTE_ENABLE_REGISTER = 46001
_FOXESS_REMOTE_TIMEOUT_REGISTER = 46002
_FOXESS_REMOTE_ACTIVE_POWER_REGISTER = 46003
_FOXESS_REMOTE_WORK_MODE_REGISTER = 49203
_FOXESS_REMOTE_CONTROL_TIMEOUT_SECONDS = 600
_FOXESS_WORK_MODE_SELF_USE = 1
_FOXESS_WORK_MODE_FEED_IN_FIRST = 2
_FOXESS_WORK_MODE_BACK_UP = 3
_FOXESS_MODBUS_DOMAIN = "foxess_modbus"
_FOXESS_REMOTE_CONTROL_MODEL_HINTS = {
    "1KOMMA5",
    "ENPAL_IX",
    "H3_PRO",
    "H3_SMART",
    "ONE_KOMMA_FIVE",
    "SK_HWR_SMART",
}

_READ_ENTITIES: dict[str, tuple[str, ...]] = {
    "battery_level": ("battery_soc", "battery_soc_1"),
    "battery_soh": ("battery_soh", "battery_soh_1"),
    "battery_voltage": ("battery_voltage", "battery_1_voltage"),
    "battery_temperature": ("battery_temp", "battery_temp_1"),
    "battery_power": ("invbatpower", "inverter_battery_power"),
    "battery_charge": ("battery_charge",),
    "battery_discharge": ("battery_discharge",),
    "grid_power": ("grid_ct",),
    "grid_feed_in": ("feed_in",),
    "grid_consumption": ("grid_consumption",),
    "solar_power": ("pv_power_now",),
    "load_power": ("load_power",),
    "work_mode": ("work_mode",),
    "backup_reserve": ("min_soc_on_grid", "min_soc"),
    "max_charge_current": ("max_charge_current",),
    "max_discharge_current": ("max_discharge_current",),
    "daily_solar_energy": ("solar_energy_today",),
    "daily_grid_import": ("grid_consumption_energy_today",),
    "daily_grid_export": ("feed_in_energy_today",),
    "daily_battery_charge": ("battery_charge_today",),
    "daily_battery_discharge": ("battery_discharge_today",),
}

for _idx in range(1, 7):
    _READ_ENTITIES[f"pv{_idx}_power"] = (f"pv{_idx}_power",)
    _READ_ENTITIES[f"pv{_idx}_voltage"] = (f"pv{_idx}_voltage",)
    _READ_ENTITIES[f"pv{_idx}_current"] = (f"pv{_idx}_current",)

_WRITE_ENTITIES: dict[str, tuple[str, ...]] = {
    "work_mode": ("work_mode",),
    "force_charge_power": ("force_charge_power",),
    "force_discharge_power": ("force_discharge_power",),
    "backup_reserve": ("min_soc_on_grid", "min_soc"),
    "max_charge_current": ("max_charge_current",),
    "max_discharge_current": ("max_discharge_current",),
    # foxess_modbus names this entity differently across inverter generations
    # (H1/AIO expose "Export Power Limit", H3/KH expose "Export Limit"). Try the
    # most specific suffix first; the number-domain guard in resolution prevents
    # matching an unrelated on/off "Export Limit" switch.
    "export_power_limit": (
        "export_power_limit",
        "export_limit_power",
        "export_limit",
    ),
}

_WORK_MODE_OPTIONS = {
    "self_consumption": "Self Use",
    "self_use": "Self Use",
    "backup": "Back-up",
    "feed_in": "Feed-in First",
    "feed-in": "Feed-in First",
    "autonomous": "Self Use",
}


class FoxESSEntityController:
    """Bridge controller for FoxESS via the foxess_modbus HA integration."""

    def __init__(
        self,
        hass: Any,
        foxess_entry_id: str | None = None,
        entity_prefix: str = "",
    ) -> None:
        self.hass = hass
        self._foxess_entry_id = (foxess_entry_id or "").strip()
        self._prefix = entity_prefix.strip()
        self._entity_map: dict[str, str] = {}
        self._remote_control_settle_seconds = 1.0
        self._remote_control_active = False
        # Rate-limit the "curtailment unavailable" diagnostic to once per
        # discovery so a missing/disabled export-limit entity does not spam
        # the log every curtailment cycle.
        self._export_limit_warned = False

    async def connect(self) -> bool:
        """Validate the required entity surface exists."""
        self._discover_entities()
        missing = self._missing_required()
        if missing:
            missing_ids = []
            for key in missing:
                entity_id = self._entity_map.get(key)
                if key in _WRITE_ENTITIES:
                    domain = "select" if key == "work_mode" else "number"
                    if not entity_id or not entity_id.startswith(f"{domain}."):
                        missing_ids.append(self._expected_entity_hint(key) or key)
                        continue
                missing_ids.append(entity_id or self._expected_entity_hint(key) or key)
            raise ValueError(f"foxess_missing_entities:{','.join(missing_ids)}")

        _LOGGER.info(
            "FoxESS entity bridge validated (%s, %d mapped)",
            (
                f"config_entry={self._foxess_entry_id}"
                if self._foxess_entry_id
                else f"prefix={self._prefix or '<auto>'}"
            ),
            len(self._entity_map),
        )
        return True

    def get_status(self) -> dict[str, Any]:
        """Read current FoxESS data and return PowerSync-canonical values."""
        self._ensure_entity_map()

        soc = self._read_float("battery_level")
        battery_kw = self._battery_power_kw()
        grid_kw = self._grid_power_kw()
        solar_kw = self._solar_power_kw()
        load_kw = self._power_kw("load_power")
        if load_kw is None or load_kw <= 0:
            load_kw = max(0.0, solar_kw + grid_kw + battery_kw)

        status: dict[str, Any] = {
            "battery_level": soc,
            "battery_power": battery_kw,
            "grid_power": grid_kw,
            "solar_power": max(0.0, solar_kw),
            "load_power": max(0.0, load_kw),
            "battery_temperature": self._read_float("battery_temperature"),
            "battery_soh": self._read_float("battery_soh"),
            "backup_reserve": self._read_float("backup_reserve"),
            "min_soc": self._read_float("backup_reserve"),
            "mode": self._state_value("work_mode"),
            "work_mode": self._state_value("work_mode"),
            "work_mode_name": self._state_value("work_mode"),
            "max_charge_current_a": self._read_float("max_charge_current"),
            "max_discharge_current_a": self._read_float("max_discharge_current"),
            "daily_solar_energy_kwh": self._energy_kwh("daily_solar_energy"),
            "daily_grid_import_kwh": self._energy_kwh("daily_grid_import"),
            "daily_grid_export_kwh": self._energy_kwh("daily_grid_export"),
            "daily_battery_charge_kwh": self._energy_kwh("daily_battery_charge"),
            "daily_battery_discharge_kwh": self._energy_kwh("daily_battery_discharge"),
        }

        for idx in range(1, 7):
            status[f"pv{idx}_power"] = self._power_kw(f"pv{idx}_power")
            status[f"pv{idx}_voltage"] = self._read_float(f"pv{idx}_voltage")
            status[f"pv{idx}_current"] = self._read_float(f"pv{idx}_current")

        if status["max_charge_current_a"]:
            status["battery_max_charge_power_w"] = self._current_to_power_w(
                status["max_charge_current_a"]
            )
            status["battery_max_charge_power"] = round(
                status["battery_max_charge_power_w"] / 1000.0, 2
            )
        if status["max_discharge_current_a"]:
            status["battery_max_discharge_power_w"] = self._current_to_power_w(
                status["max_discharge_current_a"]
            )
            status["battery_max_discharge_power"] = round(
                status["battery_max_discharge_power_w"] / 1000.0, 2
            )

        return status

    async def force_charge(self, duration_minutes: int = 30, power_w: float = 0) -> bool:
        """Force charge by setting Nathan's remote-control power and work mode."""
        self._ensure_entity_map()
        try:
            if power_w > 0:
                await self._set_number("force_charge_power", power_w / 1000.0)
            if await self._select_work_mode("Force Charge"):
                return True
        except Exception:
            _LOGGER.warning(
                "FoxESS entity bridge: Force Charge entity path failed; "
                "trying foxess_modbus remote-control registers",
                exc_info=True,
            )
        return await self._force_via_foxess_modbus_remote_control(
            "charge",
            duration_minutes,
            power_w,
        )

    async def force_discharge(self, duration_minutes: int = 30, power_w: float = 0) -> bool:
        """Force discharge by setting Nathan's remote-control power and work mode."""
        self._ensure_entity_map()
        try:
            if power_w > 0:
                await self._set_number("force_discharge_power", power_w / 1000.0)
            if await self._select_work_mode("Force Discharge"):
                return True
        except Exception:
            _LOGGER.warning(
                "FoxESS entity bridge: Force Discharge entity path failed; "
                "trying foxess_modbus remote-control registers",
                exc_info=True,
            )
        return await self._force_via_foxess_modbus_remote_control(
            "discharge",
            duration_minutes,
            power_w,
        )

    async def restore_normal(self) -> bool:
        """Restore normal self-use operation."""
        if self._remote_control_active and await self._restore_foxess_modbus_remote_control(
            _FOXESS_WORK_MODE_SELF_USE
        ):
            return True
        try:
            if await self._select_work_mode("Self Use"):
                return True
        except Exception:
            _LOGGER.warning(
                "FoxESS entity bridge: Self Use entity path failed; "
                "trying foxess_modbus remote-control registers",
                exc_info=True,
            )
        return await self._restore_foxess_modbus_remote_control(_FOXESS_WORK_MODE_SELF_USE)

    async def set_backup_reserve(self, percent: int) -> bool:
        """Set minimum reserve SOC."""
        self._ensure_entity_map()
        await self._set_number("backup_reserve", max(0, min(100, int(percent))))
        return True

    async def get_backup_reserve(self) -> int | None:
        """Read configured reserve SOC if exposed."""
        self._ensure_entity_map()
        value = self._read_float("backup_reserve")
        return int(value) if value is not None else None

    async def set_work_mode(self, mode: str | int) -> bool:
        """Set FoxESS work mode."""
        if isinstance(mode, int):
            option = {0: "Self Use", 1: "Feed-in First", 2: "Back-up", 3: "Back-up"}.get(mode)
        else:
            option = _WORK_MODE_OPTIONS.get(str(mode).strip().lower(), str(mode))
        if not option:
            _LOGGER.warning("FoxESS entity bridge: unsupported work mode %s", mode)
            return False
        return await self._select_work_mode(option)

    async def set_operation_mode(self, mode: str) -> bool:
        """Map PowerSync operation modes to FoxESS work modes."""
        return await self.set_work_mode(mode)

    async def set_backup_mode(self) -> bool:
        """Switch to backup mode for hold/idle behaviour."""
        return await self._select_work_mode("Back-up")

    async def restore_work_mode_from_idle(self) -> bool:
        """Restore from idle/backup hold mode."""
        return await self.restore_normal()

    async def set_charge_rate_limit(self, amps: float) -> bool:
        """Set maximum charge current."""
        self._ensure_entity_map()
        await self._set_number("max_charge_current", max(0.0, float(amps)))
        return True

    async def set_discharge_rate_limit(self, amps: float) -> bool:
        """Set maximum discharge current."""
        self._ensure_entity_map()
        await self._set_number("max_discharge_current", max(0.0, float(amps)))
        return True

    async def curtail(self, home_load_w: int | None = None) -> bool:
        """Apply an export limit when the upstream entity exposes it."""
        self._ensure_entity_map()
        if not self._entity_exists("export_power_limit"):
            if await self._curtail_via_foxess_modbus_remote_control(home_load_w):
                return True
            if not self._export_limit_warned:
                self._export_limit_warned = True
                _LOGGER.warning(
                    "FoxESS entity bridge: curtailment unavailable — %s",
                    self._diagnose_missing_export_limit(),
                )
            return False
        await self._set_number("export_power_limit", max(0, int(home_load_w or 0)))
        return True

    async def restore(self) -> bool:
        """Remove export limit when the upstream entity exposes it."""
        self._ensure_entity_map()
        if not self._entity_exists("export_power_limit"):
            if await self._restore_foxess_modbus_remote_control():
                return True
            _LOGGER.debug(
                "FoxESS entity bridge: export_power_limit entity not found; "
                "nothing to restore"
            )
            return False
        await self._set_number("export_power_limit", 99999)
        return True

    async def _force_via_foxess_modbus_remote_control(
        self,
        mode: str,
        duration_minutes: int,
        power_w: float,
    ) -> bool:
        inverter = self._foxess_modbus_service_target()
        if (
            not inverter
            or not self._foxess_modbus_write_service_available()
            or not self._foxess_modbus_remote_control_supported()
        ):
            return False

        is_charge = mode == "charge"
        target_power_w = self._force_power_w(
            power_w,
            "max_charge_current" if is_charge else "max_discharge_current",
        )
        active_power_w = -target_power_w if is_charge else target_power_w
        fallback_work_mode = (
            _FOXESS_WORK_MODE_BACK_UP if is_charge else _FOXESS_WORK_MODE_FEED_IN_FIRST
        )
        timeout_seconds = max(
            _FOXESS_REMOTE_CONTROL_TIMEOUT_SECONDS,
            int(max(1, duration_minutes) * 60),
        )

        try:
            await self._call_foxess_modbus_write_registers(
                inverter,
                _FOXESS_REMOTE_WORK_MODE_REGISTER,
                [fallback_work_mode],
            )
            await self._call_foxess_modbus_write_registers(
                inverter,
                _FOXESS_REMOTE_TIMEOUT_REGISTER,
                [timeout_seconds],
            )
            await self._call_foxess_modbus_write_registers(
                inverter,
                _FOXESS_REMOTE_ENABLE_REGISTER,
                [_FOXESS_REMOTE_CONTROL_ENABLE],
            )
            if self._remote_control_settle_seconds > 0:
                await asyncio.sleep(self._remote_control_settle_seconds)
            await self._call_foxess_modbus_write_registers(
                inverter,
                _FOXESS_REMOTE_ACTIVE_POWER_REGISTER,
                self._int32_register_values(active_power_w),
            )
        except Exception:
            _LOGGER.exception(
                "FoxESS entity bridge: foxess_modbus remote-control %s failed",
                mode,
            )
            return False

        _LOGGER.info(
            "FoxESS entity bridge: force %s via foxess_modbus remote control "
            "(target=%s, power=%dW, timeout=%ss)",
            mode,
            inverter,
            target_power_w,
            timeout_seconds,
        )
        self._remote_control_active = True
        return True

    async def _curtail_via_foxess_modbus_remote_control(
        self,
        home_load_w: int | None,
    ) -> bool:
        """Fallback for H3 Smart/Pro where foxess_modbus has no export-limit entity.

        nathanmarlor/foxess_modbus exposes H3 Smart/Pro remote-control support
        through registers 46001, 46002, and 46003/46004. Use its service API so
        PowerSync does not open a competing Modbus connection.
        """
        inverter = self._foxess_modbus_service_target()
        if (
            not inverter
            or not self._foxess_modbus_write_service_available()
            or not self._foxess_modbus_remote_control_supported()
        ):
            return False

        power_w = max(0, int(home_load_w or 0))
        try:
            await self._call_foxess_modbus_write_registers(
                inverter,
                _FOXESS_REMOTE_ENABLE_REGISTER,
                [_FOXESS_REMOTE_CONTROL_GRID],
            )
            await self._call_foxess_modbus_write_registers(
                inverter,
                _FOXESS_REMOTE_TIMEOUT_REGISTER,
                [_FOXESS_REMOTE_CONTROL_TIMEOUT_SECONDS],
            )
            if self._remote_control_settle_seconds > 0:
                await asyncio.sleep(self._remote_control_settle_seconds)
            await self._call_foxess_modbus_write_registers(
                inverter,
                _FOXESS_REMOTE_ACTIVE_POWER_REGISTER,
                self._int32_register_values(power_w),
            )
        except Exception:
            _LOGGER.exception(
                "FoxESS entity bridge: foxess_modbus remote-control curtailment failed"
            )
            return False

        _LOGGER.info(
            "FoxESS entity bridge: curtailment via foxess_modbus remote control "
            "(target=%s, power=%dW)",
            inverter,
            power_w,
        )
        self._remote_control_active = True
        return True

    async def _restore_foxess_modbus_remote_control(self, work_mode: int | None = None) -> bool:
        inverter = self._foxess_modbus_service_target()
        if (
            not inverter
            or not self._foxess_modbus_write_service_available()
            or not self._foxess_modbus_remote_control_supported()
        ):
            return False
        try:
            await self._call_foxess_modbus_write_registers(
                inverter,
                _FOXESS_REMOTE_ENABLE_REGISTER,
                [_FOXESS_REMOTE_CONTROL_DISABLE],
            )
            if work_mode is not None:
                await self._call_foxess_modbus_write_registers(
                    inverter,
                    _FOXESS_REMOTE_WORK_MODE_REGISTER,
                    [work_mode],
                )
        except Exception:
            _LOGGER.exception(
                "FoxESS entity bridge: foxess_modbus remote-control restore failed"
            )
            return False

        _LOGGER.info(
            "FoxESS entity bridge: restored foxess_modbus remote control "
            "(target=%s)",
            inverter,
        )
        self._remote_control_active = False
        return True

    def _foxess_modbus_write_service_available(self) -> bool:
        has_service = getattr(self.hass.services, "has_service", None)
        if has_service is None:
            return True
        return bool(has_service(_FOXESS_MODBUS_DOMAIN, "write_registers"))

    def _force_power_w(self, requested_power_w: float, current_limit_key: str) -> int:
        if requested_power_w > 0:
            return max(0, int(requested_power_w))

        current_limit_a = self._read_float(current_limit_key)
        if current_limit_a is not None and current_limit_a > 0:
            return self._current_to_power_w(current_limit_a)
        return 5000

    async def _call_foxess_modbus_write_registers(
        self,
        inverter: str,
        start_address: int,
        values: list[int],
    ) -> None:
        await self.hass.services.async_call(
            _FOXESS_MODBUS_DOMAIN,
            "write_registers",
            {
                "inverter": inverter,
                "start_address": start_address,
                "values": ",".join(str(int(value)) for value in values),
            },
            blocking=True,
        )

    def _foxess_modbus_service_target(self) -> str | None:
        """Return a foxess_modbus service target: device id first, friendly name second."""
        if self._foxess_entry_id:
            try:
                registry = er.async_get(self.hass)
                entries = er.async_entries_for_config_entry(
                    registry, self._foxess_entry_id
                )
                for entry in entries:
                    device_id = getattr(entry, "device_id", None)
                    if device_id:
                        return str(device_id)
            except Exception:  # pragma: no cover - defensive
                pass

            friendly_name = self._foxess_modbus_config_friendly_name()
            if friendly_name:
                return friendly_name

            config_entries = getattr(self.hass, "config_entries", None)
            get_entry = getattr(config_entries, "async_get_entry", None)
            if get_entry is not None:
                try:
                    entry = get_entry(self._foxess_entry_id)
                    title = (getattr(entry, "title", "") or "").strip()
                    if title:
                        return title
                except Exception:  # pragma: no cover - defensive
                    pass

        return self._prefix or None

    def _foxess_modbus_remote_control_supported(self) -> bool:
        """Return true only for upstream profiles with the 46001 remote API."""
        return any(
            self._model_hint_supports_remote_control(model_hint)
            for model_hint in self._foxess_modbus_model_hints()
        )

    def _foxess_modbus_model_hints(self) -> list[str]:
        hints: list[str] = []
        hints.extend(self._foxess_modbus_device_model_hints())
        config_entry = self._foxess_modbus_config_entry()
        if config_entry is not None:
            for value in (
                getattr(config_entry, "data", {}) or {},
                getattr(config_entry, "options", {}) or {},
            ):
                hints.extend(self._string_values(value))
        return hints

    def _foxess_modbus_device_model_hints(self) -> list[str]:
        if not self._foxess_entry_id:
            return []

        try:
            registry = er.async_get(self.hass)
            entries = er.async_entries_for_config_entry(registry, self._foxess_entry_id)
            device_ids = {
                str(device_id)
                for entry in entries
                if (device_id := getattr(entry, "device_id", None))
            }
        except Exception:  # pragma: no cover - defensive
            return []

        if not device_ids:
            return []

        hints: list[str] = []
        registries = []
        try:
            registries.append(dr.async_get(self.hass))
        except Exception:  # pragma: no cover - defensive
            pass
        local_registry = getattr(self.hass, "device_registry", None)
        if local_registry is not None and local_registry not in registries:
            registries.append(local_registry)

        for device_registry in registries:
            devices = getattr(device_registry, "devices", {}) or {}
            for device_id in device_ids:
                device = devices.get(device_id) if hasattr(devices, "get") else None
                if device is None:
                    continue

                model = getattr(device, "model", None)
                if model:
                    hints.append(str(model))

                for identifier in getattr(device, "identifiers", ()) or ():
                    parts = tuple(identifier)
                    if len(parts) >= 2 and parts[0] == _FOXESS_MODBUS_DOMAIN:
                        hints.append(str(parts[1]))

        return hints

    def _foxess_modbus_config_entry(self) -> Any | None:
        if not self._foxess_entry_id:
            return None
        config_entries = getattr(self.hass, "config_entries", None)
        get_entry = getattr(config_entries, "async_get_entry", None)
        if get_entry is None:
            return None
        try:
            return get_entry(self._foxess_entry_id)
        except Exception:  # pragma: no cover - defensive
            return None

    def _foxess_modbus_config_friendly_name(self) -> str | None:
        config_entry = self._foxess_modbus_config_entry()
        if config_entry is None:
            return None

        for value in (
            getattr(config_entry, "data", {}) or {},
            getattr(config_entry, "options", {}) or {},
        ):
            for friendly_name in self._values_for_key(value, "friendly_name"):
                friendly_name = str(friendly_name).strip()
                if friendly_name:
                    return friendly_name
        return None

    @staticmethod
    def _model_hint_supports_remote_control(model_hint: str) -> bool:
        normalized = (
            str(model_hint)
            .upper()
            .replace("-", "_")
            .replace(" ", "_")
            .replace(".", "_")
        )
        normalized = "_".join(part for part in normalized.split("_") if part)
        compact = normalized.replace("_", "")
        if (
            normalized in _FOXESS_REMOTE_CONTROL_MODEL_HINTS
            or any(
                normalized.startswith(f"{supported}_")
                or normalized.endswith(f"_{supported}")
                or f"_{supported}_" in f"_{normalized}_"
                for supported in _FOXESS_REMOTE_CONTROL_MODEL_HINTS
            )
        ):
            return True
        return (
            compact.startswith("H3PRO")
            or (compact.startswith("H3") and compact.endswith("SMART"))
            or (compact.startswith("SKHWR") and compact.endswith("SMART"))
        )

    @classmethod
    def _string_values(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            values: list[str] = []
            for item in value.values():
                values.extend(cls._string_values(item))
            return values
        if isinstance(value, (list, tuple, set)):
            values = []
            for item in value:
                values.extend(cls._string_values(item))
            return values
        return []

    @classmethod
    def _values_for_key(cls, value: Any, key: str) -> list[Any]:
        if isinstance(value, dict):
            values: list[Any] = []
            for item_key, item_value in value.items():
                if item_key == key:
                    values.append(item_value)
                values.extend(cls._values_for_key(item_value, key))
            return values
        if isinstance(value, (list, tuple, set)):
            values = []
            for item in value:
                values.extend(cls._values_for_key(item, key))
            return values
        return []

    @staticmethod
    def _int32_register_values(value: int) -> list[int]:
        value = int(value)
        if value < 0:
            value += 0x100000000
        return [(value >> 16) & 0xFFFF, value & 0xFFFF]

    def _diagnose_missing_export_limit(self) -> str:
        """Explain why the export power limit entity is unusable.

        Distinguishes "disabled in the registry" (the common foxess_modbus
        default) from "not exposed by this inverter" so the log line tells the
        user exactly what to fix.
        """
        try:
            registry = er.async_get(self.hass)
            if self._foxess_entry_id:
                entries = er.async_entries_for_config_entry(
                    registry, self._foxess_entry_id
                )
            else:
                entries = list(registry.entities.values())
        except Exception:  # pragma: no cover - defensive
            return (
                "no export power limit entity (number.*_export_power_limit) is "
                "available; curtailment cannot zero export"
            )

        candidates = [
            entry
            for entry in entries
            if entry.entity_id.startswith("number.")
            and "export" in entry.entity_id
            and "limit" in entry.entity_id
        ]
        if not candidates:
            return (
                "your FoxESS integration does not expose an export power limit "
                "entity (number.*_export_power_limit); curtailment cannot zero "
                "export — enable the Export Power Limit control in the "
                "foxess_modbus integration if your inverter supports it"
            )
        disabled = [
            entry
            for entry in candidates
            if getattr(entry, "disabled_by", None) is not None
        ]
        if disabled:
            names = ", ".join(entry.entity_id for entry in disabled)
            return (
                f"export limit entity {names} exists but is DISABLED; enable it "
                "in Settings → Devices & Services → (FoxESS device) → entity "
                "settings to allow curtailment"
            )
        names = ", ".join(entry.entity_id for entry in candidates)
        return (
            f"export limit entity {names} is enabled but has no state "
            "(unavailable); check the FoxESS Modbus connection"
        )

    async def disconnect(self) -> None:
        """No persistent connection to close."""

    def _ensure_entity_map(self) -> None:
        if not self._entity_map:
            self._discover_entities()

    def _discover_entities(self) -> None:
        """Populate logical entity map from config entry or suffix scan."""
        self._entity_map = {}
        self._export_limit_warned = False

        if self._foxess_entry_id:
            registry = er.async_get(self.hass)
            entries = er.async_entries_for_config_entry(registry, self._foxess_entry_id)
            entity_ids = [entry.entity_id for entry in entries if entry.entity_id]
            self._discover_entities_from_ids(entity_ids, legacy_prefix=self._prefix or None)

            fallback_entity_ids = [
                state.entity_id
                for state in self.hass.states.async_all()
                if state.entity_id.startswith(("sensor.", "number.", "select."))
            ]
            self._discover_entities_from_ids(
                fallback_entity_ids,
                legacy_prefix=self._prefix or None,
            )
            return

        entity_ids = [
            state.entity_id
            for state in self.hass.states.async_all()
            if state.entity_id.startswith(("sensor.", "number.", "select."))
        ]
        self._discover_entities_from_ids(entity_ids, legacy_prefix=self._prefix or None)

    def _discover_entities_from_ids(
        self,
        entity_ids: list[str],
        legacy_prefix: str | None = None,
    ) -> None:
        for key, suffixes in _READ_ENTITIES.items():
            if key in self._entity_map:
                continue
            domain = "select" if key == "work_mode" else "sensor"
            entity_id = self._resolve_entity_id(entity_ids, domain, suffixes, legacy_prefix)
            if entity_id:
                self._entity_map[key] = entity_id

        for key, suffixes in _WRITE_ENTITIES.items():
            domain = "select" if key == "work_mode" else "number"
            existing = self._entity_map.get(key)
            if existing and existing.startswith(f"{domain}."):
                continue
            entity_id = self._resolve_entity_id(entity_ids, domain, suffixes, legacy_prefix)
            if entity_id:
                self._entity_map[key] = entity_id

    def _resolve_entity_id(
        self,
        entity_ids: list[str],
        domain: str,
        suffixes: tuple[str, ...],
        legacy_prefix: str | None,
    ) -> str | None:
        if legacy_prefix:
            for suffix in suffixes:
                candidate = f"{domain}.{legacy_prefix}_{suffix}"
                if self.hass.states.get(candidate) is not None:
                    return candidate

        domain_prefix = f"{domain}."
        for suffix in suffixes:
            candidate = f"{domain}.{suffix}"
            if candidate in entity_ids and self.hass.states.get(candidate) is not None:
                return candidate

            tail = f"_{suffix}"
            matches = [
                entity_id
                for entity_id in entity_ids
                if entity_id.startswith(domain_prefix) and entity_id.endswith(tail)
            ]
            if not matches:
                continue
            matches = sorted(matches, key=lambda entity_id: (len(entity_id), entity_id))
            for entity_id in matches:
                if self.hass.states.get(entity_id) is not None:
                    return entity_id
            return matches[0]
        return None

    def _missing_required(self) -> list[str]:
        required = ["battery_level", "work_mode"]
        if "battery_power" not in self._entity_map and (
            "battery_charge" not in self._entity_map
            or "battery_discharge" not in self._entity_map
        ):
            required.append("battery_power")
        if "grid_power" not in self._entity_map and (
            "grid_feed_in" not in self._entity_map
            or "grid_consumption" not in self._entity_map
        ):
            required.append("grid_power")
        for key in ("force_charge_power", "force_discharge_power", "backup_reserve"):
            required.append(key)
        missing: list[str] = []
        for key in required:
            entity_id = self._entity_map.get(key)
            if not entity_id or self.hass.states.get(entity_id) is None:
                missing.append(key)
                continue
            if key in _WRITE_ENTITIES:
                domain = "select" if key == "work_mode" else "number"
                if not entity_id.startswith(f"{domain}."):
                    missing.append(key)
        return missing

    def _expected_entity_hint(self, key: str) -> str | None:
        suffixes = _READ_ENTITIES.get(key) or _WRITE_ENTITIES.get(key)
        if not suffixes:
            return None
        domain = "sensor"
        if key in _WRITE_ENTITIES:
            domain = "select" if key == "work_mode" else "number"
        elif key == "work_mode":
            domain = "select"
        prefix = self._prefix or "foxess"
        return f"{domain}.{prefix}_{suffixes[0]}"

    def _entity_exists(self, key: str) -> bool:
        entity_id = self._entity_map.get(key)
        return bool(entity_id and self.hass.states.get(entity_id) is not None)

    def _state_value(self, key: str) -> str | None:
        entity_id = self._entity_map.get(key)
        state = self.hass.states.get(entity_id) if entity_id else None
        if not state or str(state.state) in _UNAVAILABLE:
            return None
        return str(state.state)

    def _read_float(self, key: str) -> float | None:
        entity_id = self._entity_map.get(key)
        state = self.hass.states.get(entity_id) if entity_id else None
        if not state or str(state.state) in _UNAVAILABLE:
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    def _power_kw(self, key: str) -> float | None:
        value = self._read_float(key)
        if value is None:
            return None
        entity_id = self._entity_map.get(key)
        state = self.hass.states.get(entity_id) if entity_id else None
        unit = str((getattr(state, "attributes", {}) or {}).get("unit_of_measurement", "")).lower()
        if unit == "w":
            return value / 1000.0
        if unit == "mw":
            return value * 1000.0
        return value

    def _energy_kwh(self, key: str) -> float | None:
        value = self._read_float(key)
        if value is None:
            return None
        entity_id = self._entity_map.get(key)
        state = self.hass.states.get(entity_id) if entity_id else None
        unit = str((getattr(state, "attributes", {}) or {}).get("unit_of_measurement", "")).lower()
        if unit == "wh":
            return value / 1000.0
        if unit == "mwh":
            return value * 1000.0
        return value

    def _battery_power_kw(self) -> float:
        battery_kw = self._power_kw("battery_power")
        if battery_kw is not None:
            return battery_kw
        discharge_kw = self._power_kw("battery_discharge") or 0.0
        charge_kw = self._power_kw("battery_charge") or 0.0
        return discharge_kw - charge_kw

    def _grid_power_kw(self) -> float:
        grid_kw = self._power_kw("grid_power")
        if grid_kw is not None:
            return -grid_kw
        consumption_kw = self._power_kw("grid_consumption") or 0.0
        feed_in_kw = self._power_kw("grid_feed_in") or 0.0
        return consumption_kw - feed_in_kw

    def _solar_power_kw(self) -> float:
        solar_kw = self._power_kw("solar_power")
        pv_string_kw = sum(self._power_kw(f"pv{idx}_power") or 0.0 for idx in range(1, 7))
        if solar_kw is None:
            return pv_string_kw
        return max(solar_kw, pv_string_kw)

    def _current_to_power_w(self, amps: float) -> int:
        voltage = self._read_float("battery_voltage") or 500.0
        return int(max(0.0, amps) * voltage)

    async def _set_number(self, key: str, value: float | int) -> None:
        entity_id = self._entity_map.get(key)
        if (
            not entity_id
            or not entity_id.startswith("number.")
            or self.hass.states.get(entity_id) is None
        ):
            self._discover_entities()
            entity_id = self._entity_map.get(key)
        if (
            not entity_id
            or not entity_id.startswith("number.")
            or self.hass.states.get(entity_id) is None
        ):
            raise ValueError(self._expected_entity_hint(key) or key)
        state = self.hass.states.get(entity_id)
        value = self._clamp_number_value(entity_id, state, value)
        await self.hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entity_id, "value": value},
            blocking=True,
        )

    def _clamp_number_value(
        self,
        entity_id: str,
        state: Any,
        value: float | int,
    ) -> float | int:
        attributes = getattr(state, "attributes", {}) or {}
        numeric_value = self._float_attr(value)
        if numeric_value is None:
            return value

        minimum = self._float_attr(
            attributes.get("min", attributes.get("native_min_value"))
        )
        maximum = self._float_attr(
            attributes.get("max", attributes.get("native_max_value"))
        )

        clamped = numeric_value
        if minimum is not None:
            clamped = max(minimum, clamped)
        if maximum is not None:
            clamped = min(maximum, clamped)

        if clamped != numeric_value:
            _LOGGER.warning(
                "FoxESS entity bridge: clamped %s from %.3g to %.3g to match "
                "number entity range",
                entity_id,
                numeric_value,
                clamped,
            )
            return clamped
        return value

    def _float_attr(self, value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    async def _select_work_mode(self, option: str) -> bool:
        self._ensure_entity_map()
        entity_id = self._entity_map.get("work_mode")
        if not entity_id or self.hass.states.get(entity_id) is None:
            self._discover_entities()
            entity_id = self._entity_map.get("work_mode")
        if not entity_id or self.hass.states.get(entity_id) is None:
            _LOGGER.error("FoxESS entity bridge: work_mode entity not found")
            return False
        state = self.hass.states.get(entity_id)
        options = (getattr(state, "attributes", {}) or {}).get("options")
        if (
            options is not None
            and isinstance(options, (list, tuple, set))
            and option not in options
        ):
            _LOGGER.warning(
                "FoxESS entity bridge: %s does not expose option %s; available=%s",
                entity_id,
                option,
                ", ".join(str(item) for item in options),
            )
            return False
        await self.hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": entity_id, "option": option},
            blocking=True,
        )
        _LOGGER.info("FoxESS entity bridge: set %s=%s", entity_id, option)
        return True
