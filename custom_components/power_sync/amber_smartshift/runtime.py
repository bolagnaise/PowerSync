"""Telemetry, durable override ownership and command admission for Amber."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import math

from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import ALLOW_ENABLE, CAPABILITIES, CONFIG, EMAIL, MANAGED, PASSWORD, SITE
from .client import AmberAuthError, AmberClient, AmberError

_LOGGER = logging.getLogger(__name__)
MODES = {"charge", "discharge", "preserve-charge", "self-consume"}
MAX_AGE = 90


def utcnow():
    return datetime.now(timezone.utc)


def timestamp(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


class AmberBatteryCoordinator(DataUpdateCoordinator):
    """One site, one mutation lock, and no implicit ownership of app overrides."""

    def __init__(self, hass, entry, *, client=None, store=None):
        self.entry = entry
        self.entry_id = entry.entry_id
        self.site = self.option(SITE)
        self.config = self.option(CONFIG)
        self.client = client or AmberClient(
            async_get_clientsession(hass), hass.async_add_executor_job,
            self.option(EMAIL), self.option(PASSWORD),
        )
        self.store = store or Store(hass, 1, f"power_sync_amber_smartshift_{self.entry_id}")
        self.owned = {}
        self.last_read = None
        self.last_earnings_slot = None
        self.earnings_data = {}
        self.earnings_read = None
        self.snapshot = {}
        self.command_state = "idle"
        self.command_error = None
        self.stopping = False
        self._loaded = False
        locks = hass.data.setdefault("power_sync_amber_site_locks", {})
        self.lock = locks.setdefault(self.site, asyncio.Lock())
        super().__init__(hass, _LOGGER, name="PowerSync Amber battery", update_interval=timedelta(seconds=30))

    def option(self, key, default=None):
        return self.entry.options.get(key, self.entry.data.get(key, default))

    def entry_data(self):
        return self.hass.data.get("power_sync", {}).get(self.entry_id, {})

    async def _save(self):
        await self.store.async_save({"site": self.site, "config": self.config, "owned": self.owned})

    async def _load(self):
        if not self._loaded:
            saved = await self.store.async_load() or {}
            if not isinstance(saved, dict) or not isinstance(saved.get("owned", {}), dict):
                raise AmberError("Amber override recovery journal is invalid; control remains blocked")
            if saved and (saved.get("site") != self.site or saved.get("config") != self.config):
                if saved.get("owned"):
                    raise AmberError("Previous Amber battery cleanup must finish before changing site")
            self.owned = saved.get("owned", {})
            self._loaded = True

    def active_override(self):
        override = self.snapshot.get("override") or {}
        end = timestamp(override.get("validTo") or override.get("estimatedEndDate"))
        if end and end <= utcnow():
            return {}
        return override

    def owner(self):
        active = self.active_override()
        if not active:
            return None
        if active.get("overrideId") == self.owned.get("id"):
            return self.owned.get("source")
        return "external"

    def renewal_due(self):
        """An optimiser must revisit its safety gates before renewing a lease."""
        if self.owner() not in (None, "optimizer"):
            return False
        end = timestamp(self.active_override().get("validTo") or self.active_override().get("estimatedEndDate"))
        return not end or end <= utcnow() + timedelta(minutes=5)

    def force_state(self):
        """Only force charge/export participate in the optimiser force timer."""
        override = self.active_override()
        owner = self.owner()
        if not override or (owner == "optimizer" and override.get("value") not in ("charge", "discharge")):
            return {"active": False}
        return {
            "active": True,
            "type": {"charge": "charge", "discharge": "discharge", "self-consume": "self_consumption", "preserve-charge": "hold"}.get(override.get("value"), "hold"),
            "source": owner or "external", "scope": "optimizer" if owner == "optimizer" else "external",
            "expires_at": override.get("validTo") or override.get("estimatedEndDate"),
            "hardware_expires_at": override.get("validTo") or override.get("estimatedEndDate"),
        }

    async def _read(self):
        if not isinstance(self.site, str) or not self.site or not isinstance(self.config, str) or not self.config:
            raise AmberError("A validated Amber site and battery identity are required")
        snapshot = await self.client.stats(self.site)
        if snapshot.get("strategy", {}).get("configId") != self.config:
            raise AmberError("Amber battery configuration changed; reconnect the battery")
        self.snapshot = snapshot
        self.last_read = utcnow()

    def _meter(self, key):
        entity = self.option(key)
        state = self.hass.states.get(entity) if entity else None
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        value = number(state.state)
        unit = state.attributes.get("unit_of_measurement")
        if value is None or unit not in ("W", "kW"):
            return None
        reported = timestamp(getattr(state, "last_reported", None) or getattr(state, "last_updated", None))
        if not reported or (utcnow() - reported).total_seconds() > MAX_AGE:
            return None
        return value / 1000 if unit == "W" else value

    def _soc(self):
        value = number(self.snapshot.get("live", {}).get("stateOfChargePercentage"))
        return value if value is not None and 0 <= value <= 100 else None

    def _floor(self):
        opt = self.entry_data().get("optimization_coordinator")
        config = getattr(opt, "_config", None)
        value = number(getattr(config, "backup_reserve", self.option("optimization_backup_reserve", 0.2)))
        if value is None or not 0 <= value <= 1:
            raise HomeAssistantError("Amber software reserve is invalid")
        return value * 100

    def _cap(self):
        value = number(self.option("optimization_grid_charge_soc_cap", 1.0))
        if value is None or not 0 <= value <= 1:
            raise HomeAssistantError("Amber charge SOC limit is invalid")
        return value * 100

    def _current_price(self, channel):
        prices = self.entry_data().get("amber_coordinator")
        for interval in (getattr(prices, "data", None) or {}).get("current", []):
            if not isinstance(interval, dict) or interval.get("channelType") != channel:
                continue
            start, end = timestamp(interval.get("startTime")), timestamp(interval.get("endTime"))
            if interval.get("type") in ("CurrentInterval", "ActualInterval") and start and end and start <= utcnow() < end:
                value = number(interval.get("perKwh"))
                if value is not None:
                    return value / 100
        return None

    def _admit(self, mode, source, call_data):
        if self.stopping:
            raise HomeAssistantError("Amber connection is stopping")
        if self.option("electricity_provider", "amber") != "amber" or self.option("amber_site_id") != self.site:
            raise HomeAssistantError("Amber battery and pricing sites must match")
        if not self.last_read or (utcnow() - self.last_read).total_seconds() > MAX_AGE or self._soc() is None:
            raise HomeAssistantError("Fresh Amber battery telemetry is required")
        automatic = source not in ("manual", "user")
        if automatic:
            if not self.option(MANAGED, False) or self.option("monitoring_mode", False) or self.entry_data().get("_monitoring_handoff_active"):
                raise HomeAssistantError("Automatic Amber battery control is disabled")
            if source == "optimizer" and not self.option("optimization_enabled", True):
                raise HomeAssistantError("The PowerSync optimiser is disabled")
            if source == "optimizer":
                opt = self.entry_data().get("optimization_coordinator")
                solved = timestamp(getattr(opt, "_last_update_time", None))
                if not solved or (utcnow() - solved).total_seconds() > 360:
                    raise HomeAssistantError("A fresh optimiser plan is required to renew Amber control")
            from ..licensing.guard import service_allowed
            if not service_allowed(self.hass, call_data, "handle_force_" + mode):
                raise HomeAssistantError("HA optimiser access is required")
            if self.entry_data().get("calibration_suspected"):
                raise HomeAssistantError("Battery calibration blocks automatic Amber control")
            if any(self._meter(key) is None for key in (
                "custom_grid_power_entity", "custom_solar_power_entity", "custom_load_power_entity"
            )):
                raise HomeAssistantError("Fresh grid, solar and home-load meters are required for automatic Amber control")
            prices = self.entry_data().get("amber_coordinator")
            price_data = getattr(prices, "data", None) or {}
            updated = timestamp(price_data.get("last_update"))
            if not getattr(prices, "last_update_success", False) or not updated or (utcnow() - updated).total_seconds() > 360:
                raise HomeAssistantError("Fresh Amber prices are required for automatic control")
            if self.owner() in ("manual", "user", "external"):
                raise HomeAssistantError("A manual or Amber-app override owns the battery")
        if self.owner() == "external":
            raise HomeAssistantError("Cancel the external override in Amber before issuing a PowerSync override")
        if mode == "charge":
            if self._soc() >= self._cap():
                raise HomeAssistantError("Amber charge SOC limit reached")
            if automatic and not self.option("optimization_allow_grid_charge", True):
                raise HomeAssistantError("Grid charging is disabled")
            if automatic and self._current_price("general") is None:
                raise HomeAssistantError("A current Amber import interval is required")
            if self.entry_data().get("grid_charging_disabled_for_demand"):
                raise HomeAssistantError("Demand protection blocks grid charging")
            opt = self.entry_data().get("optimization_coordinator")
            if automatic and opt:
                if opt._grid_charge_blackout_active_now() or opt._amber_spike_protected_slots(1, utcnow())[0]:
                    raise HomeAssistantError("Blackout or spike protection blocks grid charging")
        if mode == "discharge":
            if self._soc() <= self._floor():
                raise HomeAssistantError("Amber software reserve reached")
            if automatic:
                raw_price = self._current_price("feedIn")
                # Public Amber feed-in is a cost; PowerSync uses positive earnings.
                earnings = -raw_price if raw_price is not None else None
                floor = number(self.option("optimization_min_export_price", 0))
                if earnings is None or earnings <= 0 or floor is None or earnings < max(0, floor):
                    raise HomeAssistantError("Current Amber export price does not permit battery export")
            # Amber cannot honor a dynamic export-power envelope.
            guard = self.entry_data().get("network_export_guard")
            if guard and guard.manager.snapshot.mode != "off":
                raise HomeAssistantError("Amber mode control cannot enforce the network export envelope")
            opt = self.entry_data().get("optimization_coordinator")
            if automatic and opt and (opt._scheduled_ev_preserve_active() or opt._should_block_export_for_demand()):
                raise HomeAssistantError("EV or demand protection blocks battery export")

    def api_data(self):
        live = self.snapshot.get("live", {})
        power = number(live.get("batteryPowerW"))
        capacity = number(self.snapshot.get("capacity_wh"))
        override = self.active_override()
        start = timestamp(override.get("validFrom"))
        end = timestamp(override.get("validTo") or override.get("estimatedEndDate"))
        status = self.snapshot.get("strategy", {}).get("status")
        fresh = bool(self.last_read and (utcnow() - self.last_read).total_seconds() <= MAX_AGE)
        earnings_current = bool(self.earnings_read and int(self.earnings_read.timestamp()) // 300 == int(utcnow().timestamp()) // 300)
        return {
            "battery_level": self._soc() if fresh else None,
            "battery_power": power / 1000 if power is not None and fresh and self._soc() is not None else None,
            "battery_capacity_kwh": capacity / 1000 if capacity is not None and capacity > 0 else None,
            "solar_power": self._meter("custom_solar_power_entity"),
            "grid_power": self._meter("custom_grid_power_entity"),
            "load_power": self._meter("custom_load_power_entity"),
            "telemetry_ready": fresh and self._soc() is not None,
            "amber_smartshift": {
                "entry_id": self.entry_id,
                "capabilities": dict(CAPABILITIES), "managed": self.option(MANAGED, False),
                "smartshift_enabled": True if status == "enabled" else False if status == "disabled" else None,
                "owner": self.owner(), "override": dict(override),
                "override_active_now": bool(start and end and start <= utcnow() < end),
                "power_state": live.get("powerState"), "power_state_description": live.get("powerStateDescription"),
                "last_read": self.last_read.isoformat() if self.last_read else None,
                "telemetry_age_seconds": (utcnow() - self.last_read).total_seconds() if self.last_read else None,
                "command_state": self.command_state, "command_error": self.command_error,
                "requested_action": self.owned.get("mode"),
                "interval_earnings_cents": dict(self.earnings_data) if earnings_current else {},
                "interval_earnings_read": self.earnings_read.isoformat() if earnings_current else None,
                "power_is_estimated_in_plan": True,
            },
        }

    def project_optimizer_status(self, data):
        """Keep a solver's intended action separate from provider readback."""
        native = self.api_data()
        status = native["amber_smartshift"]
        data["amber_smartshift"] = status
        reported = {"charge": "charge", "discharge": "export", "preserve-charge": "idle", "self-consume": "self_consumption"}.get(status["override"].get("value"))
        if status["override"] and not status["override_active_now"]:
            reported = "pending"
        fallback = {True: "amber_managed", False: "smartshift_disabled", None: "unknown"}[status["smartshift_enabled"]]
        effective = (reported or fallback) if native["telemetry_ready"] else "unknown"
        data["effective_current_action"] = data["current_action"] = effective
        detail = "Amber-reported override; check measured battery power for physical effect" if reported else "No PowerSync battery override is reported"
        if not native["telemetry_ready"]:
            detail = "Fresh Amber battery status is unavailable"
        data["effective_current_action_detail"] = data["current_action_detail"] = detail
        data["current_action_end_time"] = status["override"].get("validTo") or status["override"].get("estimatedEndDate")
        measured = native.get("battery_power")
        data["current_power_w"] = abs(measured * 1000) if measured is not None else None
        data["actual_battery_power_w"] = data["current_power_w"]
        data["control_power_is_estimated"] = True
        data["idle_hold_active"] = effective == "idle"
        data["idle_hold_reserve"] = data["idle_hold_reserve_percent"] = None
        return data

    def _coordinator_data(self):
        data = self.api_data()
        data["telemetry_observed_at"] = {
            "battery_level": self.last_read if data["battery_level"] is not None else None,
            "battery_power": self.last_read if data["battery_power"] is not None else None,
        }
        return data

    def publish(self):
        self.async_set_updated_data(self._coordinator_data())

    async def _cancel_owned(self):
        active = self.active_override()
        if (self.owned.get("phase") == "adding" and not self.owned.get("id")) or (self.owned.get("phase") == "confirming" and not active):
            until = timestamp(self.owned.get("end"))
            if not until or until > utcnow():
                raise AmberError("An unconfirmed Amber command requires reconciliation or expiry")
        if self.owned.get("id") and active.get("overrideId") == self.owned["id"]:
            self.owned["phase"] = "cancelling"
            await self._save()
            await self.client.cancel(self.site, self.config, self.owned["id"])
            for _ in range(3):
                await asyncio.sleep(2)
                await self._read()
                if self.active_override().get("overrideId") != self.owned["id"]:
                    break
            else:
                raise AmberError("Amber has not confirmed override cancellation")
        self.owned = {}
        await self._save()

    async def restore_normal(self, *, automatic=False):
        async with self.lock:
            await self._load()
            await self._read()
            if automatic and self.owned.get("source") in ("manual", "user"):
                return False
            try:
                await self._cancel_owned()
                self.command_state, self.command_error = "cancelled", None
            except AmberError:
                self.command_state, self.command_error = "cleanup_pending", "Amber override cleanup is pending"
                raise HomeAssistantError(self.command_error) from None
            finally:
                self.publish()
        return True

    async def command(self, mode, duration=30, *, source="manual", power_w=0, call_data=None):
        if mode not in MODES:
            raise HomeAssistantError("Unsupported Amber battery mode")
        duration = number(duration)
        if duration is None or not 1 <= duration <= 240:
            raise HomeAssistantError("Amber overrides must last between 1 and 240 minutes")
        if number(power_w) != 0:
            raise HomeAssistantError("Amber supports battery modes, not target watts")
        automatic = source not in ("manual", "user")
        async with self.lock:
            try:
                await self._load()
                await self._read()
                self._admit(mode, source, call_data or {})
                if self.owned.get("phase") in ("adding", "confirming", "cancelling"):
                    raise HomeAssistantError("Amber command reconciliation is pending")
                end = timestamp(self.active_override().get("validTo") or self.active_override().get("estimatedEndDate"))
                can_keep_duration = not automatic or (end and end <= utcnow() + timedelta(minutes=min(duration, 10), seconds=30))
                if self.owner() == source and self.active_override().get("value") == mode and end and end > utcnow() + timedelta(minutes=5) and can_keep_duration:
                    return True
                if self.snapshot["strategy"].get("status") != "enabled":
                    if not self.option(ALLOW_ENABLE, False):
                        raise HomeAssistantError("Enable Smart Shift or allow PowerSync to enable it for overrides")
                    def enable_guard():
                        self._admit(mode, source, call_data or {})
                        if not self.option(ALLOW_ENABLE, False):
                            raise HomeAssistantError("Permission to enable Smart Shift was withdrawn")
                    await self.client.enable(self.config, True, before_write=enable_guard)
                    await asyncio.sleep(5)
                    await self._read()
                    if self.snapshot["strategy"].get("status") != "enabled":
                        raise AmberError("Smart Shift enablement has not been confirmed")
                    self._admit(mode, source, call_data or {})
                await self._cancel_owned()
                self._admit(mode, source, call_data or {})
                now = utcnow()
                end = now + timedelta(minutes=min(duration, 10) if automatic else duration)
                self.owned = {"source": source, "mode": mode, "end": end.isoformat(), "phase": "adding",
                              "admission": {key: value for key, value in (call_data or {}).items()
                                            if key in ("entry_id", "source", "_licensing_generation", "_licensing_entry_generation")}}
                await self._save()
                self._admit(mode, source, call_data or {})
                response = await self.client.add(self.site, self.config, mode,
                    now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    end.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    before_write=lambda: self._admit(mode, source, call_data or {}))
                self.owned["id"] = response["overrideId"]
                self.owned["phase"] = "confirming"
                await self._save()
                self.command_state = "acknowledged"
                for _ in range(3):
                    await asyncio.sleep(2)
                    await self._read()
                    active = self.active_override()
                    if active.get("overrideId") == self.owned["id"] and active.get("value") == mode:
                        self.owned["phase"] = "active"
                        await self._save()
                        self.command_state, self.command_error = "reported", None
                        return True
                raise AmberError("Amber has not reported the requested override")
            except (AmberError, HomeAssistantError) as exc:
                if isinstance(exc, HomeAssistantError) and self.owned.get("phase") == "adding" and not self.owned.get("id"):
                    # Admission failed before transport. This is a known no-write,
                    # unlike an API error after an add might have reached Amber.
                    self.owned = {}
                    await self._save()
                self.command_state = "unconfirmed" if self.owned else "blocked"
                self.command_error = str(exc)
                raise HomeAssistantError(self.command_error) from None
            finally:
                self.publish()

    async def set_smartshift(self, enabled):
        async with self.lock:
            if self.stopping:
                raise HomeAssistantError("Amber connection is stopping")
            await self._load()
            await self._read()
            if self.owner() == "external":
                raise HomeAssistantError("An external Amber override owns the battery")
            await self._cancel_owned()
            def switch_guard():
                if self.stopping:
                    raise HomeAssistantError("Amber connection is stopping")
            await self.client.enable(self.config, enabled, before_write=switch_guard)
            await asyncio.sleep(5)
            await self._read()
            if self.snapshot["strategy"].get("status") != ("enabled" if enabled else "disabled"):
                raise HomeAssistantError("Amber has not confirmed the Smart Shift setting")
            if not enabled:
                self.hass.config_entries.async_update_entry(self.entry, options={**self.entry.options, MANAGED: False})
            self.publish()

    async def _async_update_data(self):
        async with self.lock:
            try:
                await self._load()
                await self._read()
                if self.owned and self.owner() != "external":
                    active = self.active_override()
                    unsafe = self.stopping or self.owned.get("phase") == "cancelling"
                    if self.owned.get("source") not in ("manual", "user"):
                        try:
                            self._admit(self.owned["mode"], self.owned["source"], self.owned.get("admission", {}))
                        except HomeAssistantError:
                            unsafe = True
                    soc = self._soc()
                    unsafe |= soc is None or (self.owned.get("mode") == "charge" and soc >= self._cap())
                    unsafe |= soc is None or (self.owned.get("mode") == "discharge" and soc <= self._floor())
                    confirming = self.owned.get("phase") in ("adding", "confirming")
                    end = timestamp(self.owned.get("end"))
                    if unsafe or (not active and (not confirming or (end and end <= utcnow()))):
                        await self._cancel_owned()
                        self.command_state = "cancelled"
                    elif active.get("overrideId") == self.owned.get("id") and active.get("value") == self.owned.get("mode"):
                        if self.owned.get("phase") != "active":
                            self.owned["phase"] = "active"
                            await self._save()
                        self.command_state, self.command_error = "reported", None
                slot = int(utcnow().timestamp()) // 300
                if slot != self.last_earnings_slot and utcnow().second >= 15:
                    self.last_earnings_slot = slot
                    self.earnings_data = {}  # Do not carry a previous interval forward.
                    try:
                        self.earnings_data = await self.client.earnings(self.site)
                        self.earnings_read = utcnow()
                    except AmberError:
                        self.earnings_read = None
                return self._coordinator_data()
            except AmberAuthError:
                raise ConfigEntryAuthFailed("Reconnect Amber Smart Shift") from None
            except (AmberError, HomeAssistantError):
                raise UpdateFailed("Amber battery refresh or owned-override cleanup failed") from None

    async def shutdown(self):
        self.stopping = True
        self.update_interval = None
        try:
            await asyncio.wait_for(self.restore_normal(), timeout=25)
        except (TimeoutError, AmberError, HomeAssistantError):
            _LOGGER.warning("Amber cleanup remains pending; override expiry and durable recovery remain active")
        await self.async_shutdown()
