"""Vehicle profiles for the single configured generic charger.

Profiles own SOC and Smart Schedule preferences. Metering, commands, leases,
recovery records and sessions retain the physical ``generic_ev`` identity.
"""
from __future__ import annotations

import copy
import math
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

KEY = "shared_generic_charger"
RUNTIME_ID = "generic_ev"


def entry_runtime(hass: Any, entry: Any) -> dict:
    return getattr(hass, "data", {}).get("power_sync", {}).get(getattr(entry, "entry_id", ""), {})


def profile_store(runtime: Mapping) -> Any:
    return runtime.get("automation_store") or runtime.get("_shared_generic_charger_store")


def bind_profile_store(hass: Any, entry: Any, store: Any) -> None:
    """Make persisted profiles visible before provider startup and planner load."""
    if KEY in (getattr(store, "_data", {}) or {}):
        entry_runtime(hass, entry)["_shared_generic_charger_store"] = store


def shared_data(hass: Any, entry: Any) -> dict | None:
    store = profile_store(entry_runtime(hass, entry))
    data = (getattr(store, "_data", {}) or {}).get(KEY)
    return data if isinstance(data, dict) else None


def selected_profile(data: Mapping | None) -> dict | None:
    if not data:
        return None
    selected = data.get("selected_vehicle_id")
    matches = [p for p in data.get("profiles", []) if isinstance(p, dict) and p.get("id") == selected]
    return matches[0] if selected and len(matches) == 1 else None


def context_for_options(hass: Any, opts: Mapping) -> dict | None:
    """Match the configured physical switch; never borrow another entry's car."""
    switch = opts.get("generic_charger_switch_entity")
    entries = getattr(getattr(hass, "config_entries", None), "async_entries", None)
    if entries is not None:
        for entry in entries("power_sync"):
            current = {**entry.data, **entry.options}
            if current.get("generic_charger_switch_entity") == switch:
                data = shared_data(hass, entry)
                if data is not None:
                    if data.get("charger_switch_entity") != switch:
                        return {"profiles": [], "selected_vehicle_id": None}
                    return data
    for runtime in getattr(hass, "data", {}).get("power_sync", {}).values():
        if not isinstance(runtime, dict):
            continue
        store = profile_store(runtime)
        data = (getattr(store, "_data", {}) or {}).get(KEY)
        if isinstance(data, dict) and data.get("charger_switch_entity") == switch:
            return data
    return None


def profile_soc(hass: Any, data: Mapping | None) -> float | None:
    profile = selected_profile(data)
    state = hass.states.get(profile.get("soc_entity")) if profile else None
    try:
        value = float(state.state)
    except (AttributeError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) and 0 <= value <= 100 else None


def apply_profile(executor: Any, vehicle_id: str, settings: Any) -> None:
    if vehicle_id != RUNTIME_ID:
        return
    data = shared_data(getattr(executor, "hass", None), getattr(executor, "config_entry", None))
    if data is None:
        return
    profile = selected_profile(data)
    settings.shared_charger_profile_id = profile.get("id") if profile else None
    if profile:
        settings.display_name = profile["name"]
        settings.battery_capacity_kwh = profile["battery_capacity_kwh"]
        settings.charger_fallback_battery_capacity_kwh = profile["battery_capacity_kwh"]


def restore_schedule(executor: Any) -> None:
    """Restore exactly one controller; plans and generic SOC caches are not portable."""
    from .ev_charging_planner import AutoScheduleSettings, AutoScheduleState

    data = shared_data(getattr(executor, "hass", None), getattr(executor, "config_entry", None))
    if data is None:
        return
    profile = selected_profile(data)
    values = copy.deepcopy(profile.get("schedule", {})) if profile else {"enabled": False}
    values.update(vehicle_id=RUNTIME_ID, charger_type="generic")
    settings = AutoScheduleSettings.from_dict(values)
    executor._settings[RUNTIME_ID] = settings
    executor._state[RUNTIME_ID] = AutoScheduleState(vehicle_id=RUNTIME_ID)
    getattr(executor, "_cached_soc", {}).pop(RUNTIME_ID, None)
    getattr(executor, "_current_charge_amps", {}).pop(RUNTIME_ID, None)
    getattr(executor, "_charger_capability_signatures", {}).pop(RUNTIME_ID, None)
    apply_profile(executor, RUNTIME_ID, settings)


async def save_selected_schedule(executor: Any) -> None:
    """Persist each car's latest schedule before the next selection or restart."""
    data = shared_data(getattr(executor, "hass", None), getattr(executor, "config_entry", None))
    profile = selected_profile(data)
    settings = executor._settings.get(RUNTIME_ID)
    if profile is None or settings is None:
        return
    # A superseded settings object must not overwrite the newly selected car.
    if getattr(settings, "shared_charger_profile_id", None) != profile["id"]:
        raise ValueError("shared charger selection changed")
    runtime = entry_runtime(executor.hass, executor.config_entry)
    if runtime.get("_shared_generic_switching"):
        raise ValueError("Shared charger selection is changing.")
    runtime["_shared_generic_schedule_saving"] = runtime.get("_shared_generic_schedule_saving", 0) + 1
    previous = profile.get("schedule")
    profile["schedule"] = settings.to_dict()
    try:
        await profile_store(entry_runtime(executor.hass, executor.config_entry)).async_save()
    except BaseException:
        profile["schedule"] = previous
        raise
    finally:
        runtime["_shared_generic_schedule_saving"] -= 1


def selection_block_reason(hass: Any, entry: Any, executor: Any = None) -> str | None:
    """Require fresh disconnected evidence and no outstanding controller liability."""
    from .ev_ownership import get_ev_ownership

    runtime = entry_runtime(hass, entry)
    if runtime.get("_shared_generic_commands", 0) or runtime.get("_shared_generic_schedule_saving", 0):
        return "A charger command is still in progress."
    from .actions import _dynamic_ev_state
    if _dynamic_ev_state.get(entry.entry_id, {}).get(RUNTIME_ID):
        return "Charging or charger recovery is still active."
    _, lease = get_ev_ownership(hass, entry, RUNTIME_ID)
    if lease:
        return "Stop and release the current charging session before changing vehicles."
    for key in ("dynamic_ev_state", "_pending_generic_ev_starts", "ev_boost_sessions", "ev_boost_timers"):
        if runtime.get(key, {}).get(RUNTIME_ID):
            return "Charging or charger recovery is still active."
    if executor:
        if getattr(executor, "_start_in_flight", set()):
            return "A charging start is still in progress."
        state = getattr(executor, "_state", {}).get(RUNTIME_ID)
        if state and (state.is_charging or state.curtailment_override_active):
            return "Stop Smart Schedule charging before changing vehicles."
    opts = {**getattr(entry, "data", {}), **getattr(entry, "options", {})}
    state = hass.states.get(opts.get("generic_charger_status_entity"))
    if state is None or str(state.state).lower() not in {"available", "disconnected", "unplugged", "not_plugged_in"}:
        return "Unplug the car and wait for the charger to report disconnected."
    received = getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
    try:
        age = (datetime.now(timezone.utc) - received).total_seconds()
    except (TypeError, AttributeError):
        age = -1
    if not 0 <= age <= 120:
        return "Wait for a fresh disconnected charger reading."
    power = hass.states.get(opts.get("generic_charger_power_entity"))
    if power is not None:
        try:
            watts = float(power.state) * (1000 if str(power.attributes.get("unit_of_measurement", "")).lower() == "kw" else 1)
        except (TypeError, ValueError):
            return "The charger power reading is unavailable."
        if not math.isfinite(watts) or abs(watts) > 50:
            return "The charger meter must report no charging before changing vehicles."
    return None


def start_allowed(hass: Any, entry: Any, params: dict) -> bool:
    """Fence stale queued starts without affecting stop/recovery commands."""
    if params.get("charger_type") != "generic":
        return True
    data = shared_data(hass, entry)
    if data is None:
        return True
    profile = selected_profile(data)
    opts = {**getattr(entry, "data", {}), **getattr(entry, "options", {})}
    if data.get("charger_switch_entity") != opts.get("generic_charger_switch_entity"):
        return False
    if profile is None or entry_runtime(hass, entry).get("_shared_generic_switching"):
        return False
    return params.get("_shared_generic_profile_id", profile["id"]) == profile["id"]


def snapshot(hass: Any, entry: Any, executor: Any = None) -> dict:
    opts = {**getattr(entry, "data", {}), **getattr(entry, "options", {})}
    data = shared_data(hass, entry)
    profile = selected_profile(data)
    reason = selection_block_reason(hass, entry, executor) if opts.get("generic_charger_enabled") else "Enable the generic charger in PowerSync options first."
    return {
        "available": bool(opts.get("generic_charger_enabled")),
        "enabled": data is not None,
        "selected_vehicle_id": profile["id"] if profile else None,
        "profiles": [{"id": p["id"], "name": p["name"], "soc_entity": p["soc_entity"], "battery_capacity_kwh": p["battery_capacity_kwh"]} for p in (data or {}).get("profiles", [])],
        "selection_allowed": reason is None,
        "selection_block_reason": reason,
    }


async def update(hass: Any, entry: Any, executor: Any, payload: dict) -> dict:
    """Manage car metadata/selection atomically with the existing start lock."""
    from .actions import _start_dynamic_lock

    runtime = entry_runtime(hass, entry)
    store = profile_store(runtime)
    if (store is None or executor is None or getattr(executor, "hass", None) is not hass
            or getattr(getattr(executor, "config_entry", None), "entry_id", None) != entry.entry_id):
        raise ValueError("Charging settings are not ready.")
    opts = {**entry.data, **entry.options}
    if not opts.get("generic_charger_enabled"):
        raise ValueError("Enable the generic charger first.")
    async with _start_dynamic_lock:
        reason = selection_block_reason(hass, entry, executor)
        if reason:
            raise ValueError(reason)
        await save_selected_schedule(executor)
        original = copy.deepcopy(store._data.get(KEY))
        data = copy.deepcopy(store._data.get(KEY) or {"profiles": [], "selected_vehicle_id": None})
        data["charger_switch_entity"] = opts.get("generic_charger_switch_entity")
        data["charger_amps_entity"] = opts.get("generic_charger_amps_entity")
        operation = payload.get("operation")
        if operation == "save":
            name = str(payload.get("name") or "").strip()
            sensor = str(payload.get("soc_entity") or "").strip()
            try:
                capacity = float(payload.get("battery_capacity_kwh"))
            except (TypeError, ValueError) as err:
                raise ValueError("Enter a battery capacity between 1 and 250 kWh.") from err
            import re
            if not name or len(name) > 80 or not re.fullmatch(r"(?:sensor|input_number)\.[a-z0-9_]+", sensor):
                raise ValueError("Enter a vehicle name and its SOC sensor entity ID.")
            if not math.isfinite(capacity) or not 1 <= capacity <= 250:
                raise ValueError("Enter a battery capacity between 1 and 250 kWh.")
            vehicle_id = payload.get("vehicle_id")
            profile = next((p for p in data["profiles"] if p["id"] == vehicle_id), None)
            if vehicle_id and profile is None:
                raise ValueError("Vehicle profile not found.")
            if profile is None:
                first = not data["profiles"] and original is None
                settings = executor.get_settings(RUNTIME_ID).to_dict()
                if not first:
                    settings.update(enabled=False, target_soc=80, departure_times={}, departure_days=[], departure_time=None)
                    for key in tuple(settings):
                        if key.startswith("departure_") and isinstance(settings[key], dict):
                            settings[key] = {}
                profile = {"id": uuid.uuid4().hex, "schedule": settings}
                data["profiles"].append(profile)
                if first:
                    data["selected_vehicle_id"] = profile["id"]
            profile.update(name=name, soc_entity=sensor, battery_capacity_kwh=capacity)
        elif operation == "select":
            chosen = payload.get("vehicle_id") or None
            if chosen is not None and sum(p["id"] == chosen for p in data["profiles"]) != 1:
                raise ValueError("Vehicle profile not found.")
            data["selected_vehicle_id"] = chosen
        elif operation == "delete":
            chosen = payload.get("vehicle_id")
            if not chosen or not any(p["id"] == chosen for p in data["profiles"]):
                raise ValueError("Vehicle profile not found.")
            data["profiles"] = [p for p in data["profiles"] if p["id"] != chosen]
            if data.get("selected_vehicle_id") == chosen:
                data["selected_vehicle_id"] = None
        else:
            raise ValueError("Unknown shared charger operation.")
        runtime["_shared_generic_switching"] = True
        store._data[KEY] = data
        try:
            await store.async_save()
            restore_schedule(executor)
        except BaseException:
            if original is None:
                store._data.pop(KEY, None)
            else:
                store._data[KEY] = original
            raise
        finally:
            runtime.pop("_shared_generic_switching", None)
    return snapshot(hass, entry, executor)


def generic_entity_command(function):
    """Keep selection fixed across awaited switch/current service calls."""
    from functools import wraps

    @wraps(function)
    async def guarded(hass, entity_id, *args, **kwargs):
        affected = []
        for runtime in getattr(hass, "data", {}).get("power_sync", {}).values():
            if not isinstance(runtime, dict):
                continue
            store = profile_store(runtime)
            data = (getattr(store, "_data", {}) or {}).get(KEY)
            if not isinstance(data, dict) or entity_id not in (data.get("charger_switch_entity"), data.get("charger_amps_entity")):
                continue
            is_stop = function.__name__ == "_stop_generic_charger_switch" or (
                function.__name__ == "_set_generic_charger_amps_entity" and args and args[0] <= 0
            )
            if not is_stop and (selected_profile(data) is None or runtime.get("_shared_generic_switching")):
                return False
            runtime["_shared_generic_commands"] = runtime.get("_shared_generic_commands", 0) + 1
            affected.append(runtime)
        try:
            return await function(hass, entity_id, *args, **kwargs)
        finally:
            for runtime in affected:
                runtime["_shared_generic_commands"] -= 1
    return guarded


def stop_allowed(hass: Any, entry: Any, params: dict) -> bool:
    """Preserve explicit physical Stops; reject a superseded car's queued work."""
    if params.get("charger_type") != "generic" or "_shared_generic_profile_id" not in params:
        return True
    data = shared_data(hass, entry)
    if data is None:
        return True
    profile = selected_profile(data)
    return profile is not None and params["_shared_generic_profile_id"] == profile["id"]
