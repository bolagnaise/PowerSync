"""Native iHomeManager commands with existing SH inverter telemetry."""
from __future__ import annotations

import math
import time

from .const import (
    CONF_MONITORING_MODE,
    CONF_OPTIMIZATION_BACKUP_RESERVE,
    CONF_SUNGROW_IHOMEMANAGER_HOST,
    CONF_SUNGROW_IHOMEMANAGER_PORT,
    DOMAIN,
    CONF_SUNGROW_HOST, CONF_SUNGROW_PORT, CONF_SUNGROW_SLAVE_ID,
    CONF_SUNGROW_CONNECTION_TYPE, SUNGROW_CONNECTION_IHOMEMANAGER_NATIVE,
)
from .coordinator import SungrowEnergyCoordinator
from .sungrow_ihomemanager_config import native_connection_settings
from .inverters.sungrow_sh import SungrowSHController
from .inverters.sungrow_ihomemanager import SungrowIHomeManagerController
from .licensing.guard import (
    control_generation,
    control_entry_generation,
    service_allowed,
)


class _ReadOnlySungrowController(SungrowSHController):
    """Prevent inherited inverter helpers from ever sending an SH write."""

    async def _write_register(self, address: int, value: int) -> bool:
        return False

    async def get_battery_data(self):
        data = await super().get_battery_data()
        required = ("battery_soc", "battery_power", "pv_power", "load_power")
        powers = [data.get(key) for key in required]
        powers.append(data.get("meter_power", data.get("export_power")))
        self.telemetry_complete = all(
            type(value) in (int, float) and math.isfinite(value) for value in powers
        ) and 0 <= data.get("battery_soc", -1) <= 100
        return data


class SungrowIHomeManagerEnergyCoordinator(SungrowEnergyCoordinator):
    """Preserve established inverter power semantics; dispatch only through iHM.

    Unsupported inherited controls are blocked by telemetry_only=True. A second
    transport guard blocks SH writes even if a future inherited method omits
    that check. The native client independently fences every heartbeat renewal.
    """

    native_ihomemanager = True
    supports_idle_hold = False
    supports_spread_export = False
    MAX_TELEMETRY_AGE_SECONDS = 90

    def __init__(self, hass, host, port=502, slave_id=1, *, entry_id="",
                 ihomemanager_host="", ihomemanager_port=503, ac_inverter_source_id=None):
        native_connection_settings({
            CONF_SUNGROW_HOST: host, CONF_SUNGROW_PORT: port, CONF_SUNGROW_SLAVE_ID: slave_id,
            CONF_SUNGROW_IHOMEMANAGER_HOST: ihomemanager_host,
            CONF_SUNGROW_IHOMEMANAGER_PORT: ihomemanager_port,
        })
        super().__init__(hass, host, port, slave_id, entry_id=entry_id,
                         ac_inverter_source_id=ac_inverter_source_id, telemetry_only=True)
        self._controller = _ReadOnlySungrowController(host, port, slave_id)
        self._native_ready = False
        self._telemetry_at = None
        self._native_at = None
        self._stopping = False
        self._lease_permission = {"source": "user", "entry_id": entry_id}
        self._native_host = ihomemanager_host.strip()
        self._native_port = int(ihomemanager_port)
        self._native = SungrowIHomeManagerController(
            self._native_host, self._native_port, control_allowed=self._dispatch_allowed,
        )

    def _entry(self):
        return self.hass.config_entries.async_get_entry(self._entry_id)

    def _dispatch_allowed(self, permission=None) -> bool:
        entry = self._entry()
        if self._stopping or entry is None or not self.startup_control_ready():
            return False
        options = {**entry.data, **entry.options}
        if options.get(CONF_MONITORING_MODE, True):
            return False
        # Stop renewal as soon as the endpoint or configured route is changed,
        # even before HA reaches the reload/unload callback.
        if options.get(CONF_SUNGROW_CONNECTION_TYPE) != SUNGROW_CONNECTION_IHOMEMANAGER_NATIVE:
            return False
        try:
            settings = native_connection_settings(options)
            if (settings[CONF_SUNGROW_IHOMEMANAGER_HOST] != self._native_host
                    or settings[CONF_SUNGROW_IHOMEMANAGER_PORT] != self._native_port
                    or options.get(CONF_SUNGROW_HOST) != self.host
                    or int(options.get(CONF_SUNGROW_PORT, 502)) != int(self.port)
                    or int(options.get(CONF_SUNGROW_SLAVE_ID, 1)) != int(self.slave_id)):
                return False
            reserve = float(options.get(CONF_OPTIMIZATION_BACKUP_RESERVE, 20))
            if 0 < reserve <= 1:
                reserve *= 100
            self._native.set_soc_limits(reserve)
        except (TypeError, ValueError):
            return False
        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry_id)
        if not isinstance(runtime, dict) or runtime.get("_monitoring_handoff_active"):
            return False
        permission = self._lease_permission if permission is None else permission
        if permission.get("source") == "optimizer":
            optimizer = runtime.get("optimization_coordinator")
            if optimizer is None or optimizer.enabled is not True:
                return False
        return service_allowed(self.hass, permission)

    def startup_control_ready(self) -> bool:
        return bool(
            not self._stopping and self._native_ready
            and self._telemetry_at is not None and self._native_at is not None
            and time.monotonic() - self._telemetry_at <= self.MAX_TELEMETRY_AGE_SECONDS
            and time.monotonic() - self._native_at <= self.MAX_TELEMETRY_AGE_SECONDS
            and super().startup_control_ready()
        )

    @property
    def pending_optimizer_export_restore(self) -> bool:
        return False  # This route never acquires an inverter export limit.

    async def _async_update_data(self):
        data = await super()._async_update_data()
        if data.get("telemetry_ready") is True and getattr(self._controller, "telemetry_complete", False):
            self._telemetry_at = time.monotonic()
        else:
            self._telemetry_at = None
            data["telemetry_ready"] = False
        try:
            snapshot = await self._native.read_snapshot()
            self._native_ready = snapshot.supports_dispatch
            self._native_at = time.monotonic() if self._native_ready else None
            data["ihomemanager_protocol_version"] = f"0x{snapshot.protocol_version:08X}"
            data["ihomemanager_max_charge_power_w"] = snapshot.max_charge_w
            data["ihomemanager_max_discharge_power_w"] = snapshot.max_discharge_w
        except Exception:
            self._native_ready = False
            self._native_at = None
        data["control_available"] = bool(self._native_ready and data.get("telemetry_ready"))
        data["telemetry_source"] = "sungrow_inverter_with_ihomemanager_control"
        data["battery_control_source"] = "sungrow_ihomemanager_native"
        data["supports_idle_hold"] = False
        data["supports_spread_export"] = False
        return data

    async def _dispatch(self, action, duration_minutes, power_w, source):
        permission = {"source": "user", "entry_id": self._entry_id}
        generation = control_generation()
        if source == "optimizer" or generation is not None:
            permission.update(
                source="optimizer", _licensing_generation=generation,
                _licensing_entry_generation=control_entry_generation(),
            )
        try:
            if isinstance(duration_minutes, bool) or isinstance(power_w, bool):
                return False
            seconds = float(duration_minutes) * 60
            power_w = float(power_w)
            if not math.isfinite(seconds) or not 0 < seconds <= self._native.MAX_DURATION_SECONDS:
                return False
            if not math.isfinite(power_w) or power_w < 0:
                return False
            if power_w == 0:
                key = "ihomemanager_max_charge_power_w" if action == "charge" else "ihomemanager_max_discharge_power_w"
                power_w = (self.data or {}).get(key, 0)
            async with self._modbus_lock:
                if not self._dispatch_allowed(permission):
                    return False
                previous_permission = self._lease_permission
                self._lease_permission = permission
                accepted = False
                try:
                    accepted = await self._native.start_dispatch(action, power_w, seconds)
                    return accepted
                finally:
                    if not accepted:
                        self._lease_permission = previous_permission
        except (TypeError, ValueError):
            return False

    async def force_charge(self, duration_minutes=30, power_w=0, *, source="user"):
        return await self._dispatch("charge", duration_minutes, power_w, source)

    async def force_discharge(self, duration_minutes=30, power_w=0, *, source="user"):
        return await self._dispatch("discharge", duration_minutes, power_w, source)

    async def restore_normal(self):
        # Cleanup remains available after monitoring, stale telemetry or licence
        # revocation. The native driver restores only the session it owns.
        async with self._modbus_lock:
            return await self._native.stop_dispatch()

    async def async_shutdown(self):
        self._stopping = True
        await self._native.async_shutdown()
        await super().async_shutdown()
