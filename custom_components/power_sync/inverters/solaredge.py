"""SolarEdge inverter controller for active-power curtailment.

Uses SolarEdge Modbus TCP/SunSpec for telemetry and the SolarEdge power
control register 0xF001 for active power limiting. If direct Modbus is not
available, falls back to known Home Assistant SolarEdge Modbus number entities.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, Optional

from .base import InverterController, InverterState, InverterStatus

_LOGGER = logging.getLogger(__name__)

_UNAVAILABLE = {"", "unknown", "unavailable", "none", "None"}

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
_CHARGE_OPTIONS = ("charge", "charge battery", "charging", "force charge", "remote charge")
_DISCHARGE_OPTIONS = (
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
        "dc_power",
        "battery_power",
        "battery1_power",
        "battery_power_charge",
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
        "i1_ac_energy_today",
        "solar_energy_today",
        "today_solar_energy",
        "daily_solar_energy",
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
        self.entity_prefix = (entity_prefix or "").strip()
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
            entity = self._active_power_limit_entity or self._find_active_power_limit_entity()
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
                result = await self._try_modbus_call(
                    self._client.write_register,
                    address=self.REG_ACTIVE_POWER_LIMIT,
                    value=int(percent),
                )
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


class SolarEdgeEnergyController:
    """Bridge SolarEdge Home battery telemetry and control through HA entities."""

    def __init__(
        self,
        hass: Any,
        entity_prefix: str = "",
        solaredge_entry_id: str | None = None,
    ) -> None:
        self.hass = hass
        self._prefix = entity_prefix.strip()
        self._solaredge_entry_id = (solaredge_entry_id or "").strip()
        self._entity_map: dict[str, str] = {}
        self._control_entity_map: dict[str, str] = {}
        self._saved_control_state: dict[str, Any] | None = None

    async def connect(self) -> bool:
        """Validate that at least SolarEdge battery SOC can be read."""
        self._discover_entities()
        if not self._entity_exists("battery_level"):
            hint = self._expected_entity_hint("battery_level")
            raise ValueError(f"solaredge_missing_entities:{hint}")

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
            "daily_solar_energy_kwh": self._energy_kwh("daily_solar_energy"),
            "daily_grid_import_kwh": None if grid_import_is_total else grid_import_kwh,
            "daily_grid_export_kwh": None if grid_export_is_total else grid_export_kwh,
            "total_grid_import_kwh": grid_import_kwh if grid_import_is_total else None,
            "total_grid_export_kwh": grid_export_kwh if grid_export_is_total else None,
            "daily_battery_charge_kwh": self._energy_kwh("daily_battery_charge"),
            "daily_battery_discharge_kwh": self._energy_kwh("daily_battery_discharge"),
            "control_entities": dict(self._control_entity_map),
            "control_available": self.control_available(),
            "missing_control_entities": self.missing_control_entities(),
        }
        if grid_import_is_total:
            status["total_grid_import_entity_id"] = self._entity_map.get("daily_grid_import")
        if grid_export_is_total:
            status["total_grid_export_entity_id"] = self._entity_map.get("daily_grid_export")

        for idx in range(1, 5):
            status[f"pv{idx}_power"] = self._power_kw(f"pv{idx}_power")

        return status

    async def disconnect(self) -> None:
        """No persistent connection to close."""

    def control_available(self) -> bool:
        """Return whether the minimum writable surface for dispatch exists."""
        self._ensure_entity_map()
        required = (
            "storage_control_mode",
            "storage_command_mode",
            "charge_power_limit",
            "discharge_power_limit",
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
        )
        return [key for key in required if not self._control_entity_map.get(key)]

    async def force_charge(self, duration_minutes: int = 30, power_w: float = 0) -> bool:
        """Force SolarEdge battery charging through HA control entities."""
        return await self._force("charge", duration_minutes, power_w)

    async def force_discharge(self, duration_minutes: int = 30, power_w: float = 0) -> bool:
        """Force SolarEdge battery discharging through HA control entities."""
        return await self._force("discharge", duration_minutes, power_w)

    async def restore_normal(self) -> bool:
        """Restore SolarEdge storage controls to the saved or self-use state."""
        self._ensure_entity_map()
        if self._saved_control_state:
            ok = await self._restore_saved_control_state()
            if ok:
                self._saved_control_state = None
            return ok

        ok = True
        ok &= await self._set_number_if_mapped("charge_power_limit", 0)
        ok &= await self._set_number_if_mapped("discharge_power_limit", 0)
        ok &= await self._set_number_if_mapped("command_timeout", 0)
        ok &= await self._set_select_by_alias("storage_command_mode", _IDLE_OPTIONS)
        ok &= await self._set_select_by_alias("storage_control_mode", _SELF_USE_OPTIONS)
        return bool(ok)

    async def set_backup_reserve(self, percent: int) -> bool:
        """Set SolarEdge backup reserve / minimum SOC when exposed by HA."""
        self._ensure_entity_map()
        if "backup_reserve" not in self._control_entity_map:
            _LOGGER.error("SolarEdge backup reserve write failed: no backup reserve number entity")
            return False
        return await self._set_number_if_mapped("backup_reserve", max(0, min(100, int(percent))))

    async def get_backup_reserve(self) -> int | None:
        """Read SolarEdge backup reserve / minimum SOC."""
        reserve = self._read_float("backup_reserve")
        if reserve is None and "backup_reserve" in self._control_entity_map:
            reserve = self._read_control_float("backup_reserve")
        return int(round(reserve)) if reserve is not None else None

    async def set_backup_mode(self) -> bool:
        """Hold the battery at current SOC by raising reserve or idling dispatch."""
        soc = self._read_float("battery_level")
        if soc is not None and "backup_reserve" in self._control_entity_map:
            return await self.set_backup_reserve(int(round(soc)))

        self._ensure_entity_map()
        if not self.control_available():
            _LOGGER.error(
                "SolarEdge hold SOC failed: missing control entities: %s",
                ", ".join(self.missing_control_entities()),
            )
            return False
        self._save_current_control_state()
        ok = True
        ok &= await self._set_number_if_mapped("charge_power_limit", 0)
        ok &= await self._set_number_if_mapped("discharge_power_limit", 0)
        ok &= await self._set_select_by_alias("storage_command_mode", _IDLE_OPTIONS)
        ok &= await self._set_select_by_alias("storage_control_mode", _REMOTE_CONTROL_OPTIONS)
        return bool(ok)

    async def restore_work_mode_from_idle(self) -> bool:
        """Exit optimizer idle hold."""
        return await self.restore_normal()

    async def set_operation_mode(self, mode: str) -> bool:
        """Map PowerSync operation modes onto SolarEdge self-use/normal mode."""
        if mode in {"self_consumption", "autonomous", "normal"}:
            return await self.restore_normal()
        _LOGGER.debug("SolarEdge operation mode %s is not mapped", mode)
        return False

    async def _force(self, command: str, duration_minutes: int, power_w: float) -> bool:
        self._ensure_entity_map()
        if not self.control_available():
            _LOGGER.error(
                "SolarEdge force %s failed: missing control entities: %s",
                command,
                ", ".join(self.missing_control_entities()),
            )
            return False

        self._save_current_control_state()
        duration_seconds = max(60, int(duration_minutes) * 60)
        target_power = self._coerce_target_power("charge_power_limit" if command == "charge" else "discharge_power_limit", power_w)

        try:
            ok = True
            ok &= await self._set_select_by_alias("storage_control_mode", _REMOTE_CONTROL_OPTIONS)
            ok &= await self._set_number_if_mapped("command_timeout", duration_seconds)
            if command == "charge":
                ok &= await self._set_optional_grid_charge(True)
                ok &= await self._set_number_if_mapped("discharge_power_limit", 0)
                ok &= await self._set_number_if_mapped("charge_power_limit", target_power)
                ok &= await self._set_select_by_alias("storage_command_mode", _CHARGE_OPTIONS)
            else:
                ok &= await self._set_number_if_mapped("charge_power_limit", 0)
                ok &= await self._set_number_if_mapped("discharge_power_limit", target_power)
                ok &= await self._set_select_by_alias("storage_command_mode", _DISCHARGE_OPTIONS)
            if not ok:
                await self.restore_normal()
            return bool(ok)
        except Exception as err:
            _LOGGER.error("SolarEdge force %s failed: %s", command, err, exc_info=True)
            await self.restore_normal()
            return False

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
            entity_id = self._resolve_entity_id(entity_ids, domain, suffixes, legacy_prefix, key)
            if not entity_id and key == "allow_grid_charge":
                entity_id = self._resolve_entity_id(entity_ids, "select", suffixes, legacy_prefix, key)
            if entity_id:
                self._control_entity_map[key] = entity_id

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
        if not valid_matches:
            return None
        return sorted(valid_matches, key=lambda entity_id: self._match_score(entity_id, key))[0]

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

    def _save_current_control_state(self) -> None:
        if self._saved_control_state is not None:
            return
        self._saved_control_state = {}
        for key, entity_id in self._control_entity_map.items():
            state = self.hass.states.get(entity_id)
            if state and str(state.state) not in _UNAVAILABLE:
                self._saved_control_state[key] = state.state

    async def _restore_saved_control_state(self) -> bool:
        ok = True
        for key, value in (self._saved_control_state or {}).items():
            entity_id = self._control_entity_map.get(key)
            if not entity_id:
                continue
            domain = entity_id.split(".", 1)[0]
            if domain == "number":
                ok &= await self._set_number_if_mapped(key, value)
            elif domain == "select":
                ok &= await self._select_option(entity_id, str(value))
            elif domain == "switch":
                ok &= await self._set_switch(entity_id, str(value).lower() == "on")
        return bool(ok)

    def _read_control_float(self, key: str) -> float | None:
        entity_id = self._control_entity_map.get(key)
        state = self.hass.states.get(entity_id) if entity_id else None
        if not state or str(state.state) in _UNAVAILABLE:
            return None
        try:
            return float(state.state)
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

    async def _set_number_if_mapped(self, key: str, value: Any) -> bool:
        entity_id = self._control_entity_map.get(key)
        if not entity_id:
            return key not in {"charge_power_limit", "discharge_power_limit"}
        state = self.hass.states.get(entity_id)
        attrs = getattr(state, "attributes", {}) or {}
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            _LOGGER.error("SolarEdge number write failed for %s: invalid value %r", entity_id, value)
            return False
        unit = str(attrs.get("unit_of_measurement", "")).lower()
        if unit == "kw" and abs(numeric) > 100:
            numeric = numeric / 1000.0
        try:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": entity_id, "value": numeric},
                blocking=True,
            )
            return True
        except Exception as err:
            _LOGGER.error("SolarEdge number write failed for %s: %s", entity_id, err)
            return False

    async def _set_select_by_alias(self, key: str, aliases: tuple[str, ...]) -> bool:
        entity_id = self._control_entity_map.get(key)
        if not entity_id:
            return False
        option = self._match_select_option(entity_id, aliases)
        if option is None:
            _LOGGER.error("SolarEdge select %s has no matching option for %s", entity_id, aliases)
            return False
        return await self._select_option(entity_id, option)

    def _match_select_option(self, entity_id: str, aliases: tuple[str, ...]) -> str | None:
        state = self.hass.states.get(entity_id)
        attrs = getattr(state, "attributes", {}) or {}
        options = attrs.get("options") or []
        normalized_aliases = {_normalize_option(alias) for alias in aliases}
        for option in options:
            normalized_option = _normalize_option(str(option))
            if (
                normalized_option in normalized_aliases
                or any(alias in normalized_option for alias in normalized_aliases)
            ):
                return str(option)
        current = getattr(state, "state", None)
        if current and _normalize_option(str(current)) in normalized_aliases:
            return str(current)
        return None

    async def _select_option(self, entity_id: str, option: str) -> bool:
        try:
            await self.hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": entity_id, "option": option},
                blocking=True,
            )
            return True
        except Exception as err:
            _LOGGER.error("SolarEdge select write failed for %s=%s: %s", entity_id, option, err)
            return False

    async def _set_optional_grid_charge(self, enabled: bool) -> bool:
        entity_id = self._control_entity_map.get("allow_grid_charge")
        if not entity_id:
            return True
        domain = entity_id.split(".", 1)[0]
        if domain == "switch":
            return await self._set_switch(entity_id, enabled)
        if domain == "select":
            aliases = ("on", "enabled", "enable", "allowed", "allow") if enabled else (
                "off",
                "disabled",
                "disable",
                "not allowed",
                "disallow",
            )
            return await self._set_select_by_alias("allow_grid_charge", aliases)
        return True

    async def _set_switch(self, entity_id: str, enabled: bool) -> bool:
        try:
            await self.hass.services.async_call(
                "switch",
                "turn_on" if enabled else "turn_off",
                {"entity_id": entity_id},
                blocking=True,
            )
            return True
        except Exception as err:
            _LOGGER.error("SolarEdge switch write failed for %s: %s", entity_id, err)
            return False

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

    def _is_lifetime_energy_total(self, key: str) -> bool:
        entity_id = (self._entity_map.get(key) or "").lower()
        if not entity_id:
            return False
        return any(
            entity_id.endswith(f"_{suffix}") or entity_id.endswith(suffix)
            for suffix in _LIFETIME_ENERGY_TOTAL_ENTITIES.get(key, ())
        )

    def _battery_power_kw(self) -> float:
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
        pv_kw = sum(self._power_kw(f"pv{idx}_power") or 0.0 for idx in range(1, 5))
        solar_kw = self._power_kw("solar_power")
        if solar_kw is None:
            return pv_kw
        return max(0.0, max(solar_kw, pv_kw))
