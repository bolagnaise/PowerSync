"""SolarEdge inverter controller for active-power curtailment.

Uses SolarEdge Modbus TCP/SunSpec for telemetry and the SolarEdge power
control register 0xF001 for active power limiting. If direct Modbus is not
available, falls back to known Home Assistant SolarEdge Modbus number entities.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
from datetime import datetime, timezone
from enum import Enum
import logging
import math
import re
from typing import Any, Optional

from .base import InverterController, InverterState, InverterStatus

_LOGGER = logging.getLogger(__name__)

_UNAVAILABLE = {"", "unknown", "unavailable", "none", "None"}

# Canonical suffixes come from solaredge-modbus-multi's select.py and number.py;
# the remaining values support other SolarEdge entity naming conventions.
_CONTROL_ENTITIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "storage_control_mode": (
        "select",
        (
            "storage_control_mode",
            "battery_control_mode",
            "battery_storage_control_mode",
            "control_mode",
        ),
    ),
    "storage_command_mode": (
        "select",
        (
            "storage_command_mode",
            "storage_command",
            "battery_command_mode",
            "battery_command",
            "command_mode",
            "remote_control_command_mode",
            "remote_control_command",
            "remote_command_mode",
            "remote_command",
            "battery_control_command",
            "storage_control_command",
        ),
    ),
    "charge_power_limit": (
        "number",
        (
            "storage_charge_limit",
            "storage_charge_power_limit",
            "battery_charge_power_limit",
            "battery_max_charge_power",
            "remote_control_charge_power",
            "remote_control_charge_limit",
            "charge_power_limit",
            "charge_limit",
        ),
    ),
    "discharge_power_limit": (
        "number",
        (
            "storage_discharge_limit",
            "storage_discharge_power_limit",
            "battery_discharge_power_limit",
            "battery_max_discharge_power",
            "remote_control_discharge_power",
            "remote_control_discharge_limit",
            "discharge_power_limit",
            "discharge_limit",
        ),
    ),
    "command_timeout": (
        "number",
        (
            "storage_command_timeout",
            "battery_command_timeout",
            "remote_control_timeout",
            "command_timeout",
        ),
    ),
    "backup_reserve": (
        "number",
        (
            "backup_reserve",
            "storage_backup_reserve",
            "battery_reserve",
            "battery_backup_reserve",
            "minimum_soc",
            "min_soc",
        ),
    ),
    "allow_grid_charge": (
        "switch",
        (
            "allow_grid_charge",
            "storage_grid_charge",
            "battery_grid_charge",
            "ac_charge",
            "ac_charge_policy",
            "storage_ac_charge_policy",
        ),
    ),
}

_REMOTE_CONTROL_OPTIONS = (
    "remote control",
    "remote_control",
    "remote",
    "external control",
    "external_control",
    "manual",
    "manual mode",
)
_SELF_USE_OPTIONS = (
    "maximise self consumption",
    "maximize self consumption",
    "max self consumption",
    "self consumption",
    "self_consumption",
    "self use",
    "self-use",
    "selfuse",
    "default",
    "auto",
    "automatic",
)
_CHARGE_OPTIONS = (
    "charge from solar power and grid",
    "charge from pv and ac",
    "charge from solar and grid",
    "charge from grid",
    "charge",
    "charge battery",
    "charging",
    "force charge",
    "remote charge",
)
_DISCHARGE_OPTIONS = (
    "discharge to maximize export",
    "discharge",
    "discharge battery",
    "discharging",
    "force discharge",
    "remote discharge",
    "export",
)
_IDLE_OPTIONS = ("stop", "stopped", "idle", "off", "normal", "none", "cancel")


def _normalize_option(value: str) -> str:
    return " ".join(value.replace("_", " ").replace("-", " ").lower().split())


_ENERGY_READ_ENTITIES: dict[str, tuple[str, ...]] = {
    "battery_level": (
        "state_of_energy",
        "state_of_charge",
        "battery_state_of_energy",
        "battery_state_of_charge",
        "battery_capacity",
        "battery_soc",
        "battery1_state_of_energy",
        "battery1_state_of_charge",
    ),
    "battery_power": (
        "battery_power",
        "battery1_power",
        "battery_power_charge",
        "b1_dc_power",
        "b2_dc_power",
        "b3_dc_power",
        "b4_dc_power",
        "battery1_dc_power",
        "battery2_dc_power",
        "battery3_dc_power",
        "battery4_dc_power",
    ),
    "battery_charge": (
        "battery_charge_power",
        "battery_charging_power",
        "battery1_charge_power",
    ),
    "battery_discharge": (
        "battery_discharge_power",
        "battery_discharging_power",
        "battery1_discharge_power",
    ),
    "grid_power": (
        "m1_ac_power",
        "meter_ac_power",
        "grid_power",
        "measured_power",
        "m1_power",
    ),
    "grid_import": (
        "import_power",
        "imported_power",
        "grid_import_power",
    ),
    "grid_export": (
        "export_power",
        "exported_power",
        "grid_export_power",
    ),
    "solar_power": (
        "i1_ac_power",
        "ac_power",
        "solar_power",
        "current_power",
        "pv_power",
        "pv_power_total",
        "i1_dc_power",
        "dc_power",
    ),
    # SolarEdge Modbus Multi exposes inverter DC separately from i1_ac_power.
    # On battery systems the latter includes battery discharge and is not a
    # solar-only measurement.
    "inverter_dc_power": (
        "i1_dc_power",
        "inverter_dc_power",
        "inverter1_dc_power",
        "dc_power",
    ),
    "load_power": (
        "load_power",
        "home_consumption_power",
        "house_consumption_power",
        "consumption_power",
    ),
    "battery_temperature": (
        "battery_temperature",
        "battery_temp",
        "temperature",
    ),
    "battery_soh": (
        "battery_state_of_health",
        "state_of_health",
        "battery_soh",
    ),
    "backup_reserve": (
        "backup_reserve",
        "storage_backup_reserve",
        "battery_reserve",
        "battery_backup_reserve",
    ),
    "daily_solar_energy": (
        "solar_energy_today",
        "today_solar_energy",
        "daily_solar_energy",
        "i1_ac_energy_today",
    ),
    "daily_grid_import": (
        "m1_imported_kwh",
        "grid_import_today",
        "daily_grid_import",
    ),
    "daily_grid_export": (
        "m1_exported_kwh",
        "grid_export_today",
        "daily_grid_export",
    ),
    "daily_battery_charge": (
        "battery_charged_energy_today",
        "battery_charge_today",
        "daily_battery_charge",
    ),
    "daily_battery_discharge": (
        "battery_discharged_energy_today",
        "battery_discharge_today",
        "daily_battery_discharge",
    ),
    "ev_power": (
        "ev_charger_power",
        "ev_charging_power",
        "solaredge_ev_charger_power",
    ),
}

_LIFETIME_ENERGY_TOTAL_ENTITIES: dict[str, tuple[str, ...]] = {
    "daily_grid_import": ("m1_imported_kwh",),
    "daily_grid_export": ("m1_exported_kwh",),
}

for _idx in range(1, 5):
    _ENERGY_READ_ENTITIES[f"pv{_idx}_power"] = (
        f"pv{_idx}_power",
        f"pv_power_{_idx}",
        f"i1_pv{_idx}_power",
    )


class SolarEdgeController(InverterController):
    """Controller for SolarEdge inverters via Modbus TCP or HA entities."""

    REG_INVERTER_DATA = 40071
    REG_ACTIVE_POWER_LIMIT = 0xF001
    TIMEOUT_SECONDS = 10.0
    DEFAULT_RATED_POWER_W = 5000

    STATUS_TEXT = {
        1: "off",
        2: "sleeping",
        3: "starting",
        4: "mppt",
        5: "throttled",
        6: "shutting_down",
        7: "fault",
        8: "standby",
    }

    def __init__(
        self,
        host: str,
        port: int = 502,
        slave_id: int = 1,
        model: Optional[str] = None,
        rated_power_w: Optional[float] = None,
        entity_prefix: Optional[str] = None,
        hass=None,
    ) -> None:
        super().__init__(host, port, slave_id, model)
        self.rated_power_w = float(rated_power_w or self.DEFAULT_RATED_POWER_W)
        self.entity_prefix = (
            (entity_prefix or "").strip().removesuffix("*").rstrip("_")
        )
        self._hass = hass
        self._client = None
        self._lock = asyncio.Lock()
        self._slave_in_client = False
        self._slave_param = "device_id"
        self._use_entity_mode = False
        self._active_power_limit_entity: str | None = None

    async def connect(self) -> bool:
        """Connect to SolarEdge via direct Modbus, falling back to HA entities."""
        async with self._lock:
            if self._client and getattr(self._client, "connected", False):
                self._connected = True
                self._use_entity_mode = False
                return True

            if self.host and self.host not in ("0.0.0.0", "none"):
                try:
                    from pymodbus.client import AsyncModbusTcpClient

                    self._slave_in_client = False
                    try:
                        self._client = AsyncModbusTcpClient(
                            host=self.host,
                            port=self.port,
                            timeout=self.TIMEOUT_SECONDS,
                            device_id=self.slave_id,
                        )
                        self._slave_in_client = True
                    except TypeError:
                        try:
                            self._client = AsyncModbusTcpClient(
                                host=self.host,
                                port=self.port,
                                timeout=self.TIMEOUT_SECONDS,
                                slave=self.slave_id,
                            )
                            self._slave_in_client = True
                        except TypeError:
                            self._client = AsyncModbusTcpClient(
                                host=self.host,
                                port=self.port,
                                timeout=self.TIMEOUT_SECONDS,
                            )

                    if await self._client.connect():
                        self._connected = True
                        self._use_entity_mode = False
                        _LOGGER.info(
                            "Connected to SolarEdge inverter at %s:%s (slave %s)",
                            self.host,
                            self.port,
                            self.slave_id,
                        )
                        return True
                except Exception as err:
                    _LOGGER.warning(
                        "SolarEdge Modbus connection failed for %s:%s: %s",
                        self.host,
                        self.port,
                        err,
                    )

            entity = self._find_active_power_limit_entity()
            if entity:
                self._active_power_limit_entity = entity
                self._connected = True
                self._use_entity_mode = True
                _LOGGER.info("SolarEdge using HA entity fallback: %s", entity)
                return True

            self._connected = False
            return False

    async def disconnect(self) -> None:
        """Close the direct Modbus connection."""
        async with self._lock:
            if self._client:
                self._client.close()
                self._client = None
            self._connected = False

    async def curtail(
        self,
        home_load_w: Optional[float] = None,
        rated_capacity_w: Optional[float] = None,
    ) -> bool:
        """Apply SolarEdge active power limiting.

        ``home_load_w`` maps to a percentage of rated inverter power. ``None``
        or non-positive load means full curtailment (0% active power limit).
        """
        rated_w = float(rated_capacity_w or self.rated_power_w or self.DEFAULT_RATED_POWER_W)
        if home_load_w is not None and home_load_w > 0 and rated_w > 0:
            target_pct = math.ceil((float(home_load_w) / rated_w) * 100.0)
        else:
            target_pct = 0
        target_pct = max(0, min(100, int(target_pct)))

        ok = await self._set_active_power_limit(target_pct)
        if ok:
            _LOGGER.info(
                "SolarEdge active power limit set to %d%% (home_load=%sW, rated=%sW)",
                target_pct,
                int(home_load_w) if home_load_w is not None else "none",
                int(rated_w),
            )
        return ok

    async def restore(self) -> bool:
        """Restore SolarEdge active power limit to 100%."""
        ok = await self._set_active_power_limit(100)
        if ok:
            _LOGGER.info("SolarEdge active power limit restored to 100%%")
        return ok

    async def get_status(self) -> InverterState:
        """Read current SolarEdge telemetry and curtailment state."""
        if not self._connected and not await self.connect():
            return InverterState(
                status=InverterStatus.OFFLINE,
                is_curtailed=False,
                error_message="SolarEdge connection unavailable",
            )

        attrs: dict[str, object] = {
            "mode": "entity" if self._use_entity_mode else "modbus",
            "rated_ac_power_w": self.rated_power_w,
        }

        limit_pct = await self._get_active_power_limit()
        if limit_pct is not None:
            attrs["active_power_limit_percent"] = limit_pct

        if self._use_entity_mode:
            is_curtailed = limit_pct is not None and limit_pct < 100
            return InverterState(
                status=InverterStatus.CURTAILED if is_curtailed else InverterStatus.ONLINE,
                is_curtailed=is_curtailed,
                power_limit_percent=limit_pct,
                attributes=attrs,
            )

        telemetry = await self._read_inverter_telemetry()
        attrs.update(telemetry)
        status_code = telemetry.get("status_code")
        status_text = telemetry.get("status")
        is_curtailed = bool(
            (limit_pct is not None and limit_pct < 100)
            or status_code == 5
            or status_text == "throttled"
        )

        return InverterState(
            status=InverterStatus.CURTAILED if is_curtailed else InverterStatus.ONLINE,
            is_curtailed=is_curtailed,
            power_output_w=telemetry.get("ac_power_w"),
            power_limit_percent=limit_pct,
            attributes=attrs,
        )

    async def _set_active_power_limit(self, percent: int) -> bool:
        if not self._connected and not await self.connect():
            _LOGGER.error("SolarEdge active power limit write failed: not connected")
            return False

        if self._use_entity_mode:
            entity = (
                self._active_power_limit_entity
                or self._find_active_power_limit_entity()
            )
            if not entity or not self._hass:
                return False
            try:
                await self._hass.services.async_call(
                    "number",
                    "set_value",
                    {"entity_id": entity, "value": percent},
                    blocking=True,
                )
                return True
            except Exception as err:
                _LOGGER.error("SolarEdge entity write failed for %s: %s", entity, err)
                return False

        if not self._client or not self._client.connected:
            return False
        try:
            if self._slave_in_client:
                result = await self._client.write_register(
                    address=self.REG_ACTIVE_POWER_LIMIT,
                    value=int(percent),
                )
            else:
                # Select the API keyword before transmission. Retrying an awaited
                # TypeError could repeat a write that already reached the inverter.
                parameters = inspect.signature(self._client.write_register).parameters
                kwargs = {"address": self.REG_ACTIVE_POWER_LIMIT, "value": int(percent)}
                for keyword in ("device_id", "slave", "unit"):
                    if keyword in parameters:
                        kwargs[keyword] = self.slave_id
                        break
                result = await self._client.write_register(**kwargs)
            if result is None or result.isError():
                _LOGGER.error("SolarEdge active power limit write rejected: %s", result)
                return False
            return True
        except Exception as err:
            _LOGGER.error("SolarEdge active power limit write error: %s", err)
            return False

    async def _get_active_power_limit(self) -> int | None:
        if self._use_entity_mode:
            entity = self._active_power_limit_entity or self._find_active_power_limit_entity()
            state = self._hass.states.get(entity) if self._hass and entity else None
            if state and state.state not in ("unknown", "unavailable", None):
                try:
                    return int(float(state.state))
                except (TypeError, ValueError):
                    return None
            return None

        regs = await self._read_holding_registers(self.REG_ACTIVE_POWER_LIMIT, 1)
        if not regs:
            return None
        return int(regs[0])

    async def _read_inverter_telemetry(self) -> dict[str, object]:
        regs = await self._read_holding_registers(self.REG_INVERTER_DATA, 38)
        if not regs:
            return {}

        def scaled(value: int, sf: int) -> float:
            return round(value * (10 ** sf), max(0, -sf))

        ac_power = scaled(self._to_signed16(regs[12]), self._to_signed16(regs[13]))
        dc_power = scaled(self._to_signed16(regs[29]), self._to_signed16(regs[30]))
        status_code = self._to_signed16(regs[36])

        return {
            "ac_power_w": ac_power,
            "dc_power_w": dc_power,
            "status_code": status_code,
            "status": self.STATUS_TEXT.get(status_code, f"unknown_{status_code}"),
        }

    async def _read_holding_registers(self, address: int, count: int) -> list[int] | None:
        if not self._client or not self._client.connected:
            if not await self.connect():
                return None
        try:
            if self._slave_in_client:
                result = await self._client.read_holding_registers(
                    address=address,
                    count=count,
                )
            else:
                result = await self._try_modbus_call(
                    self._client.read_holding_registers,
                    address=address,
                    count=count,
                )
            if result is None or result.isError():
                _LOGGER.debug("SolarEdge Modbus read failed at 0x%04X: %s", address, result)
                return None
            return list(result.registers)
        except Exception as err:
            _LOGGER.debug("SolarEdge Modbus read error at 0x%04X: %s", address, err)
            return None

    async def _try_modbus_call(self, method, **kwargs):
        for param in ("device_id", "slave", "unit"):
            try:
                return await method(**kwargs, **{param: self.slave_id})
            except TypeError:
                continue
        try:
            return await method(**kwargs)
        except TypeError:
            _LOGGER.error("Could not find compatible pymodbus API for %s", method.__name__)
            return None

    def _find_active_power_limit_entity(self) -> str | None:
        if not self._hass:
            return None

        prefixes = []
        if self.entity_prefix:
            prefixes.append(self.entity_prefix)
        prefixes.extend(["solaredge", "solaredge_i1"])

        candidates: list[str] = []
        for prefix in prefixes:
            candidates.extend(
                [
                    f"number.{prefix}_active_power_limit",
                    f"number.{prefix}_nominal_active_power_limit",
                    f"number.{prefix}_i1_active_power_limit",
                    f"number.{prefix}_i1_nominal_active_power_limit",
                ]
            )

        for entity_id in dict.fromkeys(candidates):
            state = self._hass.states.get(entity_id)
            if state is not None:
                return entity_id
        return None

    @staticmethod
    def _to_signed16(value: int) -> int:
        return value - 0x10000 if value >= 0x8000 else value


class SolarEdgeMutationOutcome(Enum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class _ControlSession:
    """Shared journal and lock for controllers resolving to one command entity."""

    def __init__(self, identity):
        self.identity = identity
        self.lock = asyncio.Lock()
        self.health = "initializing"
        self.baseline = None
        self.owned = {}
        self.last_mutation = None
        self.generation = 0
        self.loaded = False
        self.store = None
        self.interrupted_at = None
        self.pending_mutation = None


class SolarEdgeEnergyController:
    """Bridge SolarEdge Home battery telemetry and control through HA entities."""

    WRITE_CONFIRM_TIMEOUT_SECONDS = 15.0
    WRITE_CONFIRM_INTERVAL_SECONDS = 0.25

    def __init__(
        self,
        hass: Any,
        entity_prefix: str = "",
        solaredge_entry_id: str | None = None,
    ) -> None:
        self.hass = hass
        self._prefix = entity_prefix.strip().removesuffix("*").rstrip("_")
        self._solaredge_entry_id = (solaredge_entry_id or "").strip()
        self._entity_map: dict[str, str] = {}
        self._control_entity_map: dict[str, str] = {}
        self._battery_power_entity_ids: list[str] = []
        self._control_session: _ControlSession | None = None

    async def connect(self) -> bool:
        """Validate that at least SolarEdge battery SOC can be read."""
        self._discover_entities()
        if not self._entity_exists("battery_level"):
            hint = self._expected_entity_hint("battery_level")
            raise ValueError(f"solaredge_missing_entities:{hint}")

        session = self._coordinator()
        async with session.lock:
            await self._load_session(session)

        missing_control = self.missing_control_entities()
        if missing_control:
            _LOGGER.info(
                "SolarEdge battery dispatch unavailable until HA exposes writable entities: %s",
                ", ".join(missing_control),
            )

        _LOGGER.info(
            "SolarEdge energy bridge validated (%s, %d telemetry mapped, %d controls mapped)",
            (
                f"config_entry={self._solaredge_entry_id}"
                if self._solaredge_entry_id
                else f"prefix={self._prefix or '<auto>'}"
            ),
            len(self._entity_map),
            len(self._control_entity_map),
        )
        return True

    def get_status(self) -> dict[str, Any]:
        """Return PowerSync-canonical SolarEdge energy data."""
        self._ensure_entity_map()

        battery_kw = self._battery_power_kw()
        grid_kw = self._grid_power_kw()
        solar_kw = self._solar_power_kw()
        load_kw = self._power_kw("load_power")
        if load_kw is None or load_kw <= 0:
            load_kw = max(0.0, solar_kw + grid_kw + battery_kw)
        grid_import_kwh = self._energy_kwh("daily_grid_import")
        grid_export_kwh = self._energy_kwh("daily_grid_export")
        grid_import_is_total = self._is_lifetime_energy_total("daily_grid_import")
        grid_export_is_total = self._is_lifetime_energy_total("daily_grid_export")

        status: dict[str, Any] = {
            "telemetry_ready": self.telemetry_ready(),
            "battery_level": self._read_float("battery_level"),
            "battery_power": battery_kw,
            "grid_power": grid_kw,
            "solar_power": max(0.0, solar_kw),
            "load_power": max(0.0, load_kw),
            "ev_power": self._power_kw("ev_power"),
            "battery_temperature": self._read_float("battery_temperature"),
            "battery_soh": self._read_float("battery_soh"),
            "backup_reserve": self._read_float("backup_reserve"),
            "min_soc": self._read_float("backup_reserve"),
            "daily_solar_energy_kwh": self._daily_solar_energy_kwh(),
            "daily_grid_import_kwh": None if grid_import_is_total else grid_import_kwh,
            "daily_grid_export_kwh": None if grid_export_is_total else grid_export_kwh,
            "total_grid_import_kwh": grid_import_kwh if grid_import_is_total else None,
            "total_grid_export_kwh": grid_export_kwh if grid_export_is_total else None,
            "daily_battery_charge_kwh": self._energy_kwh("daily_battery_charge"),
            "daily_battery_discharge_kwh": self._energy_kwh("daily_battery_discharge"),
            "control_health": self.control_health,
            "last_mutation": self.last_mutation,
            "mutation_active": self.mutation_active,
            "generation": self.generation,
            "control_entities": dict(self._control_entity_map),
            "control_available": self.control_available(),
            "missing_control_entities": self.missing_control_entities(),
        }
        if grid_import_is_total:
            status["total_grid_import_entity_id"] = self._entity_map.get(
                "daily_grid_import"
            )
        if grid_export_is_total:
            status["total_grid_export_entity_id"] = self._entity_map.get(
                "daily_grid_export"
            )

        for idx in range(1, 5):
            status[f"pv{idx}_power"] = self._power_kw(f"pv{idx}_power")

        return status

    def telemetry_ready(self) -> bool:
        """Return whether the entity bridge has complete optimizer telemetry."""
        self._ensure_entity_map()
        if self._battery_power_entity_ids:
            battery_ready = all(
                self._power_kw_from_entity_id(entity_id) is not None
                for entity_id in self._battery_power_entity_ids
            )
        else:
            battery_mapped = any(
                key in self._entity_map
                for key in ("battery_power", "battery_charge", "battery_discharge")
            )
            battery_ready = not battery_mapped or self._power_kw("battery_power") is not None or (
                self._power_kw("battery_charge") is not None
                and self._power_kw("battery_discharge") is not None
            )
        grid_mapped = any(
            key in self._entity_map
            for key in ("grid_power", "grid_import", "grid_export")
        )
        grid_ready = not grid_mapped or self._power_kw("grid_power") is not None or (
            self._power_kw("grid_import") is not None
            and self._power_kw("grid_export") is not None
        )
        mapped_pv_keys = [
            f"pv{idx}_power"
            for idx in range(1, 5)
            if f"pv{idx}_power" in self._entity_map
        ]
        pv_strings_ready = bool(mapped_pv_keys) and all(
            self._power_kw(key) is not None for key in mapped_pv_keys
        )
        solar_kw = self._power_kw("solar_power")
        solar_entity_id = self._entity_map.get("solar_power")
        explicit_solar_ready = solar_kw is not None and not self._is_ac_solar_source(
            solar_entity_id
        )
        if pv_strings_ready or explicit_solar_ready:
            solar_ready = True
        elif self._has_battery_power_source():
            solar_ready = (
                self._power_kw("inverter_dc_power") is not None
                and self._battery_dc_power_kw_for_solar() is not None
            )
        else:
            # AC solar is a supported fallback only when no battery source is
            # mapped, because battery discharge contaminates i1_ac_power.
            solar_ready = (
                solar_kw is not None
                or self._power_kw("inverter_dc_power") is not None
            )
        return (
            self._read_float("battery_level") is not None
            and battery_ready
            and grid_ready
            and solar_ready
        )

    async def disconnect(self) -> None:
        """No persistent connection to close."""

    def control_available(self) -> bool:
        """Return whether the required dispatch controls are mapped."""
        self._ensure_entity_map()
        required = (
            "storage_control_mode",
            "storage_command_mode",
            "charge_power_limit",
            "discharge_power_limit",
            "command_timeout",
        )
        return all(self._control_entity_map.get(key) for key in required)

    def missing_control_entities(self) -> list[str]:
        """Return logical SolarEdge control entities that are not currently mapped."""
        self._ensure_entity_map()
        required = (
            "storage_control_mode",
            "storage_command_mode",
            "charge_power_limit",
            "discharge_power_limit",
            "command_timeout",
        )
        return [key for key in required if not self._control_entity_map.get(key)]

    def _registry_entry(self, entity_id):
        try:
            from homeassistant.helpers import entity_registry as er

            registry = er.async_get(self.hass)
            return registry.async_get(entity_id)
        except (ImportError, AttributeError):
            return None

    def _physical_identity(self, entry):
        if entry is None or getattr(entry, "platform", None) != "solaredge_modbus_multi":
            return None
        try:
            from homeassistant.helpers import device_registry as dr
            device = dr.async_get(self.hass).async_get(entry.device_id)
            identifiers = {identifier[1] for identifier in device.identifiers if identifier[0] == "solaredge_modbus_multi"}
            if len(identifiers) == 1:
                uid = next(iter(identifiers))
                if entry.unique_id == f"{uid}_storage_command_mode":
                    return f"solaredge_modbus_multi:{uid}"
        except (ImportError, AttributeError, TypeError):
            pass
        return None

    def _coordinator(self):
        if self._control_session is not None:
            return self._control_session
        self._ensure_entity_map()
        command_entity = self._control_entity_map.get("storage_command_mode")
        entry = self._registry_entry(command_entity) if command_entity else None
        identity = self._physical_identity(entry)
        if identity is None:
            if entry is not None and entry.config_entry_id and entry.unique_id:
                # Legacy adapters retain registry identity across entity renames.
                identity = f"{entry.config_entry_id}:{entry.unique_id}"
            else:
                identity = command_entity or self._prefix or self._solaredge_entry_id
        coordinators = self.hass.__dict__.setdefault(
            "_powersync_solaredge_controls", {}
        )
        if identity not in coordinators:
            coordinators[identity] = _ControlSession(identity)
        session = coordinators[identity]
        if command_entity:
            # Registry churn must not move a live controller out of containment.
            self._control_session = session
        return session

    @property
    def _saved_control_state(self):
        return self._coordinator().baseline

    @_saved_control_state.setter
    def _saved_control_state(self, value):
        self._coordinator().baseline = value

    @property
    def control_health(self) -> str:
        return self._coordinator().health

    @property
    def last_mutation(self) -> dict[str, Any] | None:
        result = self._coordinator().last_mutation
        return dict(result) if result else None

    @property
    def mutation_active(self) -> bool:
        return self._coordinator().lock.locked()

    @property
    def generation(self) -> int:
        return self._coordinator().generation

    def _create_store(self, identity):
        from homeassistant.helpers.storage import Store

        return Store(
            self.hass,
            1,
            "power_sync_solaredge_" + hashlib.sha256(identity.encode()).hexdigest(),
        )

    async def _load_session(self, session):
        if session.loaded:
            return
        try:
            session.store = self._create_store(session.identity)
            record = await session.store.async_load()
            session.health = "ready"
            if record:
                if (
                    not isinstance(record, dict)
                    or not isinstance(record.get("owned", {}), dict)
                    or not isinstance(record.get("baseline") or {}, dict)
                    or not isinstance(record.get("generation", 0), int)
                ):
                    raise ValueError("Invalid SolarEdge control journal")
                session.baseline = record.get("baseline")
                session.owned = record.get("owned", {})
                session.generation = record.get("generation", 0)
                session.last_mutation = record.get("last_mutation")
                session.pending_mutation = record.get("pending_mutation")
                if record.get("in_progress") and session.pending_mutation:
                    session.last_mutation = {
                        **session.pending_mutation,
                        "outcome": "unknown",
                        "possibly_transmitted": True,
                        "message": "Home Assistant stopped before the mutation completed",
                    }
                session.interrupted_at = record.get("recorded_at")
                if (
                    record.get("in_progress")
                    or record.get("health") != "ready"
                    or session.owned
                ):
                    session.health = "reconciliation_required"
        except Exception:
            session.health = "reconciliation_required"
            _LOGGER.exception(
                "SolarEdge control journal cannot be loaded; control is blocked"
            )
        session.loaded = True

    async def _persist(self, session, *, in_progress=False):
        if session.store is None:
            raise RuntimeError("SolarEdge control journal unavailable")
        await session.store.async_save(
            {
                "baseline": session.baseline,
                "owned": session.owned,
                "generation": session.generation,
                "health": session.health,
                "last_mutation": session.last_mutation,
                "pending_mutation": session.pending_mutation,
                "in_progress": in_progress,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    def _result(
        self,
        session,
        outcome,
        operation,
        *,
        entity=None,
        value=None,
        message="",
        possibly_transmitted=False,
        confirmation_source=None,
    ):
        if outcome == SolarEdgeMutationOutcome.CONFIRMED:
            session.pending_mutation = None
        session.last_mutation = {
            "outcome": outcome.value,
            "operation": operation,
            "operation_id": session.generation,
            "entity_id": entity,
            "intended_value": value,
            "possibly_transmitted": possibly_transmitted,
            "confirmation_source": confirmation_source,
            "message": message,
        }
        if outcome == SolarEdgeMutationOutcome.UNKNOWN:
            session.health = "reconciliation_required"
            session.interrupted_at = datetime.now(timezone.utc).isoformat()
            _LOGGER.error(
                "SolarEdge %s halted: unknown outcome for %s=%r (%s). No further inverter writes or rollback; baseline retained; reconciliation required",
                operation,
                entity,
                value,
                message,
            )
        return outcome == SolarEdgeMutationOutcome.CONFIRMED

    async def _mutate(
        self,
        operation,
        make_plan,
        *,
        automatic=False,
        expected_generation=None,
        restoring=False,
    ):
        session = self._coordinator()
        if automatic and session.lock.locked():
            # Do not replace the in-flight operation's diagnostic result.
            return False
        async with session.lock:
            await self._load_session(session)
            if session.health != "ready":
                if (
                    not session.last_mutation
                    or session.last_mutation["outcome"] != "unknown"
                ):
                    self._result(
                        session,
                        SolarEdgeMutationOutcome.REJECTED,
                        "blocked",
                        message="Control requires reconciliation",
                    )
                return False
            if (
                expected_generation is not None
                and expected_generation != session.generation
            ):
                return self._result(
                    session,
                    SolarEdgeMutationOutcome.REJECTED,
                    operation,
                    message="Superseded operation",
                )
            try:
                fresh = None
                command_entry = self._registry_entry(self._control_entity_map.get("storage_command_mode"))
                if command_entry is not None and command_entry.platform == "solaredge_modbus_multi":
                    fresh = await self._fresh_storage_state()
                    if fresh is None:
                        raise ValueError("A fresh storage baseline is unavailable")
                    for key, entity in self._control_entity_map.items():
                        state = self.hass.states.get(entity)
                        if key in fresh and state is not None and not self._control_values_match(entity, state.state, fresh[key]):
                            raise ValueError(f"Cached {key} differs from fresh storage read")
                plan = make_plan()
                plan = [
                    self._preflight(key, value, native=restoring) for key, value in plan
                ]
                if fresh is not None:
                    required_readback = (
                        {key for key, _, _ in plan} | session.owned.keys()
                    )
                    missing = required_readback - fresh.keys()
                    if missing:
                        raise ValueError(
                            "Fresh storage read is missing planned or owned fields: "
                            + ", ".join(sorted(missing))
                        )
                # Snapshot every intended field before any mutation, including optional controls.
                for key, expected in session.owned.items():
                    entity = self._control_entity_map.get(key)
                    current = self.hass.states.get(entity) if entity else None
                    original = (session.baseline or {}).get(key)
                    if current is None or not (
                        self._control_values_match(entity, current.state, expected)
                        or self._control_values_match(entity, current.state, original)
                    ):
                        raise ValueError(f"External change to owned {key}")
                baseline = (
                    dict(session.baseline or {})
                    if restoring
                    else {
                        key: session.baseline[key]
                        for key in session.owned
                        if session.baseline and key in session.baseline
                    }
                )
                for key, entity in self._control_entity_map.items():
                    if fresh is not None and key not in fresh:
                        continue
                    state = self.hass.states.get(entity)
                    if state is not None and str(state.state) not in _UNAVAILABLE:
                        baseline.setdefault(key, state.state)
                for key, entity, value in plan:
                    baseline.setdefault(key, self.hass.states.get(entity).state)
                if not restoring and self._snapshot_is_active(baseline):
                    raise ValueError("Active dispatch is not a safe baseline")
            except (ValueError, TypeError, OverflowError) as err:
                return self._result(
                    session,
                    SolarEdgeMutationOutcome.REJECTED,
                    operation,
                    message=str(err),
                )
            session.baseline = baseline or None
            session.generation += 1
            original_owned = dict(session.owned)
            current_entity = None
            current_value = None
            possible = False
            try:
                for key, entity, value in plan:
                    current = self.hass.states.get(entity)
                    # Command timeout is a lease: equal values still renew it.
                    if (
                        key != "command_timeout" or restoring
                    ) and self._control_values_match(entity, current.state, value):
                        if restoring:
                            session.owned.pop(key, None)
                        continue
                    current_entity, current_value = entity, value
                    session.pending_mutation = {
                        "operation": operation,
                        "operation_id": session.generation,
                        "entity_id": entity,
                        "intended_value": value,
                    }
                    await self._persist(session, in_progress=True)
                    possible = True
                    domain = entity.split(".", 1)[0]
                    if domain == "number":
                        service, data = (
                            "set_value",
                            {"entity_id": entity, "value": value},
                        )
                    elif domain == "select":
                        service, data = (
                            "select_option",
                            {"entity_id": entity, "option": value},
                        )
                    else:
                        service, data = (
                            ("turn_on" if value == "on" else "turn_off"),
                            {"entity_id": entity},
                        )
                    await self.hass.services.async_call(
                        domain, service, data, blocking=True
                    )
                    if not await self._confirm_mutation(key, entity, value):
                        raise TimeoutError("Requested state was not reflected")
                    if restoring:
                        session.owned.pop(key, None)
                    elif not self._control_values_match(entity, baseline[key], value):
                        session.owned[key] = value
                    else:
                        session.owned.pop(key, None)
                if restoring:
                    session.baseline = None
                command_entry = self._registry_entry(
                    self._control_entity_map.get("storage_command_mode")
                )
                source = (
                    "fresh_upstream_storage_poll"
                    if command_entry
                    and command_entry.platform == "solaredge_modbus_multi"
                    else "service_return_and_entity_reflection"
                )
                self._result(
                    session,
                    SolarEdgeMutationOutcome.CONFIRMED,
                    operation,
                    confirmation_source=source,
                )
                await self._persist(session)
                return True
            except (Exception, asyncio.CancelledError) as err:
                session.baseline = baseline or None
                if restoring:
                    session.owned = original_owned
                self._result(
                    session,
                    SolarEdgeMutationOutcome.UNKNOWN
                    if possible
                    else SolarEdgeMutationOutcome.REJECTED,
                    operation,
                    entity=current_entity,
                    value=current_value,
                    possibly_transmitted=possible,
                    message=str(err) or type(err).__name__,
                )
                if not possible:
                    # A failed journal write also prevents safely issuing later commands.
                    session.health = "reconciliation_required"
                try:
                    await asyncio.shield(self._persist(session, in_progress=possible))
                except (Exception, asyncio.CancelledError):
                    _LOGGER.error(
                        "SolarEdge could not persist final control outcome; write-ahead record remains",
                        exc_info=True,
                    )
                if isinstance(err, asyncio.CancelledError):
                    raise
                return False

    def _preflight(self, key, value, *, native=False):
        entity = self._control_entity_map.get(key)
        state = self.hass.states.get(entity) if entity else None
        if state is None or str(state.state) in _UNAVAILABLE:
            raise ValueError(f"Missing or unavailable {key}")
        command_entry = self._registry_entry(
            self._control_entity_map.get("storage_command_mode")
        )
        entry = self._registry_entry(entity)
        if command_entry is not None and (
            entry is None
            or not command_entry.device_id
            or entry.device_id != command_entry.device_id
            or entry.config_entry_id != command_entry.config_entry_id
        ):
            raise ValueError(f"{key} does not belong to the selected inverter")
        attrs = state.attributes or {}
        if self._control_candidate_rejection(entity, key):
            raise ValueError(f"Unusable control for {key}")
        if entity.startswith("number."):
            value = float(value)
            if (
                not native
                and key in {"charge_power_limit", "discharge_power_limit"}
                and str(attrs.get("unit_of_measurement", "")).lower() == "kw"
            ):
                value /= 1000
            if not math.isfinite(value) or not math.isfinite(float(state.state)):
                raise ValueError(f"Invalid numeric {key}")
            minimum = float(attrs.get("min", attrs.get("native_min_value", 0)))
            maximum = float(attrs.get("max", attrs.get("native_max_value", math.inf)))
            if not math.isfinite(minimum) or math.isnan(maximum) or maximum <= minimum:
                raise ValueError(f"Invalid range for {key}")
            if not minimum <= value <= maximum:
                raise ValueError(f"{key} outside supported range")
            step = attrs.get("step", attrs.get("native_step"))
            if step is not None and float(step) > 0:
                steps = (value - minimum) / float(step)
                if not math.isclose(steps, round(steps), abs_tol=1e-6):
                    raise ValueError(f"{key} does not match supported step")
        elif entity.startswith("select."):
            if (
                value not in (attrs.get("options") or [])
                or state.state not in attrs["options"]
            ):
                raise ValueError(f"Unsupported option or baseline for {key}")
        elif entity.startswith("switch."):
            if value not in {"on", "off"} or state.state not in {"on", "off"}:
                raise ValueError(f"Unsupported switch state for {key}")
        return key, entity, value

    def _option(self, key, aliases):
        entity = self._control_entity_map.get(key)
        option = self._match_select_option(entity, aliases) if entity else None
        if option is None:
            raise ValueError(f"No supported option for {key}")
        return option

    async def force_charge(
        self, duration_minutes=30, power_w=0, *, automatic=False
    ) -> bool:
        return await self._force(
            "charge", duration_minutes, power_w, automatic=automatic
        )

    async def force_discharge(
        self, duration_minutes=30, power_w=0, *, automatic=False
    ) -> bool:
        return await self._force(
            "discharge", duration_minutes, power_w, automatic=automatic
        )

    async def _force(self, command, duration_minutes, power_w, *, automatic=False):
        def plan():
            option = self._option(
                "storage_command_mode",
                _CHARGE_OPTIONS if command == "charge" else _DISCHARGE_OPTIONS,
            )
            power_key = (
                "charge_power_limit" if command == "charge" else "discharge_power_limit"
            )
            if (
                not math.isfinite(float(power_w))
                or float(power_w) < 0
                or not math.isfinite(float(duration_minutes))
                or float(duration_minutes) <= 0
            ):
                raise ValueError("Invalid power or duration")
            result = [
                (
                    "storage_control_mode",
                    self._option("storage_control_mode", _REMOTE_CONTROL_OPTIONS),
                )
            ]
            # A bounded dispatch requires a writable lease.
            result.append(("command_timeout", max(60, int(duration_minutes * 60))))
            if command == "charge" and "allow_grid_charge" in self._control_entity_map:
                entity = self._control_entity_map["allow_grid_charge"]
                if entity.startswith("switch."):
                    result.append(("allow_grid_charge", "on"))
                else:
                    policy = self._match_select_option(
                        entity, ("always allowed", "enabled", "on", "allowed")
                    )
                    if policy is not None:
                        result.append(("allow_grid_charge", policy))
                    elif not self._is_explicit_grid_charge_option(option):
                        raise ValueError("No supported grid-charge policy")
            opposite = (
                "discharge_power_limit" if command == "charge" else "charge_power_limit"
            )
            result.extend(
                [
                    (opposite, 0),
                    (power_key, self._coerce_target_power(power_key, power_w)),
                    ("storage_command_mode", option),
                ]
            )
            return result

        return await self._mutate("force_" + command, plan, automatic=automatic)

    @staticmethod
    def _snapshot_is_active(saved):
        command = _normalize_option(str(saved.get("storage_command_mode") or ""))
        return command in {
            _normalize_option(alias)
            for alias in (
                *_CHARGE_OPTIONS,
                *_DISCHARGE_OPTIONS,
                "charge from clipped solar power",
                "charge from solar power",
                "discharge to minimize import",
            )
        }

    def _saved_control_state_contains_active_dispatch(self):
        return self._snapshot_is_active(self._saved_control_state or {})

    async def restore_normal(
        self, *, automatic=False, expected_generation=None
    ) -> bool:
        def plan():
            session = self._coordinator()
            if self._snapshot_is_active(session.baseline or {}):
                raise ValueError("Saved dispatch cannot be restored")
            if not session.owned:
                command_entity = self._control_entity_map.get("storage_command_mode")
                command_state = (
                    self.hass.states.get(command_entity) if command_entity else None
                )
                if (
                    command_state is None
                    or str(command_state.state) in _UNAVAILABLE
                    or self._snapshot_is_active(
                        {"storage_command_mode": command_state.state}
                    )
                ):
                    raise ValueError("Cannot stop dispatch without an owned baseline")
            result = []
            for key, expected in session.owned.items():
                if not session.baseline or key not in session.baseline:
                    raise ValueError("No baseline for owned field")
                entity = self._control_entity_map.get(key)
                state = self.hass.states.get(entity) if entity else None
                baseline = session.baseline[key]
                if state is None or not (
                    self._control_values_match(entity, state.state, expected)
                    or self._control_values_match(entity, state.state, baseline)
                ):
                    raise ValueError(
                        f"External change to {key}; supervised reconciliation required"
                    )
                result.append((key, baseline))
            # Restore the benign command before restoring its limits and lease.
            result.sort(
                key=lambda item: (
                    item[0] != "storage_command_mode",
                    item[0] == "storage_control_mode",
                )
            )
            return result

        return await self._mutate(
            "restore_normal",
            plan,
            automatic=automatic,
            expected_generation=expected_generation,
            restoring=True,
        )

    async def set_backup_reserve(self, percent, *, automatic=False) -> bool:
        return await self._mutate(
            "set_backup_reserve",
            lambda: [("backup_reserve", percent)],
            automatic=automatic,
        )

    async def get_backup_reserve(self) -> int | None:
        self._ensure_entity_map()
        reserve = self._read_float("backup_reserve")
        if reserve is None:
            reserve = self._read_control_float("backup_reserve")
        return int(round(reserve)) if reserve is not None else None

    async def set_backup_mode(self, *, automatic=False) -> bool:
        def plan():
            soc = self._read_float("battery_level")
            if soc is not None and "backup_reserve" in self._control_entity_map:
                return [("backup_reserve", int(round(soc)))]
            return [
                ("charge_power_limit", 0),
                ("discharge_power_limit", 0),
                (
                    "storage_command_mode",
                    self._option("storage_command_mode", _IDLE_OPTIONS),
                ),
                (
                    "storage_control_mode",
                    self._option("storage_control_mode", _REMOTE_CONTROL_OPTIONS),
                ),
            ]

        return await self._mutate("set_backup_mode", plan, automatic=automatic)

    async def restore_work_mode_from_idle(self, *, automatic=False) -> bool:
        return await self.restore_normal(automatic=automatic)

    async def set_operation_mode(self, mode: str) -> bool:
        if mode in {"self_consumption", "autonomous", "normal"}:
            return await self.restore_normal()
        return False

    async def run_external_mutation(self, callback, *, automatic=False) -> bool:
        """Serialize one active-power mutation; its callback must not retry or clean up."""
        session = self._coordinator()
        if automatic and session.lock.locked():
            # Do not replace the in-flight operation's diagnostic result.
            return False
        async with session.lock:
            await self._load_session(session)
            if session.health != "ready":
                if (
                    not session.last_mutation
                    or session.last_mutation["outcome"] != "unknown"
                ):
                    self._result(
                        session,
                        SolarEdgeMutationOutcome.REJECTED,
                        "blocked",
                        message="Control requires reconciliation",
                    )
                return False
            session.generation += 1
            possible = False
            try:
                session.pending_mutation = {"operation": "active_power", "operation_id": session.generation}
                await self._persist(session, in_progress=True)
                possible = True
                if not await callback():
                    raise RuntimeError(
                        "External control returned an indeterminate failure"
                    )
                self._result(
                    session,
                    SolarEdgeMutationOutcome.CONFIRMED,
                    "active_power",
                    confirmation_source="service_return",
                )
                await self._persist(session)
                return True
            except (Exception, asyncio.CancelledError) as err:
                self._result(
                    session,
                    SolarEdgeMutationOutcome.UNKNOWN
                    if possible
                    else SolarEdgeMutationOutcome.REJECTED,
                    "active_power",
                    possibly_transmitted=possible,
                    message=str(err),
                )
                session.health = "reconciliation_required"
                try:
                    await asyncio.shield(self._persist(session, in_progress=possible))
                except (Exception, asyncio.CancelledError):
                    _LOGGER.error(
                        "SolarEdge external outcome journal failed", exc_info=True
                    )
                if isinstance(err, asyncio.CancelledError):
                    raise
                return False

    def _native_readback(self, snapshot):
        result = dict(snapshot)
        for key in ("charge_power_limit", "discharge_power_limit"):
            entity = self._control_entity_map.get(key)
            state = self.hass.states.get(entity) if entity else None
            if (
                state
                and str(state.attributes.get("unit_of_measurement", "")).lower() == "kw"
            ):
                result[key] /= 1000
        policy = self._control_entity_map.get("allow_grid_charge", "")
        if policy.startswith("switch.") and "allow_grid_charge" in result:
            result["allow_grid_charge"] = {
                "Always Allowed": "on",
                "Disabled": "off",
            }.get(result["allow_grid_charge"])
            if result["allow_grid_charge"] is None:
                del result["allow_grid_charge"]
        return result

    async def _fresh_storage_state(self):
        from .solaredge_readback import async_read_storage_baseline

        command_entity = self._control_entity_map.get("storage_command_mode")
        if not command_entity:
            return None
        snapshot = await async_read_storage_baseline(self.hass, command_entity)
        return self._native_readback(snapshot) if snapshot else None

    async def _confirm_mutation(self, key, entity, value):
        command_entry = self._registry_entry(
            self._control_entity_map.get("storage_command_mode")
        )
        if (
            command_entry is not None
            and command_entry.platform == "solaredge_modbus_multi"
        ):
            snapshot = await self._fresh_storage_state()
            return snapshot is not None and self._control_values_match(
                entity, snapshot.get(key), value
            )
        return await self._wait_for_reflected_state(entity, value)

    async def reconcile(self) -> bool:
        """Check a fresh upstream register poll without writing inverter controls.

        Existing baselines must match in full. Without a saved baseline, a benign
        storage snapshot can be adopted. Storage polls cannot clear uncertain
        active-power writes. Unsupported readback contracts remain blocked.
        """
        session = self._coordinator()
        async with session.lock:
            await self._load_session(session)
            if (
                session.last_mutation
                and session.last_mutation.get("operation") == "active_power"
                and session.health != "ready"
            ):
                return False
            observed = await self._fresh_storage_state()
            if observed is None or self._snapshot_is_active(observed):
                return False
            command = _normalize_option(str(observed.get("storage_command_mode", "")))
            control = _normalize_option(str(observed.get("storage_control_mode", "")))
            benign_command = command in {
                _normalize_option(value)
                for value in (
                    *_SELF_USE_OPTIONS,
                    *_IDLE_OPTIONS,
                    "solar power only (off)",
                )
            }
            if not benign_command or control not in {
                _normalize_option(value)
                for value in (*_REMOTE_CONTROL_OPTIONS, *_SELF_USE_OPTIONS)
            }:
                return False
            if session.baseline:
                for key, value in session.baseline.items():
                    entity = self._control_entity_map.get(key)
                    if not entity or not self._control_values_match(
                        entity, observed.get(key), value
                    ):
                        return False
            old_owned, old_baseline = session.owned, session.baseline
            old_result = session.last_mutation
            session.owned, session.baseline = {}, None
            session.health = "ready"
            session.generation += 1
            self._result(
                session,
                SolarEdgeMutationOutcome.CONFIRMED,
                "reconcile",
                confirmation_source="fresh_upstream_storage_poll",
            )
            try:
                await self._persist(session)
            except (Exception, asyncio.CancelledError) as err:
                session.owned, session.baseline = old_owned, old_baseline
                session.last_mutation = old_result
                session.health = "reconciliation_required"
                if isinstance(err, asyncio.CancelledError):
                    raise
                return False
            return True

    def _ensure_entity_map(self) -> None:
        if not self._entity_map and not self._control_entity_map:
            self._discover_entities()

    def _discover_entities(self) -> None:
        self._entity_map = {}
        self._control_entity_map = {}

        entity_ids: list[str] = []
        if self._solaredge_entry_id:
            from homeassistant.helpers import entity_registry as er

            registry = er.async_get(self.hass)
            entries = er.async_entries_for_config_entry(
                registry, self._solaredge_entry_id
            )
            entity_ids.extend(entry.entity_id for entry in entries if entry.entity_id)

        entity_ids.extend(
            state.entity_id
            for state in self.hass.states.async_all()
            if state.entity_id.startswith(("sensor.", "number.", "select.", "switch."))
            and state.entity_id not in entity_ids
        )
        self._discover_entities_from_ids(entity_ids, legacy_prefix=self._prefix or None)

    def _discover_entities_from_ids(
        self,
        entity_ids: list[str],
        legacy_prefix: str | None = None,
    ) -> None:
        for key, suffixes in _ENERGY_READ_ENTITIES.items():
            entity_id = self._resolve_entity_id(entity_ids, "sensor", suffixes, legacy_prefix, key)
            if entity_id:
                self._entity_map[key] = entity_id
        for key, (domain, suffixes) in _CONTROL_ENTITIES.items():
            entity_id = self._resolve_control_entity_id(
                entity_ids, domain, suffixes, legacy_prefix, key
            )
            if not entity_id and key == "allow_grid_charge":
                entity_id = self._resolve_control_entity_id(
                    entity_ids, "select", suffixes, legacy_prefix, key
                )
            if entity_id:
                self._control_entity_map[key] = entity_id
        self._battery_power_entity_ids = self._discover_battery_power_entities(entity_ids)

    def _discover_battery_power_entities(self, entity_ids: list[str]) -> list[str]:
        """Return one power entity for each mapped SolarEdge battery channel."""
        candidates: dict[int, list[str]] = {}
        prefix = f"{self._prefix.lower()}_" if self._prefix else ""
        for entity_id in entity_ids:
            if not entity_id.startswith("sensor."):
                continue
            body = entity_id.split(".", 1)[-1].lower()
            if prefix and not body.startswith(prefix):
                continue
            match = re.search(r"(?:^|_)b(\d+)_(?:dc_)?power$", body)
            if not match:
                match = re.search(r"(?:^|_)battery(\d+)_(?:dc_)?power$", body)
            if not match:
                continue
            channel = int(match.group(1))
            if self.hass.states.get(entity_id) is not None:
                candidates.setdefault(channel, []).append(entity_id)

        selected = [
            sorted(channel_entities, key=lambda entity_id: self._match_score(entity_id, "battery_power"))[0]
            for channel_entities in candidates.values()
        ]
        selected.sort()
        if not selected and self._entity_map.get("battery_power"):
            selected = [self._entity_map["battery_power"]]
        return selected

    def _resolve_control_entity_id(
        self,
        entity_ids: list[str],
        domain: str,
        suffixes: tuple[str, ...],
        prefix: str | None,
        key: str,
    ) -> str | None:
        """Return the first usable control candidate in suffix order."""
        for suffix in suffixes:
            entity_id = self._resolve_entity_id(
                entity_ids,
                domain,
                (suffix,),
                prefix,
                key,
            )
            if not entity_id:
                continue
            rejection = self._control_candidate_rejection(entity_id, key)
            if rejection:
                _LOGGER.debug(
                    "SolarEdge rejected %s candidate %s: %s",
                    key,
                    entity_id,
                    rejection,
                )
                continue
            return entity_id
        return None

    def _resolve_entity_id(
        self,
        entity_ids: list[str],
        domain: str,
        suffixes: tuple[str, ...],
        legacy_prefix: str | None,
        key: str,
    ) -> str | None:
        if legacy_prefix:
            for suffix in suffixes:
                candidate = f"{domain}.{legacy_prefix}_{suffix}"
                if self.hass.states.get(candidate) is not None:
                    return candidate

        domain_prefix = f"{domain}."
        matches: list[str] = []
        for suffix in suffixes:
            candidate = f"{domain}.{suffix}"
            if candidate in entity_ids and self.hass.states.get(candidate) is not None:
                matches.append(candidate)

            tail = f"_{suffix}"
            matches.extend(
                entity_id
                for entity_id in entity_ids
                if entity_id.startswith(domain_prefix) and entity_id.endswith(tail)
            )

        valid_matches = [
            entity_id
            for entity_id in dict.fromkeys(matches)
            if self.hass.states.get(entity_id) is not None
        ]
        if key == "battery_power":
            valid_matches = [
                entity_id
                for entity_id in valid_matches
                if not self._is_inverter_dc_entity(entity_id)
            ]
        if key == "inverter_dc_power":
            valid_matches = [
                entity_id
                for entity_id in valid_matches
                if not self._is_explicit_battery_power_entity(entity_id)
            ]
        if key.startswith("solar") or key.startswith("pv"):
            valid_matches = [
                entity_id
                for entity_id in valid_matches
                if not self._is_non_solar_power_entity(entity_id)
            ]
        if not valid_matches:
            return None
        return sorted(valid_matches, key=lambda entity_id: self._match_score(entity_id, key))[0]

    def _control_candidate_rejection(self, entity_id: str, key: str) -> str | None:
        """Return why a writable entity cannot safely fulfil a control role."""
        if key not in _CONTROL_ENTITIES:
            return None

        state = self.hass.states.get(entity_id)
        if state is None:
            return "entity has no state"

        domain = entity_id.split(".", 1)[0]
        body = entity_id.split(".", 1)[-1].lower()
        if key == "storage_control_mode":
            if "_limit_control_mode" in body:
                return "Limit Control Mode controls export or production, not storage"
            options = (getattr(state, "attributes", {}) or {}).get("options") or []
            recognised = {
                _normalize_option(alias)
                for alias in (*_REMOTE_CONTROL_OPTIONS, *_SELF_USE_OPTIONS)
            }
            if options and not any(
                _normalize_option(str(option)) in recognised for option in options
            ):
                return "selector has no recognised storage-control options"

        if domain == "number" and key in {
            "charge_power_limit",
            "discharge_power_limit",
        }:
            attrs = getattr(state, "attributes", {}) or {}
            unit = str(attrs.get("unit_of_measurement", "")).lower()
            if (
                "ac_charge_limit" in body or "accharge_limit" in body
            ) and unit and unit not in {"w", "kw"}:
                return (
                    "AC Charge Limit controls an energy or production-policy limit"
                )
            try:
                minimum = float(attrs.get("min", attrs.get("native_min_value", 0)))
                maximum_value = attrs.get("max", attrs.get("native_max_value"))
                maximum = (
                    float(maximum_value) if maximum_value is not None else None
                )
            except (TypeError, ValueError):
                return "numeric range is invalid"
            if not math.isfinite(minimum) or (
                maximum is not None and not math.isfinite(maximum)
            ):
                return "numeric range is not finite"
            if maximum is not None and (maximum == 0 or maximum <= minimum):
                return f"numeric range is unusable ({minimum} to {maximum})"

        return None

    @staticmethod
    def _is_explicit_grid_charge_option(option: str) -> bool:
        """Return whether a storage command explicitly permits grid charging."""
        normalized = _normalize_option(option)
        return "grid" in normalized or re.search(r"\bac\b", normalized) is not None

    @staticmethod
    def _is_non_solar_power_entity(entity_id: str) -> bool:
        body = entity_id.split(".", 1)[-1].lower()
        return any(token in body for token in ("_b", "battery", "_m", "meter", "grid"))

    @staticmethod
    def _is_inverter_dc_entity(entity_id: str) -> bool:
        body = entity_id.split(".", 1)[-1].lower()
        return bool(
            re.search(r"(?:^|_)i\d+_dc_power$", body)
            or body.endswith("_inverter_dc_power")
            or body.endswith("_inverter1_dc_power")
        )

    @staticmethod
    def _is_explicit_battery_power_entity(entity_id: str) -> bool:
        body = entity_id.split(".", 1)[-1].lower()
        return bool(
            re.search(r"(?:^|_)b\d+_(?:dc_)?power$", body)
            or re.search(r"(?:^|_)battery\d+_(?:dc_)?power$", body)
            or body.endswith("_battery_power")
        )

    def _match_score(self, entity_id: str, key: str) -> tuple[int, int, str]:
        body = entity_id.split(".", 1)[-1].lower()
        if key.startswith("battery"):
            role = 0 if ("_b" in body or "battery" in body) else 1
        elif key.startswith("grid") or "grid_" in key:
            role = 0 if ("_m" in body or "meter" in body or "grid" in body) else 1
        elif key.startswith("solar") or key.startswith("pv"):
            role = 0 if ("_i" in body or "solar" in body or "pv" in body) and "_b" not in body and "_m" not in body else 1
        elif key.startswith("load"):
            role = 0 if ("load" in body or "home" in body or "consumption" in body) else 1
        else:
            role = 0
        prefix_penalty = 0 if not self._prefix or body.startswith(f"{self._prefix.lower()}_") else 1
        return (role + prefix_penalty, len(entity_id), entity_id)

    def _read_control_float(self, key: str) -> float | None:
        entity_id = self._control_entity_map.get(key)
        state = self.hass.states.get(entity_id) if entity_id else None
        if not state or str(state.state) in _UNAVAILABLE:
            return None
        try:
            value = float(state.state)
            return value if math.isfinite(value) else None
        except (TypeError, ValueError):
            return None

    def _coerce_target_power(self, key: str, power_w: float) -> float:
        try:
            requested = float(power_w or 0)
        except (TypeError, ValueError):
            requested = 0.0
        if requested > 0:
            return requested

        entity_id = self._control_entity_map.get(key)
        state = self.hass.states.get(entity_id) if entity_id else None
        attrs = getattr(state, "attributes", {}) or {}
        for attr in ("max", "native_max_value"):
            try:
                max_value = float(attrs.get(attr))
            except (TypeError, ValueError):
                continue
            if max_value > 0:
                unit = str(attrs.get("unit_of_measurement", "")).lower()
                return max_value * 1000.0 if unit == "kw" else max_value
        return 5000.0

    def _match_select_option(
        self, entity_id: str, aliases: tuple[str, ...]
    ) -> str | None:
        state = self.hass.states.get(entity_id)
        attrs = getattr(state, "attributes", {}) or {}
        options = attrs.get("options") or []
        normalized_aliases = {_normalize_option(alias) for alias in aliases}

        # Prefer an exact supported option across the complete list before
        # falling back to substring aliases. SolarEdge Modbus Multi orders
        # several charge variants before "Charge from Solar Power and Grid";
        # matching the generic "charge" alias inline would select the first
        # clipped-solar option instead of the requested grid-charge command.
        for alias in aliases:
            for option in options:
                if _normalize_option(str(option)) == _normalize_option(alias):
                    return str(option)
        current = getattr(state, "state", None)
        if current and _normalize_option(str(current)) in normalized_aliases:
            return str(current)
        return None

    async def _wait_for_reflected_state(
        self,
        entity_id: str,
        expected: Any,
    ) -> bool:
        """Check supporting entity reflection after a successful service return."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.WRITE_CONFIRM_TIMEOUT_SECONDS
        while True:
            state = self.hass.states.get(entity_id)
            current = getattr(state, "state", None)
            if self._control_values_match(entity_id, current, expected):
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(self.WRITE_CONFIRM_INTERVAL_SECONDS, remaining))

    @staticmethod
    def _control_values_match(
        entity_id: str,
        current: Any,
        expected: Any,
    ) -> bool:
        if current is None or str(current) in _UNAVAILABLE:
            return False
        if entity_id.startswith("number."):
            try:
                return math.isclose(float(current), float(expected))
            except (TypeError, ValueError):
                return False
        return _normalize_option(str(current)) == _normalize_option(str(expected))

    def _expected_entity_hint(self, key: str) -> str:
        suffixes = _ENERGY_READ_ENTITIES.get(key) or ()
        prefix = self._prefix or "solaredge_b1"
        suffix = suffixes[0] if suffixes else key
        return f"sensor.{prefix}_{suffix}"

    def _entity_exists(self, key: str) -> bool:
        entity_id = self._entity_map.get(key)
        return bool(entity_id and self.hass.states.get(entity_id) is not None)

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
        entity_id = self._entity_map.get(key)
        return self._power_kw_from_entity_id(entity_id)

    def _power_kw_from_entity_id(self, entity_id: str | None) -> float | None:
        """Read one mapped power entity and normalize it to kW."""
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if not state or str(state.state) in _UNAVAILABLE:
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value):
            return None
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

    def _is_lifetime_energy_total(self, key: str) -> bool:
        entity_id = (self._entity_map.get(key) or "").lower()
        if not entity_id:
            return False
        return any(
            entity_id.endswith(f"_{suffix}") or entity_id.endswith(suffix)
            for suffix in _LIFETIME_ENERGY_TOTAL_ENTITIES.get(key, ())
        )

    def _battery_power_kw(self) -> float:
        if self._battery_power_entity_ids:
            channel_values = [
                self._power_kw_from_entity_id(entity_id)
                for entity_id in self._battery_power_entity_ids
            ]
            if all(value is not None for value in channel_values):
                # SolarEdge battery DC power is positive when charging and
                # negative when discharging; PowerSync uses the opposite.
                return -sum(value or 0.0 for value in channel_values)
            return 0.0

        discharge_kw = self._power_kw("battery_discharge")
        charge_kw = self._power_kw("battery_charge")
        if discharge_kw is not None or charge_kw is not None:
            return (discharge_kw or 0.0) - (charge_kw or 0.0)

        raw_kw = self._power_kw("battery_power")
        if raw_kw is None:
            return 0.0
        # SolarEdge battery power is positive when charging and negative when
        # discharging; PowerSync uses the opposite convention.
        return -raw_kw

    def _battery_dc_power_kw_for_solar(self) -> float | None:
        """Return raw SolarEdge battery DC power for PV reconstruction."""
        if self._battery_power_entity_ids:
            channel_values = [
                self._power_kw_from_entity_id(entity_id)
                for entity_id in self._battery_power_entity_ids
            ]
            if not all(value is not None for value in channel_values):
                return None
            return sum(value or 0.0 for value in channel_values)

        charge_kw = self._power_kw("battery_charge")
        discharge_kw = self._power_kw("battery_discharge")
        if charge_kw is not None or discharge_kw is not None:
            if charge_kw is None or discharge_kw is None:
                return None
            return charge_kw - discharge_kw

        return self._power_kw("battery_power")

    def _has_battery_power_source(self) -> bool:
        return bool(
            self._battery_power_entity_ids
            or any(
                key in self._entity_map
                for key in ("battery_power", "battery_charge", "battery_discharge")
            )
        )

    def _grid_power_kw(self) -> float:
        import_kw = self._power_kw("grid_import")
        export_kw = self._power_kw("grid_export")
        if import_kw is not None or export_kw is not None:
            return (import_kw or 0.0) - (export_kw or 0.0)

        raw_kw = self._power_kw("grid_power")
        if raw_kw is None:
            return 0.0
        # SolarEdge meter AC power is normally negative when importing and
        # positive when exporting; PowerSync uses positive import.
        return -raw_kw

    def _solar_power_kw(self) -> float:
        has_battery_source = self._has_battery_power_source()
        mapped_pv_keys = [
            f"pv{idx}_power"
            for idx in range(1, 5)
            if f"pv{idx}_power" in self._entity_map
        ]
        pv_values = [self._power_kw(key) for key in mapped_pv_keys]
        solar_kw = self._power_kw("solar_power")
        if not has_battery_source:
            # Preserve the published battery-free behavior: use the first
            # legacy solar/AC source and compare it with available PV strings.
            pv_kw = sum(value or 0.0 for value in pv_values)
            return max(0.0, max(solar_kw or 0.0, pv_kw))

        battery_dc_kw = self._battery_dc_power_kw_for_solar()
        if battery_dc_kw is None:
            # A missing battery channel makes the site's DC energy split
            # incomplete even when a PV-string sensor is still available.
            return 0.0

        if mapped_pv_keys and all(value is not None for value in pv_values):
            # PV string channels are the most direct solar-only source.
            return max(0.0, sum(value or 0.0 for value in pv_values))

        solar_entity_id = self._entity_map.get("solar_power")
        inverter_dc_entity_id = self._entity_map.get("inverter_dc_power")
        if (
            solar_kw is not None
            and solar_entity_id != inverter_dc_entity_id
            and not self._is_ac_solar_source(solar_entity_id)
        ):
            return max(0.0, solar_kw)

        inverter_dc_kw = self._power_kw("inverter_dc_power")
        if inverter_dc_kw is None:
            # Never expose contaminated i1_ac_power as PV on a battery system
            # when the DC reconstruction inputs are incomplete.
            return 0.0
        return max(0.0, inverter_dc_kw + battery_dc_kw)

    @staticmethod
    def _is_ac_solar_source(entity_id: str | None) -> bool:
        if not entity_id:
            return False
        body = entity_id.split(".", 1)[-1].lower()
        return bool(
            body == "ac_power"
            or body.endswith("_ac_power")
            or re.search(r"(?:^|_)i\d+_ac_power$", body)
        )

    def _daily_solar_energy_kwh(self) -> float | None:
        """Return a safe daily PV source, excluding contaminated AC counters."""
        entity_id = self._entity_map.get("daily_solar_energy")
        body = entity_id.split(".", 1)[-1].lower() if entity_id else ""
        inverter_ac_counter = bool(
            re.search(r"(?:^|_)i\d+_ac_energy_today$", body)
            or body.endswith("_inverter_ac_energy_today")
        )
        if inverter_ac_counter and self._has_battery_power_source():
            return None
        return self._energy_kwh("daily_solar_energy")
