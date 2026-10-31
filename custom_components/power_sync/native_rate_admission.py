"""Final PowerSync Tesla service admission against shared prospective demand.

This fences the handoff to HA. Provider integrations may queue internally;
their outstanding demand remains reserved until fresh observed settlement.
"""
from __future__ import annotations

import asyncio
import math
import re
import time
from functools import wraps

from .native_rate_budget import RUNTIME_KEY, site_identity
from .teslemetry_solar import _EV_SUBMISSION_CONTEXT, ev_owner_snapshot


class SiteImportAdmissionDenied(RuntimeError):
    site_import_admission_denied = True


def native_coexistence_reason(hass, site, budget):
    """Existing sessions need tickets; unsupported chargers cannot share authority."""
    for entry_id, runtime in hass.data.get("power_sync", {}).items():
        if not isinstance(runtime, dict):
            continue
        entry = runtime.get("entry")
        if entry is not None and site_identity(entry) not in (site, None):
            continue
        if (runtime.get("native_rate_battery_transitions", 0)
                or runtime.get("powerwall_dispatch_fallback_pending")
                or any(state.get("active") and not state.get("powerwall_local_dispatch")
                    for state in (runtime.get("force_charge_state", {}), runtime.get("force_discharge_state", {})))):
            return "Waiting for local battery control and tariff restoration to settle"
        store = getattr(runtime.get("automation_store"), "_data", {}) or {}
        profiles = store.get("vehicle_charging_configs", [])
        if isinstance(profiles, list) and any(isinstance(profile, dict)
                and str(profile.get("charger_type") or "tesla").lower() != "tesla" for profile in profiles):
            return "Native Tesla rate management cannot yet share a site with a PowerSync charger controller"
        for key in ("ev_ownership", "ev_recovered_ownership"):
            for vin, lease in (runtime.get(key) or {}).items():
                if (not isinstance(lease, dict) or lease.get("owner") != "powersync"
                        or lease.get("owner_mode") == "native_schedule_rate"):
                    continue
                record = budget.managed.get(str(vin).upper())
                if record is None or record.get("entry_id") != entry_id or record.get("site") != site:
                    return "Waiting for existing managed EV charging demand to be reconciled"
    return None


def _max_backup_conflicting_control(runtime):
    """Existing force/recovery must finish before a new backup commitment."""
    if any((runtime.get(key) or {}).get("active") for key in (
            "force_charge_state", "force_discharge_state", "hold_soc_state", "self_consumption_state")):
        return True
    if any(getattr(runtime.get(key), "pending", False) for key in (
            "powerwall_native_max_charge", "powerwall_battery_dispatch")):
        return True
    return any(runtime.get(key) for key in (
        "pending_tesla_restore", "optimizer_force_restart_restore_pending", "native_max_recovery_unavailable",
        "powerwall_local_recovery_pending", "powerwall_dispatch_fallback_pending", "powerwall_settings_pending",
        "tesla_settings_pending", "powerwall_settings_recovering", "max_backup_restore_attempts",
        "max_backup_recovery_unavailable"))


def guard_battery_transition(hass, entry, handler):
    """Exclude new native authority throughout a battery service handoff."""
    bound_runtime = hass.data.get("power_sync", {}).get(entry.entry_id)
    @wraps(handler)
    async def guarded(call):
        runtime = hass.data.get("power_sync", {}).get(entry.entry_id)
        if bound_runtime is not None and runtime is not bound_runtime:
            raise SiteImportAdmissionDenied("Battery service belongs to a replaced setup generation")
        if runtime is None:
            return await handler(call)
        runtime["native_rate_battery_transitions"] = runtime.get("native_rate_battery_transitions", 0)+1
        requested_at = time.time()
        async def run():
            if hass.data.get("power_sync", {}).get(entry.entry_id) is not runtime:
                raise SiteImportAdmissionDenied("Battery setup changed while awaiting route ownership")
            previous = runtime.get("battery_route_requested_at")
            runtime["battery_route_requested_at"] = requested_at
            try:
                if runtime.get("native_max_startup_pending"):
                    raise SiteImportAdmissionDenied("Waiting for native Max startup restoration")
                name = handler.__name__
                direction = "charge" if "force_charge" in name else "discharge" if "force_discharge" in name else None
                episode = direction or name in ("handle_hold_battery_soc", "handle_set_self_consumption")
                max_backup = name == "handle_schedule_max_backup"
                if episode and (runtime.get("max_backup_end_ts") is not None or runtime.get("max_backup_recovery_unavailable")):
                    raise SiteImportAdmissionDenied("Max Backup window or cleanup still owns this site; wait for cleanup before forcing battery power")
                if max_backup and runtime.get("max_backup_restore_attempts"):
                    raise SiteImportAdmissionDenied("Existing Max Backup cleanup must resolve before another window; reloading the integration retries cleanup")
                if max_backup and _max_backup_conflicting_control(runtime):
                    raise SiteImportAdmissionDenied("Stop or restore existing battery control before scheduling Max Backup")
                site_control = runtime.get("powerwall_site_control")
                if site_control is None:
                    return await handler(call)
                from .powerwall_control import (
                    NativeMaxTarget, BatteryPowerTarget, GridExportTarget, SiteImportTarget,
                    foreign_site_owner, control_obligations,
                )
                if foreign_site_owner(hass, entry.entry_id, site_control.site_id) is not None:
                    raise SiteImportAdmissionDenied("Another PowerSync entry owns this Tesla site's control or recovery")
                data = getattr(call, "data", {}) or {}
                restoring = "restore_normal" in name
                target = None
                deadline = None
                backup_end = None
                if direction:
                    power = data.get("power_w", 0)
                    imported = data.get("grid_import_target_w")
                    if type(imported) in (int, float) and math.isfinite(imported) and imported > 0:
                        target = SiteImportTarget(imported)
                    elif type(power) in (int, float) and math.isfinite(power) and power > 0:
                        target = BatteryPowerTarget(-power) if direction == "charge" else GridExportTarget(power)
                    elif type(power) in (int, float) and power == 0:
                        target = NativeMaxTarget(direction)
                    # Optimizer durations are already validated by the service
                    # contract; waiting for ownership cannot extend the window.
                    duration = data.get("duration")
                    if data.get("source") == "optimizer" and type(duration) is int and 1 <= duration <= 1440:
                        deadline = time.monotonic() + max(0, requested_at + duration * 60 - time.time())
                elif max_backup:
                    duration = data.get("duration_minutes")
                    try:
                        duration = int(duration)
                    except (TypeError, ValueError):
                        duration = 0
                    if 1 <= duration <= 1440:
                        backup_end = requested_at + duration * 60
                        deadline = time.monotonic() + max(0, backup_end - time.time())
                def request_allowed():
                    admission = runtime.get("powerwall_control_admission")
                    return (foreign_site_owner(hass, entry.entry_id, site_control.site_id) is None
                            and (backup_end is None or time.time() < backup_end)
                            and not (episode and (runtime.get("max_backup_end_ts") is not None
                                                    or runtime.get("max_backup_recovery_unavailable")))
                            and not (max_backup and _max_backup_conflicting_control(runtime))
                            and (admission is None or admission(call, restoring=restoring) is True))
                site_control.set_obligations(entry.entry_id, control_obligations(runtime))
                try:
                    async with site_control.operation(
                            label=name, target=target, entry_id=entry.entry_id,
                            deadline=deadline, restoring=restoring,
                            is_current=lambda: hass.data.get("power_sync", {}).get(entry.entry_id) is runtime,
                            apply_guard=request_allowed,
                            identity=lambda: site_identity(entry)):
                        return await handler(call)
                finally:
                    site_control.set_obligations(entry.entry_id, control_obligations(runtime))
            finally:
                if previous is None:
                    runtime.pop("battery_route_requested_at", None)
                else:
                    runtime["battery_route_requested_at"] = previous
        async def take_route():
            if site_control := runtime.get("powerwall_site_control"):
                async with site_control.route_scope(entry.entry_id):
                    return await run()
            if route_lock := runtime.get("powerwall_battery_dispatch_route_lock"):
                async with route_lock:
                    return await run()
            return await run()
        try:
            # Optimizer calls already own the licensing control lock. Manual
            # handlers relinquish optimizer ownership while holding the route,
            # so they must drain optimizer writes before taking that route too.
            manager = hass.data.get("power_sync_license_manager")
            manual_scope = getattr(manager, "manual_command_scope", None)
            data = getattr(call, "data", {}) or {}
            if data.get("source") != "optimizer" and manual_scope is not None:
                async with manual_scope(entry.entry_id):
                    return await take_route()
            return await take_route()
        finally:
            runtime["native_rate_battery_transitions"] -= 1
    return guarded


def managed_borrowable_w(hass, entry, vin, profile):
    """Planning allowance only; final admission must lower the battery first."""
    budget = hass.data.get(RUNTIME_KEY)
    site = site_identity(entry)
    if budget is None or not budget.site_armed(site) or native_coexistence_reason(hass, site, budget):
        return 0.0
    runtime = hass.data.get("power_sync", {}).get(entry.entry_id)
    if not isinstance(runtime, dict):
        return 0.0
    try:
        evidence = _site_evidence(hass, entry, runtime, str(vin).upper(), profile)
        now = time.time()
        if (evidence["battery_generation"] is None or evidence["unit_w"] <= 0
                or not 0 <= now-evidence["vehicle_at"] <= 15
                or not 0 <= now-evidence["meter_at"] <= 15
                or abs(evidence["vehicle_at"]-evidence["meter_at"]) > 5):
            return 0.0
        pending = budget.unobserved_w(site, exclude_vin=str(vin).upper())
        value = evidence["borrowable_battery_w"]-pending-evidence["battery_pending_w"]
        return max(0.0, value) if math.isfinite(value) else 0.0
    except (AttributeError, TypeError, ValueError):
        return 0.0


def bind_vehicle(vin):
    context = _EV_SUBMISSION_CONTEXT.get()
    if context is not None:
        if str(context.get("vin") or "").upper() != str(vin).upper():
            context["owner_snapshot"] = ev_owner_snapshot(context["hass"], context["entry"], vin)
        context["vin"] = vin


def _fresh_at(state):
    stamp = getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
    return stamp.timestamp() if stamp else 0.0


def _number(value):
    if isinstance(value, bool):
        return math.nan
    try:
        return float(value)
    except (ValueError, TypeError):
        return math.nan


def _identity(registration):
    if not registration or not registration.device_id or not registration.unique_id:
        return None
    return f"{registration.platform}:{registration.config_entry_id}:{registration.device_id}:{registration.unique_id}"


def _current_setting(hass, registry, registration, entity_id, operation):
    if operation == "rate":
        return hass.states.get(entity_id)
    candidates = []
    if registration is not None:
        for candidate in registry.entities.values():
            if (candidate.device_id == registration.device_id
                    and candidate.config_entry_id == registration.config_entry_id
                    and candidate.platform == registration.platform
                    and not getattr(candidate, "disabled_by", None)
                    and re.search(r"^number\..*(charging_amps|charge_current)(?:_\d+)?$", candidate.entity_id)):
                state = hass.states.get(candidate.entity_id)
                value = _number(getattr(state, "state", None))
                if math.isfinite(value) and 0 <= value <= 80 and 0 <= time.time()-_fresh_at(state) <= 90:
                    candidates.append(state)
    # Duplicate provider entities must never make a start reserve less than
    # any of their current settings. Missing setting blocks an armed start.
    return max(candidates, key=lambda s: float(s.state)) if candidates else None


def _site_evidence(hass, entry, runtime, vin, profile):
    from .native_rate_telemetry import vehicle_report, charging_unit_w
    from .optimization.tesla_schedules import _section, _timestamp
    from .powerwall_local.battery_dispatch import site_grid_limits_w
    local = (runtime.get("powerwall_local") or {}).get("coordinator")
    snapshot = getattr(local, "data", None)
    at = getattr(local, "last_success_ts", None) or 0
    data = vehicle_report(hass, vin) or {}
    charge = _section(data, "charge_state")
    unit = charging_unit_w(charge, profile)
    actual_ev = _number(charge.get("charger_actual_current")) * unit
    cap = None
    if snapshot is not None:
        info = (getattr(snapshot, "raw", {}) or {}).get("config", {}).get("site_info", {})
        if isinstance(info, dict) and info.get("max_site_meter_power_ac") is not None:
            cap = site_grid_limits_w(snapshot)[0]
            configured = {**entry.data, **entry.options}.get("optimization_max_grid_import_w")
            if configured not in (None, 0):
                cap = min(cap if cap is not None else math.inf, _number(configured))
    controller = runtime.get("powerwall_battery_dispatch")
    battery_w = _number(getattr(snapshot, "battery_w", None))
    actual_battery = max(0.0, -battery_w) if math.isfinite(battery_w) else math.nan
    committed = getattr(controller, "prospective_charge_w", None)
    if committed is None:
        committed = max(0.0, -_number(getattr(controller, "_last_watts", 0)))
    optimizer = runtime.get("optimization_coordinator")
    action = optimizer._get_current_action() if optimizer is not None else None
    controlled = controller is not None and controller.charge_generation is not None
    if not controlled and action is not None and action.action == "charge":
        committed = max(committed, _number(action.power_w))
    return {"grid_w": _number(getattr(snapshot, "grid_w", None)), "cap_w": cap,
            "actual_ev_w": actual_ev, "battery_pending_w": max(0.0, committed-actual_battery),
            "borrowable_battery_w": actual_battery if controlled else 0.0,
            "battery_generation": controller.charge_generation if controlled else None,
            "vehicle_at": _timestamp(charge.get("timestamp")),
            "meter_at": at if getattr(local, "last_update_success", False) else 0,
            "unit_w": unit}


async def refresh_managed_records(hass, budget):
    """Observe each managed VIN independently; never infer it from a total."""
    if not budget.managed:
        return
    from homeassistant.helpers import entity_registry as er
    from .native_rate_telemetry import vehicle_report, charging_unit_w
    from .optimization.tesla_schedules import _section, _timestamp
    from .automations.ev_ownership import get_ev_ownership
    registry = er.async_get(hass)
    for vin, record in tuple(budget.managed.items()):
        runtime = hass.data.get("power_sync", {}).get(record.get("entry_id"), {})
        local = (runtime.get("powerwall_local") or {}).get("coordinator")
        if not getattr(local, "last_update_success", False):
            continue
        registration = registry.async_get(record.get("entity_id"))
        if _identity(registration) != record["identity"]:
            continue
        # Legacy starts may reserve demand without a usable current setting.
        # HA's state lookup requires an entity ID; absent readback must remain
        # unknown so reconciliation retains the uncertain reservation.
        setting_entity_id = record.get("setting_entity_id")
        state = hass.states.get(setting_entity_id) if setting_entity_id else None
        charge = _section(vehicle_report(hass, vin) or {}, "charge_state")
        current = _number(charge.get("charger_actual_current"))
        unit = charging_unit_w(charge, {})
        if not math.isfinite(current) or current < 0 or (current > 0 and unit <= 0):
            continue
        at = _timestamp(charge.get("timestamp"))
        meter_at = getattr(local, "last_success_ts", 0) or 0
        if 0 <= time.time()-at <= 15 and 0 <= time.time()-meter_at <= 15 and abs(at-meter_at) <= 5:
            budget.observe(vin, watts=current*unit, at=at, meter_at=meter_at)
        entry = runtime.get("entry")
        owner = get_ev_ownership(hass, entry, vin)[1] if entry is not None else None
        await budget.reconcile_managed(vin, {"identity": record["identity"],
            "amps": _number(getattr(state, "state", None)), "setting_at": _fresh_at(state),
            "vehicle_at": at, "meter_at": meter_at, "actual_w": current*unit,
            "charging": str(charge.get("charging_state", "")).lower() not in
                {"stopped", "complete", "disconnected", "nopower", "no_power"},
            "owner_active": owner is not None})


async def prepare_ev_submission(hass, entity_id, *, operation, amps=None):
    ticket = []
    try:
        context = _EV_SUBMISSION_CONTEXT.get()
        budget = hass.data.get(RUNTIME_KEY)
        if context is not None and budget is not None:
            async with budget.admission_lock(site_identity(context["entry"])):
                return await _prepare_ev_submission(hass, entity_id, operation=operation, amps=amps,
                                                    committed=ticket)
        return await _prepare_ev_submission(hass, entity_id, operation=operation, amps=amps,
                                            committed=ticket)
    except BaseException:
        if ticket:
            await hass.data[RUNTIME_KEY].abort_managed(ticket[0])
        raise


async def _prepare_ev_submission(hass, entity_id, *, operation, amps, committed):
    """Return False only for unavailable provider metadata, before submission.

    Policy denial raises through all provider fallback paths. A granted
    ticket is durable before the caller immediately invokes the HA service.
    """
    from homeassistant.helpers import entity_registry as er
    context = _EV_SUBMISSION_CONTEXT.get()
    budget = hass.data.get(RUNTIME_KEY)
    if budget is None:
        return True
    if context is None:
        await budget.load()
        if any(entry.get("native_max") for entry in budget.entries.values()):
            raise SiteImportAdmissionDenied("A known EV owner is required while native Max owns the site")
        return True
    await refresh_managed_records(hass, budget)
    entry = context["entry"]
    runtime = context["runtime"]
    site = site_identity(entry)
    vin = str(context.get("vin") or "").upper()
    if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin) or site is None or runtime is None:
        if budget.armed_sites or any(e.get("sessions") or e.get("native_max") for e in budget.entries.values()):
            raise SiteImportAdmissionDenied("A unique vehicle and site are required for shared charging")
        return True
    registry = er.async_get(hass)
    registration = registry.async_get(entity_id)
    identity = _identity(registration)
    state = _current_setting(hass, registry, registration, entity_id, operation)
    current = _number(getattr(state, "state", None))
    known_setting = math.isfinite(current) and current.is_integer() and 0 <= current <= 80 and 0 <= time.time()-_fresh_at(state) <= 90
    if (budget.site_armed(site) or budget.native_max_claim(site)) and (identity is None or not known_setting):
        return False
    if (budget.site_armed(site) or budget.native_max_claim(site)) and operation == "start" and current <= 0:
        return False
    requested = current if operation == "start" and known_setting else amps
    if requested is None and not budget.site_armed(site):
        requested = 80  # Unknown legacy start; retain its conservative envelope.
    if isinstance(requested, bool) or not isinstance(requested, (int, float)) or not float(requested).is_integer() or not 0 <= requested <= 80:
        return False
    profile = context.get("params") or {}
    evidence = _site_evidence(hass, entry, runtime, vin, profile)
    budget.observe(vin, watts=evidence["actual_ev_w"], at=evidence["vehicle_at"],
                   meter_at=evidence["meter_at"])
    unit = evidence["unit_w"]
    if not unit:
        if budget.site_armed(site):
            return False
        unit = 900  # No authority is gained from this conservative legacy bound.
    evidence["rate_reduction"] = operation == "rate" and known_setting and requested <= current
    evidence["current_amps"] = int(current) if known_setting else None
    record = {"entry_id": entry.entry_id, "site": site, "identity": identity or entity_id,
              "entity_id": entity_id, "setting_entity_id": getattr(state, "entity_id", None),
              "target_amps": int(requested), "target_w": requested*unit,
              "reserved_w": max(requested, current if known_setting else requested)*unit,
              "watts_per_amp": unit, "submitted_at": time.time(), "operation": operation,
              "uncertain": True}

    pairing_keys = ("powerwall_local_energy_site_id", "tesla_energy_site_id",
                    "powerwall_local_din", "powerwall_local_public_key_der")
    def pairing():
        config = {**entry.data, **entry.options}
        return tuple(config.get(key) for key in pairing_keys)
    original_pairing = pairing()
    battery = runtime.get("powerwall_battery_dispatch")
    generation = evidence.get("battery_generation")
    owners = context.get("owner_snapshot")
    if owners is None:
        owners = ev_owner_snapshot(hass, entry, vin)
    mode = str(profile.get("owner_mode") or next((owner[3] for owner in owners
        if owner[0] == entry.entry_id), "") or "").lower()
    automated = bool(mode and not mode.startswith(("manual", "boost")))
    def policy_current():
        if not automated:
            return True
        config = {**entry.data, **entry.options}
        optimizer = runtime.get("optimization_coordinator")
        manager = hass.data.get("power_sync_license_manager")
        return (not config.get("monitoring_mode", False)
                and not getattr(optimizer, "_monitoring_mode_active", lambda: False)()
                and (manager is None or manager.permits(entry.entry_id)))

    def valid():
        return (hass.data.get("power_sync", {}).get(entry.entry_id) is runtime
                and ev_owner_snapshot(hass, entry, vin) == owners and policy_current()
                and not runtime.get("powerwall_local_dispatch_stopping")
                and pairing() == original_pairing
                and (generation is None or (runtime.get("powerwall_battery_dispatch") is battery
                     and battery.charge_generation == generation))
                and _identity(registry.async_get(entity_id)) == identity
                and (not known_setting or (live := hass.states.get(state.entity_id)) is not None
                     and _number(live.state) == current and 0 <= time.time()-_fresh_at(live) <= 90))

    ticket = await budget.admit_managed(vin, record, evidence, valid)
    if ticket is not None:
        committed.append(ticket)
    if ticket is None or not valid():
        raise SiteImportAdmissionDenied("Waiting for shared site charging headroom")

    # This provider task may be suspended while the battery yields capacity.
    # Fence reconciliation for its entire lifetime, including cancellation.
    # Both managed and native writes take SolarEV before the site lock.
    # Durable reservations plus revision checks also fence journal changes.
    budget.managed_tasks[vin] = asyncio.current_task()
    revision = ticket["revision"]
    def fits(latest):
        now = time.time()
        budget.observe(vin, watts=latest["actual_ev_w"], at=latest["vehicle_at"],
                       meter_at=latest["meter_at"])
        values = [latest.get(k) for k in ("grid_w", "cap_w", "actual_ev_w", "battery_pending_w", "vehicle_at", "meter_at")]
        return (valid() and budget.revision == revision
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values)
                and 0 <= now-latest["vehicle_at"] <= 15 and 0 <= now-latest["meter_at"] <= 15
                and abs(latest["vehicle_at"]-latest["meter_at"]) <= 5
                and latest["grid_w"]+budget.unobserved_w(site, exclude_vin=vin)
                    + max(0, ticket["record"]["reserved_w"]-latest["actual_ev_w"])
                    + latest["battery_pending_w"] <= latest["cap_w"])

    if ticket["battery_generation"] is not None:
        # The reservation is already durable and included in the dispatch
        # controller's config. Reapplying this exact generation lowers its
        # pre-start target without changing the optimiser's owner/deadline.
        try:
            result = await battery.refresh_current_generation(generation)
            if result is None or not valid() or budget.revision != revision:
                raise SiteImportAdmissionDenied("Battery charging generation changed")
            acknowledged_at = result["acknowledged_at"]
            previous_at, settled = None, 0
            for _ in range(20):
                latest = _site_evidence(hass, entry, runtime, vin, profile)
                at = latest.get("meter_at", 0)
                if not valid() or budget.revision != revision:
                    raise SiteImportAdmissionDenied("Site charging reservation changed")
                if at != previous_at:
                    settled = settled+1 if at > acknowledged_at and fits(latest) else 0
                    previous_at = at
                if settled >= 2:
                    break
                await asyncio.sleep(0.5)
            else:
                raise SiteImportAdmissionDenied("Waiting for measured site charging headroom")
        except asyncio.CancelledError:
            raise
        except SiteImportAdmissionDenied:
            raise
        except Exception as err:
            raise SiteImportAdmissionDenied("Battery charging headroom could not be verified") from err
    if budget.site_armed(site) and not evidence.get("rate_reduction"):
        latest = _site_evidence(hass, entry, runtime, vin, profile)
        if not fits(latest):
            raise SiteImportAdmissionDenied("Site charging headroom changed before submission")
    maximum = budget.native_max_claim(site)
    max_token = ticket["record"].get("native_max_token")
    if (not valid() or budget.revision != revision
            or (maximum.get("token") if maximum else None) != max_token):
        raise SiteImportAdmissionDenied("Site charging ownership changed before submission")
    # The provider call follows synchronously in the caller. Keep its task
    # fenced through cancellation; an acknowledgement is not physical draw.
    return True
