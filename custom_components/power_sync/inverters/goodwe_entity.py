"""GoodWe telemetry bridge backed by Home Assistant GoodWe entities.

This bridge intentionally does not open a connection to the inverter. It reads
the sensors published by the Home Assistant GoodWe integration so LAN Kit-20
systems can avoid a second direct TCP/502 polling client.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .base import InverterController, InverterState, InverterStatus

_LOGGER = logging.getLogger(__name__)

_UNAVAILABLE = {"", "unknown", "unavailable", "none", "None"}

_READ_ENTITIES: dict[str, tuple[str, ...]] = {
    "battery_level": (
        "battery_soc",
        "battery_state_of_charge",
        "battery_level",
        "soc",
    ),
    "battery_soh": ("battery_soh", "battery_state_of_health", "soh"),
    "battery_temperature": ("battery_temperature", "battery_temp"),
    "battery_power": ("battery_power", "pbattery", "pbattery1", "battery_p"),
    "battery_charge": ("battery_charge_power", "battery_charge"),
    "battery_discharge": ("battery_discharge_power", "battery_discharge"),
    "grid_power": (
        "active_power",
        "meter_active_power",
        "meter_active_power_l1",
        "meter_active_power_l2",
        "meter_active_power_l3",
        "grid_power",
        "on_grid_power",
    ),
    "grid_feed_in": (
        "grid_export_power",
        "feed_in_power",
    ),
    "grid_consumption": (
        "grid_import_power",
        "grid_consumption_power",
    ),
    "solar_power": ("ppv", "pv_power", "total_pv_power", "solar_power"),
    "load_power": ("house_consumption", "load_power", "load_ptotal"),
    "work_mode": ("work_mode", "operation_mode", "inverter_operation_mode"),
    "rated_power_w": ("rated_power", "rated_power_w", "nominal_power"),
    "model_name": ("model_name", "model"),
    "serial_number": ("serial_number", "serial"),
    "daily_solar_energy": ("total_pv_generation_today", "pv_generation_today"),
    "daily_grid_import": (
        "meter_total_energy_import_today",
        "grid_import_today",
    ),
    "daily_grid_export": (
        "meter_total_energy_export_today",
        "grid_export_today",
    ),
    "daily_battery_charge": (
        "total_battery_charge_today",
        "battery_charge_today",
    ),
    "daily_battery_discharge": (
        "total_battery_discharge_today",
        "battery_discharge_today",
    ),
}

_GRID_EXPORT_POSITIVE_SUFFIXES = {
    "active_power",
    "meter_active_power",
    "meter_active_power_l1",
    "meter_active_power_l2",
    "meter_active_power_l3",
}


class GoodWeEntityTelemetryController:
    """Read GoodWe telemetry from Home Assistant entity states."""

    def __init__(self, hass: Any, entity_prefix: str = "") -> None:
        self.hass = hass
        self._preferred_prefix = (entity_prefix or "").strip()
        self._entity_map: dict[str, str] = {}
        self._prefix: str = self._preferred_prefix

    @property
    def entity_prefix(self) -> str:
        """Return the resolved entity prefix."""
        return self._prefix

    async def connect(self) -> bool:
        """Validate that the required telemetry entity surface exists."""
        self._discover_entities()
        missing = self._missing_required()
        if missing:
            missing_ids = [
                self._entity_map.get(key)
                or self._expected_entity_hint(key)
                or key
                for key in missing
            ]
            raise ValueError(f"goodwe_entity_missing_entities:{','.join(missing_ids)}")

        _LOGGER.info(
            "GoodWe entity telemetry validated (prefix=%s, %d mapped)",
            self._prefix or "<auto>",
            len(self._entity_map),
        )
        return True

    def get_runtime_data(self) -> dict[str, Any]:
        """Return PowerSync-canonical GoodWe telemetry from entity states."""
        self._ensure_entity_map()
        missing = self._missing_required()
        if missing:
            raise ValueError(
                "goodwe_entity_missing_entities:"
                + ",".join(self._expected_entity_hint(key) or key for key in missing)
            )

        battery_kw = self._battery_power_kw()
        grid_kw = self._grid_power_kw()
        solar_kw = self._power_kw("solar_power") or 0.0
        load_kw = self._power_kw("load_power")
        if load_kw is None or load_kw <= 0:
            load_kw = max(0.0, solar_kw + grid_kw + battery_kw)

        data: dict[str, Any] = {
            "solar_power": max(0.0, solar_kw),
            "grid_power": grid_kw,
            "battery_power": battery_kw,
            "load_power": max(0.0, load_kw),
            "battery_level": self._read_float("battery_level"),
            "battery_temperature": self._read_float("battery_temperature"),
            "battery_soh": self._read_float("battery_soh"),
            "model_name": self._state_value("model_name"),
            "serial_number": self._state_value("serial_number"),
            "rated_power_w": self._power_w("rated_power_w"),
            "work_mode": self._state_value("work_mode"),
            "work_mode_name": self._state_value("work_mode"),
            "entity_telemetry": True,
        }

        for status_key, entity_key in (
            ("daily_solar_energy_kwh", "daily_solar_energy"),
            ("daily_grid_import_kwh", "daily_grid_import"),
            ("daily_grid_export_kwh", "daily_grid_export"),
            ("daily_battery_charge_kwh", "daily_battery_charge"),
            ("daily_battery_discharge_kwh", "daily_battery_discharge"),
        ):
            value = self._energy_kwh(entity_key)
            if value is not None:
                data[status_key] = value

        return data

    async def disconnect(self) -> None:
        """No direct connection to close."""

    def _ensure_entity_map(self) -> None:
        if not self._entity_map:
            self._discover_entities()

    def _discover_entities(self) -> None:
        """Populate logical entity map using preferred, goodwe, then single candidate prefix."""
        all_entity_ids = self._sensor_entity_ids()
        prefixes: list[str] = []
        for prefix in (self._preferred_prefix, "goodwe"):
            if prefix and prefix not in prefixes:
                prefixes.append(prefix)

        candidates = self._candidate_prefixes(all_entity_ids)
        if len(candidates) == 1 and candidates[0] not in prefixes:
            prefixes.append(candidates[0])

        for prefix in prefixes:
            entity_map = self._entity_map_for_prefix(all_entity_ids, prefix)
            if self._required_present(entity_map):
                self._entity_map = entity_map
                self._prefix = prefix
                return

        fallback_map = self._entity_map_from_ids(all_entity_ids)
        self._entity_map = fallback_map
        self._prefix = self._preferred_prefix

    def _entity_map_for_prefix(
        self,
        entity_ids: list[str],
        prefix: str,
    ) -> dict[str, str]:
        entity_map: dict[str, str] = {}
        for key, suffixes in _READ_ENTITIES.items():
            for suffix in suffixes:
                candidate = f"sensor.{prefix}_{suffix}"
                if self.hass.states.get(candidate) is not None:
                    entity_map[key] = candidate
                    break
        return entity_map

    def _entity_map_from_ids(self, entity_ids: list[str]) -> dict[str, str]:
        entity_map: dict[str, str] = {}
        for key, suffixes in _READ_ENTITIES.items():
            entity_id = self._resolve_entity_id(entity_ids, suffixes)
            if entity_id:
                entity_map[key] = entity_id
        return entity_map

    def _resolve_entity_id(
        self,
        entity_ids: list[str],
        suffixes: tuple[str, ...],
    ) -> str | None:
        for suffix in suffixes:
            candidate = f"sensor.{suffix}"
            if candidate in entity_ids and self.hass.states.get(candidate) is not None:
                return candidate

            tail = f"_{suffix}"
            matches = [
                entity_id
                for entity_id in entity_ids
                if entity_id.startswith("sensor.") and entity_id.endswith(tail)
            ]
            if not matches:
                continue
            matches = sorted(matches, key=lambda entity_id: (len(entity_id), entity_id))
            for entity_id in matches:
                if self.hass.states.get(entity_id) is not None:
                    return entity_id
            return matches[0]
        return None

    def _candidate_prefixes(self, entity_ids: list[str]) -> list[str]:
        candidates: set[str] = set()
        for entity_id in entity_ids:
            if not entity_id.startswith("sensor."):
                continue
            object_id = entity_id.removeprefix("sensor.")
            for suffix in _READ_ENTITIES["battery_level"]:
                tail = f"_{suffix}"
                if object_id.endswith(tail):
                    prefix = object_id[: -len(tail)]
                    if prefix:
                        candidates.add(prefix)
        return sorted(
            prefix
            for prefix in candidates
            if self._required_present(self._entity_map_for_prefix(entity_ids, prefix))
        )

    def _sensor_entity_ids(self) -> list[str]:
        try:
            return sorted(self.hass.states.async_entity_ids("sensor"))
        except TypeError:
            return sorted(
                entity_id
                for entity_id in self.hass.states.async_entity_ids()
                if entity_id.startswith("sensor.")
            )

    @staticmethod
    def _required_present(entity_map: dict[str, str]) -> bool:
        has_battery_power = "battery_power" in entity_map or (
            "battery_charge" in entity_map and "battery_discharge" in entity_map
        )
        has_grid_power = "grid_power" in entity_map or (
            "grid_consumption" in entity_map and "grid_feed_in" in entity_map
        )
        return "battery_level" in entity_map and has_battery_power and has_grid_power

    def _missing_required(self) -> list[str]:
        missing: list[str] = []
        if self._read_float("battery_level") is None:
            missing.append("battery_level")
        if self._power_kw("battery_power") is None and (
            self._power_kw("battery_charge") is None
            or self._power_kw("battery_discharge") is None
        ):
            missing.append("battery_power")
        if self._power_kw("grid_power") is None and (
            self._power_kw("grid_consumption") is None
            or self._power_kw("grid_feed_in") is None
        ):
            missing.append("grid_power")
        return missing

    def _expected_entity_hint(self, key: str) -> str | None:
        suffixes = _READ_ENTITIES.get(key)
        if not suffixes:
            return None
        prefix = self._preferred_prefix or "goodwe"
        return f"sensor.{prefix}_{suffixes[0]}"

    def _state(self, key: str) -> Any | None:
        entity_id = self._entity_map.get(key)
        return self.hass.states.get(entity_id) if entity_id else None

    def _state_value(self, key: str) -> str | None:
        state = self._state(key)
        if not state or str(state.state) in _UNAVAILABLE:
            return None
        return str(state.state)

    def _read_float(self, key: str) -> float | None:
        state = self._state(key)
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
        state = self._state(key)
        unit = str((getattr(state, "attributes", {}) or {}).get("unit_of_measurement", "")).lower()
        if unit == "w":
            return value / 1000.0
        if unit == "mw":
            return value * 1000.0
        return value

    def _power_w(self, key: str) -> int | None:
        value = self._power_kw(key)
        if value is None:
            return None
        state = self._state(key)
        unit = str((getattr(state, "attributes", {}) or {}).get("unit_of_measurement", "")).lower()
        if unit in {"w", "kw", "mw"}:
            return int(round(value * 1000.0))
        return int(round(value))

    def _energy_kwh(self, key: str) -> float | None:
        value = self._read_float(key)
        if value is None:
            return None
        state = self._state(key)
        unit = str((getattr(state, "attributes", {}) or {}).get("unit_of_measurement", "")).lower()
        if unit == "wh":
            return round(value / 1000.0, 3)
        if unit == "mwh":
            return round(value * 1000.0, 3)
        return round(value, 3)

    def _battery_power_kw(self) -> float:
        value = self._power_kw("battery_power")
        if value is not None:
            return value
        discharge_kw = self._power_kw("battery_discharge") or 0.0
        charge_kw = self._power_kw("battery_charge") or 0.0
        return discharge_kw - charge_kw

    def _grid_power_kw(self) -> float:
        value = self._power_kw("grid_power")
        if value is not None:
            state = self._state("grid_power")
            entity_id = self._entity_map.get("grid_power", "")
            object_id = entity_id.removeprefix("sensor.")
            matched_suffix = next(
                (
                    suffix
                    for suffix in _READ_ENTITIES["grid_power"]
                    if object_id.endswith(f"_{suffix}") or object_id == suffix
                ),
                "",
            )
            if matched_suffix in _GRID_EXPORT_POSITIVE_SUFFIXES:
                return -value
            if state and str(state.state).startswith("-"):
                return value
            return value
        consumption_kw = self._power_kw("grid_consumption") or 0.0
        feed_in_kw = self._power_kw("grid_feed_in") or 0.0
        return consumption_kw - feed_in_kw


class GoodWeEntityInverterController(InverterController):
    """AC-inverter controller backed by GoodWe Experimental HA entities.

    This is deliberately separate from the GoodWe battery telemetry bridge.
    It represents one standalone PV inverter and controls only the upstream
    integration's grid-export-limit number and enable switch.
    """

    _UNAVAILABLE = {"", "unknown", "unavailable", "none"}
    _STORE_VERSION = 1
    _VERIFY_ATTEMPTS = 10
    _VERIFY_DELAY_SECONDS = 0.5

    def __init__(self, hass: Any, entity_prefix: str, entry_id: str = "") -> None:
        prefix = (entity_prefix or "").strip()
        super().__init__(host=f"entity:{prefix}", port=0, slave_id=1, model="ms")
        self.hass = hass
        self.entity_prefix = prefix
        self.entry_id = entry_id
        self._snapshot: dict[str, Any] | None = None
        self._snapshot_loaded = False
        self._session_curtailed = False
        self._store = None
        if entry_id and prefix:
            try:
                from homeassistant.helpers.storage import Store

                safe_prefix = prefix.replace(".", "_")
                self._store = Store(
                    hass,
                    self._STORE_VERSION,
                    f"power_sync.goodwe_ac_restore.{entry_id}.{safe_prefix}",
                )
            except Exception as err:
                _LOGGER.debug("GoodWe entity restore storage unavailable: %s", err)

    @property
    def _pv_entity(self) -> str:
        return f"sensor.{self.entity_prefix}_pv_power"

    @property
    def _limit_entity(self) -> str:
        return f"number.{self.entity_prefix}_grid_export_limit"

    @property
    def _switch_entity(self) -> str:
        return f"switch.{self.entity_prefix}_grid_export_limit_switch"

    def _state(self, entity_id: str) -> Any | None:
        return self.hass.states.get(entity_id)

    @classmethod
    def _available_value(cls, state: Any | None) -> str | None:
        if state is None:
            return None
        value = str(getattr(state, "state", "")).strip()
        return None if value.lower() in cls._UNAVAILABLE else value

    def _number(self, entity_id: str) -> float | None:
        value = self._available_value(self._state(entity_id))
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _power_w(self, entity_id: str) -> float | None:
        value = self._number(entity_id)
        if value is None:
            return None
        state = self._state(entity_id)
        unit = str((getattr(state, "attributes", {}) or {}).get("unit_of_measurement", "W")).lower()
        if unit == "kw":
            return value * 1000.0
        if unit == "mw":
            return value * 1_000_000.0
        return value

    def _energy_kwh(self, entity_id: str) -> float | None:
        value = self._number(entity_id)
        if value is None:
            return None
        state = self._state(entity_id)
        unit = str((getattr(state, "attributes", {}) or {}).get("unit_of_measurement", "kWh")).lower()
        if unit == "wh":
            return value / 1000.0
        if unit == "mwh":
            return value * 1000.0
        return value

    def _switch_value(self) -> bool | None:
        value = self._available_value(self._state(self._switch_entity))
        if value is None:
            return None
        lowered = value.lower()
        if lowered == "on":
            return True
        if lowered == "off":
            return False
        return None

    def _runtime_marks_curtailed(self) -> bool:
        if self._session_curtailed:
            return True
        if not self.entry_id:
            return False
        entry_data = (
            getattr(self.hass, "data", {})
            .get("power_sync", {})
            .get(self.entry_id, {})
        )
        return entry_data.get("inverter_last_state") == "curtailed"

    async def _load_snapshot(self) -> None:
        if self._snapshot_loaded:
            return
        self._snapshot_loaded = True
        if self._store is None:
            return
        try:
            stored = await self._store.async_load()
            if isinstance(stored, dict) and {
                "export_limit",
                "switch_enabled",
            }.issubset(stored):
                self._snapshot = stored
        except Exception as err:
            _LOGGER.warning("GoodWe entity restore snapshot load failed: %s", err)

    async def _save_snapshot(self, snapshot: dict[str, Any] | None) -> None:
        self._snapshot = snapshot
        self._snapshot_loaded = True
        if self._store is None:
            return
        try:
            await self._store.async_save(snapshot or {})
        except Exception as err:
            _LOGGER.warning("GoodWe entity restore snapshot save failed: %s", err)

    def _validate_surface(self) -> tuple[bool, str]:
        if not self.entity_prefix:
            return False, "GoodWe entity prefix is required"
        if self._power_w(self._pv_entity) is None:
            return False, f"Missing or unavailable numeric PV entity: {self._pv_entity}"
        if self._number(self._limit_entity) is None:
            return False, f"Missing or unavailable export limit: {self._limit_entity}"
        if self._switch_value() is None:
            return False, f"Missing or unavailable export switch: {self._switch_entity}"
        return True, ""

    async def connect(self) -> bool:
        """Validate entities and recover a snapshot left by a prior HA run."""
        valid, reason = self._validate_surface()
        if not valid:
            _LOGGER.warning("GoodWe entity bridge unavailable: %s", reason)
            self._connected = False
            return False

        await self._load_snapshot()
        if self._snapshot and not self._runtime_marks_curtailed():
            _LOGGER.warning(
                "GoodWe entity bridge found an interrupted curtailment session; "
                "restoring the saved export-limit state"
            )
            if not await self._restore_snapshot(clear_on_success=True):
                self._connected = False
                return False

        self._connected = True
        return True

    async def disconnect(self) -> None:
        """Release this stateless HA-entity adapter."""
        self._connected = False

    async def _call_number(self, value: float) -> bool:
        try:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": self._limit_entity, "value": value},
                blocking=True,
            )
            return True
        except Exception as err:
            _LOGGER.error("GoodWe entity export-limit write failed: %s", err)
            return False

    async def _call_switch(self, enabled: bool) -> bool:
        try:
            await self.hass.services.async_call(
                "switch",
                "turn_on" if enabled else "turn_off",
                {"entity_id": self._switch_entity},
                blocking=True,
            )
            return True
        except Exception as err:
            _LOGGER.error("GoodWe entity export switch write failed: %s", err)
            return False

    async def _wait_for_number(self, expected: float) -> bool:
        for _ in range(self._VERIFY_ATTEMPTS):
            actual = self._number(self._limit_entity)
            if actual is not None and abs(actual - expected) <= 0.5:
                return True
            await asyncio.sleep(self._VERIFY_DELAY_SECONDS)
        return False

    async def _wait_for_switch(self, expected: bool) -> bool:
        for _ in range(self._VERIFY_ATTEMPTS):
            if self._switch_value() is expected:
                return True
            await asyncio.sleep(self._VERIFY_DELAY_SECONDS)
        return False

    async def _apply_state(self, export_limit: float, switch_enabled: bool) -> bool:
        if not await self._call_number(export_limit):
            return False
        if not await self._wait_for_number(export_limit):
            _LOGGER.error("GoodWe entity export-limit readback did not reach %.1f W", export_limit)
            return False
        if not await self._call_switch(switch_enabled):
            return False
        if not await self._wait_for_switch(switch_enabled):
            _LOGGER.error("GoodWe entity export switch readback did not reach %s", switch_enabled)
            return False
        return True

    async def _restore_snapshot(self, *, clear_on_success: bool) -> bool:
        snapshot = self._snapshot
        if not snapshot:
            return True
        success = await self._apply_state(
            float(snapshot["export_limit"]),
            bool(snapshot["switch_enabled"]),
        )
        if success and clear_on_success:
            await self._save_snapshot(None)
            self._session_curtailed = False
        return success

    async def curtail(
        self,
        home_load_w: float | None = None,
        rated_capacity_w: float | None = None,
    ) -> bool:
        """Enable zero-export mode transactionally through HA entities."""
        if not await self.connect():
            return False

        await self._load_snapshot()
        if self._snapshot is None:
            original_limit = self._number(self._limit_entity)
            original_switch = self._switch_value()
            if original_limit is None or original_switch is None:
                return False
            await self._save_snapshot(
                {
                    "export_limit": original_limit,
                    "switch_enabled": original_switch,
                }
            )

        if await self._apply_state(0.0, True):
            self._session_curtailed = True
            _LOGGER.info(
                "GoodWe entity inverter curtailed via %s and %s",
                self._limit_entity,
                self._switch_entity,
            )
            return True

        _LOGGER.error("GoodWe entity curtailment was partial; rolling back")
        self._session_curtailed = False
        await self._restore_snapshot(clear_on_success=True)
        return False

    async def restore(self) -> bool:
        """Restore the exact export-limit number and switch state."""
        if not self._validate_surface()[0]:
            return False
        await self._load_snapshot()
        if not self._snapshot:
            _LOGGER.info("GoodWe entity inverter has no saved curtailment state to restore")
            return True
        success = await self._restore_snapshot(clear_on_success=True)
        if success:
            _LOGGER.info("GoodWe entity inverter export-limit state restored")
        return success

    async def get_status(self) -> InverterState:
        """Return normalized PV and export-control telemetry."""
        if not await self.connect():
            return InverterState(
                status=InverterStatus.OFFLINE,
                is_curtailed=False,
                error_message="GoodWe entity bridge unavailable",
                attributes={"entity_prefix": self.entity_prefix},
            )

        pv_w = self._power_w(self._pv_entity)
        export_limit = self._number(self._limit_entity)
        switch_enabled = self._switch_value()
        is_curtailed = bool(switch_enabled and export_limit is not None and export_limit <= 0.5)
        attrs: dict[str, Any] = {
            "entity_prefix": self.entity_prefix,
            "export_limit_w": export_limit,
            "export_limit_enabled": switch_enabled,
            "data_source": "home_assistant_entities",
        }
        for index in (1, 2, 3):
            value = self._power_w(f"sensor.{self.entity_prefix}_pv{index}_power")
            if value is not None:
                attrs[f"pv{index}_power"] = value
        daily = self._energy_kwh(
            f"sensor.{self.entity_prefix}_today_s_pv_generation"
        )
        if daily is not None:
            attrs["daily_pv_generation"] = round(daily, 3)

        return InverterState(
            status=InverterStatus.CURTAILED if is_curtailed else InverterStatus.ONLINE,
            is_curtailed=is_curtailed,
            power_output_w=pv_w,
            attributes=attrs,
        )
