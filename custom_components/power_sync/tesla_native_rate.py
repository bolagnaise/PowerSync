"""HA ports for positive current control inside explicit native Tesla windows."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import math
import time

from .optimization.tesla_native_rate import NativeRateController, OWNER_MODE, RateObservation, RateWriteResult, decide_rate, restoration_fits, governed_forecast
from .optimization.tesla_schedules import _near_site, _section, _timestamp, active_native_window
from .teslemetry_solar import guard_ev_control

RUNTIME_KEY = "tesla_native_rate"


def _fresh_at(state):
    value = getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
    return value.timestamp() if value else 0.0


def _finite(value):
    if isinstance(value, bool):
        raise ValueError("Invalid telemetry")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Invalid telemetry")
    return result


@asynccontextmanager
async def _license_lock(hass, entry_id, *, restore):
    manager = hass.data.get("power_sync_license_manager")
    if manager is None or restore:
        yield
    else:
        async with manager.control_lock(entry_id):
            yield


@guard_ev_control
async def _write_current(hass, config_entry, vehicle_id, observed, amps, authorize, *, restore=False, refresh=None, on_submit=None, license_held=False):
    """Exactly one number write, fenced after every await; no wake/start/stop."""
    from homeassistant.helpers import entity_registry as er
    async with _license_lock(hass, config_entry.entry_id, restore=restore or license_held):
        # Both interlocks may have waited. A previously eligible observation
        # cannot authorize a write after the car, window or site load changed.
        if refresh is not None:
            latest = await refresh()
            if (latest is None or latest.entity_id != observed.entity_id
                    or latest.identity != observed.identity
                    or latest.current_amps != observed.current_amps):
                return False
            observed = latest
        state = hass.states.get(observed.entity_id)
        registration = er.async_get(hass).async_get(observed.entity_id)
        identity = _entity_identity(registration)
        if (not authorize() or state is None or identity != observed.identity
                or not 0 <= time.time()-_fresh_at(state) <= 90
                or _finite(state.state) != observed.current_amps):
            return False
        minimum = max(1, math.ceil(_finite(state.attributes.get("min"))))
        maximum = min(80, math.floor(_finite(state.attributes.get("max"))))
        if type(amps) is not int or not minimum <= amps <= maximum:
            return False
        submitted = time.time()
        previous_report = _fresh_at(state)
        # No await between the last authorization and provider submission.
        if not authorize():
            return False
        if on_submit is not None:
            on_submit()
        await hass.services.async_call("number", "set_value",
            {"entity_id": observed.entity_id, "value": amps}, blocking=True)
        # A submitted command is not readback. Do not wake or refresh another
        # provider; require this selected entity to report after submission.
        for _ in range(10):
            current = hass.states.get(observed.entity_id)
            registration = er.async_get(hass).async_get(observed.entity_id)
            if not authorize() or _entity_identity(registration) != observed.identity:
                return False
            if (current and _fresh_at(current) > previous_report
                    and _fresh_at(current) >= submitted and _finite(current.state) == amps):
                return True
            await asyncio.sleep(0.5)
        return False


def _entity_identity(entry):
    if entry is None or not getattr(entry, "device_id", None) or not getattr(entry, "unique_id", None):
        return None
    return f"{entry.platform}:{entry.config_entry_id}:{entry.device_id}:{entry.unique_id}"


async def _write_in_scope(hass, config_entry, vehicle_id, ports, observation, amps, authorize, submitted):
    """Use the same lock order as managed EV commands, including cleanup."""
    session = ports.controller.sessions.get(vehicle_id, {})
    restore = session.get("recovering") is True or ports.controller.stopping
    async with _license_lock(hass, config_entry.entry_id, restore=restore):
        return await _write_in_solar_scope(hass, config_entry, vehicle_id, ports,
                                           observation, amps, authorize, submitted)


@guard_ev_control
async def _write_in_solar_scope(hass, config_entry, vehicle_id, ports, observation, amps, authorize, submitted):
    # Existing managed optimiser commands acquire licence before SolarEV.
    # Keep that global order, then take the shared site/battery locks.
    if ports.budget is not None:
        async with ports.budget.admission_lock(ports.site):
            return await ports._admitted_write(observation, amps, authorize, submitted,
                                               license_held=True)
    return await ports._admitted_write(observation, amps, authorize, submitted,
                                       license_held=True)


class NativeRatePorts:
    def __init__(self, hass, entry, setting, budget=None):
        from .native_rate_budget import site_identity
        self.hass, self.entry, self.setting = hass, entry, setting
        self.budget = budget
        self.site = site_identity(entry)
        self.pairing = self._pairing_identity()
        self._runtime = hass.data.get("power_sync", {}).get(entry.entry_id)
        self.controller = None
        self.observations = {}

    def _pairing_identity(self):
        config = {**self.entry.data, **self.entry.options}
        values = (config.get(key) for key in ("powerwall_local_energy_site_id", "tesla_energy_site_id",
                  "powerwall_local_din", "powerwall_local_public_key_der"))
        return hashlib.sha256(repr(tuple(values)).encode()).hexdigest()

    @property
    def admits_increases(self):
        return self.budget is not None and self.site is not None

    def coexistence_reason(self):
        from .native_rate_admission import native_coexistence_reason
        if self.budget is None or self.site is None:
            return "A paired site and shared charging budget are required"
        return native_coexistence_reason(self.hass, self.site, self.budget)

    def waiting_reason(self):
        if self.runtime.get("native_rate_battery_transitions", 0):
            return "battery_control_changing"
        return None

    def session_context(self, observed):
        return {"watts_per_amp": observed.watts_per_amp, "pairing": self.pairing}

    def changed(self):
        if self.current():
            refresh = getattr(self.runtime.get("optimization_coordinator"), "_schedule_settings_reoptimization", None)
            if refresh:
                refresh()

    def forecast(self, vin, base, timestamps, interval_minutes, now):
        session = self.controller.sessions.get(vin)
        observed = self.observations.get(vin)
        if (session is None or observed is None or self.controller.stopping
                or session.get("recovering") or session.get("uncertain")
                or observed.window_id != session.get("window_id")
                or not self.permitted(vin, session["session"], False)):
            return base
        from .native_rate_telemetry import newer_schedule_report
        reader = getattr(self.runtime.get("optimization_coordinator"), "_tesla_schedule_reader", None)
        cached = reader.cache.get(vin) if reader is not None else None
        schedule_at, data = (cached[0], cached[1]) if cached else (None, {})
        newer, at = newer_schedule_report(self.hass, vin, schedule_at.timestamp() if schedule_at else 0)
        if newer is not None:
            schedule_at, data = datetime.fromtimestamp(at, timezone.utc), newer
        window = active_native_window(data=data, now=datetime.fromtimestamp(now, timestamps[0].tzinfo),
            schedule_read_at=schedule_at, latitude=self.hass.config.latitude,
            longitude=self.hass.config.longitude) if schedule_at and timestamps else None
        if window is None or window.identity != session["window_id"]:
            return base
        state = self.hass.states.get(observed.entity_id)
        from homeassistant.helpers import entity_registry as er
        try:
            current_amps = _finite(state.state) if state is not None else None
        except (TypeError, ValueError):
            return base
        if (state is None or not 0 <= now-_fresh_at(state) <= 90
                or current_amps != session.get("confirmed_amps")
                or _entity_identity(er.async_get(self.hass).async_get(observed.entity_id)) != observed.identity):
            return base
        return governed_forecast(base, observed, session["baseline_amps"], timestamps, interval_minutes, now)

    def update_armed_sites(self):
        if self.budget is None:
            return
        sites = set()
        for runtime in self.hass.data.get("power_sync", {}).values():
            controller = runtime.get(RUNTIME_KEY) if isinstance(runtime, dict) else None
            ports = getattr(controller, "ports", None)
            if ports is not None and ports.site is not None and ports.setting.rate_vins:
                sites.add(ports.site)
        self.budget.armed_sites = sites

    @property
    def runtime(self):
        return self._runtime if self._runtime is not None else {}

    def current(self):
        return (self._runtime is not None
                and self.hass.data.get("power_sync", {}).get(self.entry.entry_id) is self._runtime
                and self._runtime.get(RUNTIME_KEY) is self.controller
                and self._pairing_identity() == self.pairing)

    def enabled_vins(self):
        return self.setting.rate_vins

    def _lease(self, vin):
        from .automations.ev_ownership import get_ev_ownership
        return get_ev_ownership(self.hass, self.entry, vin)[1]

    def superseded(self, vin, session):
        from .automations.ev_ownership import get_ev_ownership
        # Other entries share the same physical VIN even when their local
        # profile IDs and ownership namespaces differ.
        for entry_id, runtime in self.hass.data.get("power_sync", {}).items():
            other = runtime.get("entry") if isinstance(runtime, dict) else None
            if entry_id != self.entry.entry_id and other is not None:
                if get_ev_ownership(self.hass, other, vin)[1] is not None:
                    return True
        lease = self._lease(vin)
        return bool(lease and (lease.get("owner_mode") != OWNER_MODE or lease.get("session_id") != session))

    def permitted(self, vin, session, restore):
        if not self.current() or self.superseded(vin, session):
            return False
        if any(gate.blocked(vin) for gate in self.hass.data.get("power_sync_native_solar", {}).values()):
            return False
        task = self.budget.provider_tasks.get(vin) if self.budget is not None else None
        if task is not None and not task.done() and task is not asyncio.current_task():
            return False
        if restore:
            # Durable journal, identity and current CAS authorize cleanup only.
            record = self.controller.sessions.get(vin, {}) if self.controller else {}
            return bool(session and session == record.get("session") and record.get("pairing") == self.pairing)
        if self.controller.stopping or (session and self.controller.sessions.get(vin, {}).get("recovering")):
            return False
        if self.coexistence_reason() is not None:
            return False
        config = {**self.entry.data, **self.entry.options}
        optimizer = self.runtime.get("optimization_coordinator")
        if (self.runtime.get("powerwall_local_dispatch_stopping")
                or config.get("monitoring_mode", False)
                or getattr(optimizer, "_monitoring_mode_active", lambda: True)()
                or not self.setting.rate_enabled(vin)
                or not getattr(optimizer, "_enabled", False)):
            return False
        manager = self.hass.data.get("power_sync_license_manager")
        if manager is not None and not manager.permits(self.entry.entry_id):
            return False
        observed = self.observations.get(vin)
        if observed is None:
            return False
        baseline = self.controller.sessions.get(vin, {}).get("baseline_amps", observed.current_amps)
        return decide_rate(observed, baseline, time.time())[0] is not None

    def claim(self, vin, session):
        from .automations.ev_ownership import claim_ev_ownership
        # Never apply the general external-owner override policy here.
        if self._lease(vin) is not None or not self.permitted(vin, None, False):
            return False
        claim_ev_ownership(self.hass, self.entry, vin, owner_mode=OWNER_MODE,
            session_id=session, reason="Rate only inside Tesla app window",
            extra={"controls_start_stop": False})
        return True

    def release(self, vin, session):
        from .automations.ev_ownership import release_ev_ownership
        lease = self._lease(vin)
        if lease and lease.get("owner_mode") == OWNER_MODE and lease.get("session_id") == session:
            release_ev_ownership(self.hass, self.entry, vin, reason="Native schedule rate lease ended")

    async def _entity(self, vin):
        from .automations.actions import (_get_ev_config, _resolve_ble_prefix_for_vehicle,
            _resolve_teslemetry_bt_prefix, _get_tesla_ev_entity)
        from .const import (EV_PROVIDER_TESLA_BLE, EV_PROVIDER_TESLEMETRY_BT,
                           EV_PROVIDER_BOTH, EV_PROVIDER_FLEET_API)
        from homeassistant.helpers import entity_registry as er
        # Once acquired, keep the exact provider identity through restoration.
        session = self.controller.sessions.get(vin, {})
        entity_id = session.get("entity_id")
        registry = er.async_get(self.hass)
        if not entity_id:
            candidates = []
            provider = _get_ev_config(self.entry)["ev_provider"]
            if provider in (EV_PROVIDER_TESLA_BLE, EV_PROVIDER_BOTH):
                prefix = _resolve_ble_prefix_for_vehicle(self.hass, self.entry, vin)
                if prefix:
                    candidates.append(f"number.{prefix}_charging_amps")
            if provider in (EV_PROVIDER_TESLEMETRY_BT, EV_PROVIDER_BOTH):
                prefix = _resolve_teslemetry_bt_prefix(self.hass)
                if str(prefix or "").upper() == vin:
                    candidates.append(f"number.{prefix}_charge_current_request")
            if provider in (EV_PROVIDER_FLEET_API, EV_PROVIDER_BOTH):
                candidate = await _get_tesla_ev_entity(self.hass,
                    r"number\..*(charging_amps|charge_current)(?:_\d+)?$", vin)
                if candidate:
                    candidates.append(candidate)
            entity_id = next((candidate for candidate in candidates
                if (state := self.hass.states.get(candidate)) is not None
                and str(state.state) not in ("unknown", "unavailable")
                and 0 <= time.time()-_fresh_at(state) <= 90
                and _entity_identity(registry.async_get(candidate))), None)
        state = self.hass.states.get(entity_id) if entity_id else None
        identity = _entity_identity(registry.async_get(entity_id)) if entity_id else None
        return state, identity

    async def read(self, vin):
        from homeassistant.util import dt as dt_util
        from .automations.ev_vehicle_capacity import resolve_ev_battery_capacity
        from .powerwall_local.battery_dispatch import site_grid_limits_w
        if not self.current():
            return None
        self.update_armed_sites()
        if self.budget is not None:
            from .native_rate_admission import refresh_managed_records
            await refresh_managed_records(self.hass, self.budget)
        optimizer = self.runtime.get("optimization_coordinator")
        reader = getattr(optimizer, "_tesla_schedule_reader", None)
        state, identity = await self._entity(vin)
        if state is None or identity is None:
            self.observations.pop(vin, None)
            return None
        session = self.controller.sessions.get(vin, {})
        cleanup = session.get("recovering") or self.controller.stopping
        cached = reader.cache.get(vin) if reader else None
        # Cleanup may outlive the native window, but a current increase still
        # needs the same fresh whole-site power budget as an active session.
        data = cached[1] if cached else {}
        from .native_rate_telemetry import vehicle_report, newer_schedule_report, charging_unit_w
        live = vehicle_report(self.hass, vin)
        charge = _section(live or data, "charge_state")
        location = _section(live or data, "drive_state") or _section(live or data, "location_data")
        now = dt_util.now()
        schedule_at = cached[0] if cached else None
        newer, at = newer_schedule_report(self.hass, vin, schedule_at.timestamp() if schedule_at else 0)
        if newer is not None:
            data = newer
            schedule_at = datetime.fromtimestamp(at, timezone.utc)
        window = active_native_window(data=data, now=now,
            schedule_read_at=schedule_at, latitude=self.hass.config.latitude,
            longitude=self.hass.config.longitude) if schedule_at else None
        config = {**self.entry.data, **self.entry.options}
        store_data = getattr(self.runtime.get("automation_store"), "_data", {}) or {}
        configs = store_data.get("vehicle_charging_configs", [])
        profile = next((value for value in configs if isinstance(value, dict)
            and str(value.get("vehicle_id", "")).upper() == vin), {}) if isinstance(configs, list) else {}
        capacity = resolve_ev_battery_capacity(manual_capacity_kwh=profile.get("battery_capacity_kwh"),
            provider_capacity_kwh=profile.get("provider_battery_capacity_kwh"),
            model=profile.get("vehicle_model", profile.get("model")),
            trim=profile.get("vehicle_trim", profile.get("trim"))).effective_battery_capacity_kwh
        local = (self.runtime.get("powerwall_local") or {}).get("coordinator")
        snapshot = getattr(local, "data", None)
        meter_at = getattr(local, "last_success_ts", None) or 0
        unit_w = charging_unit_w(charge, profile)
        # Missing/unknown current is not physical zero, particularly while a
        # restored current setting is waiting for the vehicle to ramp up.
        actual_current = charge.get("charger_actual_current")
        try:
            actual_amps = _finite(actual_current)
        except (ValueError, TypeError):
            return None
        if isinstance(actual_current, bool) or actual_amps < 0:
            return None
        if unit_w <= 0 and actual_amps > 0:
            return None  # Positive current with unknown watts is not zero draw.
        actual_ev_w = actual_amps*unit_w
        ev_at = _timestamp(charge.get("timestamp"))
        if self.budget is not None:
            self.budget.observe(vin, watts=actual_ev_w, at=ev_at, meter_at=meter_at)
        site_limit, _ = site_grid_limits_w(snapshot) if snapshot else (None, None)
        info = (getattr(snapshot, "raw", {}) or {}).get("config", {}).get("site_info", {}) if snapshot else {}
        known_limit = isinstance(info, dict) and info.get("max_site_meter_power_ac") is not None
        user_limit = config.get("optimization_max_grid_import_w")
        if user_limit not in (None, 0) and known_limit:
            site_limit = min(site_limit if site_limit is not None else math.inf, _finite(user_limit))
        if self.coexistence_reason() is not None:
            # Cleanup can reduce current but cannot increase it into unknown
            # pre-existing or unsupported managed demand.
            site_limit = None
        battery = self.runtime.get("powerwall_battery_dispatch")
        controlled = battery is not None and battery.charge_generation is not None
        actual_battery = max(0.0, -_finite(snapshot.battery_w)) if snapshot else math.nan
        acknowledged_battery = (getattr(battery, "prospective_charge_w", None)
            if battery is not None else 0.0)
        if acknowledged_battery is None:
            acknowledged_battery = max(0.0, -_finite(getattr(battery, "_last_watts", 0)))
        # Reserve measured draw and accepted/possibly submitted dispatch once.
        # An LP forecast is not a battery commitment, even when enabled. Only
        # an active grid-following generation may lend capacity to EV charging.
        planned_battery = max(actual_battery, acknowledged_battery)
        from .native_battery_flexibility import native_battery_flexibility
        flexibility = native_battery_flexibility(
            self.hass, self.entry, self.runtime, site_limit, budget=self.budget)
        other_pending = self.budget.unobserved_w(self.site, exclude_vin=vin) if self.budget else 0.0
        current = _finite(state.state)
        if not current.is_integer():
            return None
        result = RateObservation(vin, state.entity_id, identity, int(current),
            max(1, math.ceil(_finite(state.attributes.get("min")))),
            min(80, math.floor(_finite(state.attributes.get("max")))),
            unit_w,
            str(charge.get("charging_state", "")).lower() == "charging",
            _near_site(location, self.hass.config.latitude, self.hass.config.longitude),
            _fresh_at(state) if cleanup else min(_timestamp(charge.get("timestamp")), _timestamp(location.get("timestamp")), _fresh_at(state)),
            meter_at if getattr(local, "last_update_success", False) else 0,
            window.identity if window else None, window.start if window else None,
            window.end if window else None, window.read_at if window else None,
            max(0.0, _finite(charge.get("charge_limit_soc", 0))-_finite(charge.get("battery_level", 100)))*capacity*1000/100/0.9,
            _finite(snapshot.load_w)-actual_ev_w-_finite(snapshot.solar_w)+other_pending if snapshot else math.nan,
            planned_battery, site_limit, self.setting.rate_enabled(vin),
            planned_battery if controlled else 0.0, actual_ev_w, ev_at,
            str(charge.get("charging_state", "")).lower() in {"stopped", "complete", "disconnected"},
            actual_amps, flexibility.watts if flexibility else 0.0,
            flexibility.proof if flexibility else None)
        self.observations[vin] = result
        return result

    async def write(self, observation, amps, authorize):
        submitted = [False]
        try:
            result = await self._write(observation, amps, authorize, submitted)
        except Exception:
            return RateWriteResult(submitted[0], False, "unavailable")
        return RateWriteResult(submitted[0], result is True)

    async def _write(self, observation, amps, authorize, submitted):
        if self.budget is not None:
            vin = observation.vin
            task = asyncio.current_task()
            previous = self.budget.provider_tasks.get(vin)
            if previous is not None and not previous.done() and previous is not task:
                return False
            self.budget.provider_tasks[vin] = task
            try:
                return await _write_in_scope(self.hass, self.entry, vin, self, observation,
                                             amps, authorize, submitted)
            finally:
                if self.budget.provider_tasks.get(vin) is task:
                    self.budget.provider_tasks.pop(vin, None)
        return await _write_in_scope(self.hass, self.entry, observation.vin, self, observation,
                                     amps, authorize, submitted)

    async def _admitted_write(self, observation, amps, authorize, submitted, *, license_held=False):
        session = self.controller.sessions.get(observation.vin, {})
        restore = session.get("recovering") is True or self.controller.stopping
        revision = self.budget.revision if self.budget is not None else None
        battery = self.runtime.get("powerwall_battery_dispatch")
        generation = battery.charge_generation if battery is not None else None
        acknowledged_at = None
        increases = amps > observation.current_amps

        def current_authority():
            return (authorize() and (self.budget is None or self.budget.revision == revision)
                    and (generation is None or (self.runtime.get("powerwall_battery_dispatch") is battery
                         and battery.charge_generation == generation)))

        if increases and self.budget is not None:
            # The attempted current is already durable. Lower the exact active
            # optimiser charge generation before borrowing its physical power.
            if generation is not None:
                result = await battery.refresh_current_generation(generation)
                if result is None or not current_authority():
                    return False
                acknowledged_at = result["acknowledged_at"]
                previous_at, settled = None, 0
                for _ in range(20):
                    latest = await self.read(observation.vin)
                    if latest is None or not current_authority():
                        return False
                    if (latest.meter_at > acknowledged_at and latest.meter_at != previous_at
                            and restoration_fits(latest, amps, time.time())):
                        settled += 1
                        previous_at = latest.meter_at
                        if settled >= 2:
                            break
                    elif latest.meter_at != previous_at:
                        settled = 0
                        previous_at = latest.meter_at
                    await asyncio.sleep(0.5)
                else:
                    return False

        async def refresh():
            latest = await self.read(observation.vin)
            if latest is None or not current_authority():
                return None
            if increases:
                if (observation.native_battery_proof is not None
                        and latest.native_battery_proof != observation.native_battery_proof):
                    return None
                now = time.time()
                if (not 0 <= now-latest.measured_ev_at <= 15
                        or abs(latest.measured_ev_at-latest.meter_at) > 5
                        or (acknowledged_at is not None and latest.meter_at <= acknowledged_at)
                        or not restoration_fits(latest, amps, now)):
                    return None
            if restore:
                return latest if restoration_fits(latest, amps, time.time()) else None
            if session.get("window_id") is not None and latest.window_id != session["window_id"]:
                return None
            target, _ = decide_rate(latest, session["baseline_amps"], time.time())
            return latest if target == amps else None

        result = await _write_current(self.hass, self.entry, observation.vin, observation,
                                      amps, current_authority, restore=restore, refresh=refresh,
                                      license_held=license_held,
                                      on_submit=lambda: submitted.__setitem__(0, True))
        return result


async def async_prepare(hass, entry):
    """Load global claims and cleanup journals before any controller starts."""
    from .native_rate_budget import budget_for, EntryRateJournal
    from .tesla_schedule_planning import preferences
    from .teslemetry_solar import ownership
    # A durable SolarEV hold in another entry is still physical authority,
    # including before that entry has completed its own startup.
    for configured in hass.config_entries.async_entries("power_sync"):
        gate = await ownership(hass, configured.entry_id)
        async with gate.lock:
            await gate.load()
    runtime = hass.data["power_sync"][entry.entry_id]
    budget = await budget_for(hass)
    setting = await preferences(hass, entry.entry_id)
    async with setting.lock:
        await setting.load()
    ports = NativeRatePorts(hass, entry, setting, budget)
    controller = NativeRateController(EntryRateJournal(budget, entry.entry_id, ports.site), ports)
    ports.controller = controller
    runtime[RUNTIME_KEY] = controller
    await controller.load()  # Startup sessions are cleanup-only, never replay.
    ports.update_armed_sites()


async def async_start(hass, entry):
    """Activate only after optimiser setup and restart recovery are ready."""
    from homeassistant.helpers.event import async_track_time_interval
    runtime = hass.data["power_sync"][entry.entry_id]
    controller = runtime[RUNTIME_KEY]
    async def tick(_now=None):
        await controller.tick()
    runtime["tesla_native_rate_cancel"] = async_track_time_interval(hass, tick, timedelta(seconds=30))
    hass.async_create_task(tick())


async def async_stop(hass, entry):
    runtime = hass.data.get("power_sync", {}).get(entry.entry_id, {})
    if cancel := runtime.pop("tesla_native_rate_cancel", None):
        cancel()
    if controller := runtime.get(RUNTIME_KEY):
        await controller.stop()
        budget = controller.ports.budget
        pending = {task for vin, task in budget.provider_tasks.items()
                   if vin in controller.sessions and not task.done()}
        if pending:
            # A timed-out task remains in the shared fence and its durable
            # cleanup claim. Never cancel it and let a replacement overtake it.
            await asyncio.wait(pending, timeout=15)
        await budget.drain_storage()
