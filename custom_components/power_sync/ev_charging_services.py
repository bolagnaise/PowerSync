"""HA automation actions for the existing EV charging mode settings."""

from __future__ import annotations

import asyncio

import voluptuous as vol

from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .ev_mode_settings import charging_mode_settings
from .solar_surplus_control import async_update_solar_surplus_config

ACTION_MODES = ("solar_surplus", "scheduled", "price_level", "smart_schedule")
EV_RETURN_SERVICE = "set_ev_return"
# "Not home tonight" without a time lasts until this local hour next morning.
NOT_HOME_DEFAULT_HOUR = 10


def _default_not_home_until(now):
    from datetime import timedelta
    until = now.replace(hour=NOT_HOME_DEFAULT_HOUR, minute=0, second=0, microsecond=0)
    return until if until > now + timedelta(hours=1) else until + timedelta(days=1)


async def async_set_ev_return(hass, entry, vehicle_id, mode, when=None):
    """Record the owner's answer about when one away car is coming home."""
    from homeassistant.util import dt as dt_util
    from .optimization.ev_return import MODE_NOT_HOME
    from .optimization.ev_return_inputs import (
        async_save_overrides, build_override, load_overrides,
    )

    entry_data = hass.data[DOMAIN][entry.entry_id]
    store = entry_data.get("automation_store")
    if store is None or not isinstance(getattr(store, "_data", None), dict):
        raise HomeAssistantError("EV charging storage is not available")
    vehicle_id = str(vehicle_id or "").strip()
    if not vehicle_id:
        raise HomeAssistantError("Select a vehicle")
    now = dt_util.now()
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
    if mode == MODE_NOT_HOME and when is None:
        when = _default_not_home_until(now)
    try:
        override = build_override(mode, when, now)
    except ValueError as err:
        raise HomeAssistantError(str(err)) from err
    lock = entry_data.setdefault("ev_return_lock", asyncio.Lock())
    async with lock:
        overrides = load_overrides(store)
        if override is None:
            overrides.pop(vehicle_id, None)
        else:
            overrides[vehicle_id] = override
        await async_save_overrides(store, overrides)
    coordinator = entry_data.get("optimization_coordinator")
    refresh = getattr(coordinator, "_schedule_settings_reoptimization", None)
    if refresh:
        refresh()
    return {"vehicle_id": vehicle_id, "mode": mode,
            "until": override.until.isoformat() if override else None}


async def async_update_charging_config(hass, entry, store, mode, updates):
    """Share serialized, durable app/action writes without resetting other options."""
    if store is None or not isinstance(getattr(store, "_data", None), dict):
        raise HomeAssistantError("EV charging storage is not available")
    entry_data = hass.data[DOMAIN][entry.entry_id]
    lock = entry_data.setdefault(f"{mode}_config_lock", asyncio.Lock())
    async with lock:
        previous = store._data.get(mode)
        settings = charging_mode_settings(mode, previous, updates)
        store._data[mode] = settings
        try:
            await store.async_save()
        except Exception:
            if previous is None:
                store._data.pop(mode, None)
            else:
                store._data[mode] = previous
            raise
        coordinator = entry_data.get("optimization_coordinator")
        refresh = getattr(coordinator, "_schedule_settings_reoptimization", None)
        if refresh:
            refresh()
        return settings


async def async_set_charging_mode(hass, entry, mode, enabled, vehicle_id=None):
    """Persist an explicit desired state; normal EV evaluation retains ownership."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    if mode == "solar_surplus":
        return await async_update_solar_surplus_config(
            hass, entry, entry_data.get("automation_store"), {"enabled": enabled}
        )
    if mode != "smart_schedule":
        return await async_update_charging_config(
            hass, entry, entry_data.get("automation_store"), f"{mode}_charging",
            {"enabled": enabled},
        )
    executor = entry_data.get("auto_schedule_executor")
    store = entry_data.get("store")
    if executor is None or store is None:
        raise HomeAssistantError("Smart Schedule is not available")
    # Do not create a new/default vehicle or select a different shared loadpoint.
    if not vehicle_id or vehicle_id not in executor._settings:
        raise HomeAssistantError("Select an existing Smart Schedule vehicle ID")
    settings = await executor.async_save_settings_update(store, vehicle_id, {"enabled": enabled})
    return {"vehicle_id": vehicle_id, "enabled": settings.enabled}


def register_services(hass):
    """Register once, resolving the selected loaded entry on every action call."""
    for mode in ACTION_MODES:
        name = f"set_{mode}_charging"
        if hass.services.has_service(DOMAIN, name):
            continue

        async def handle(call, selected_mode=mode):
            entry_id = call.data["entry_id"]
            entry = hass.config_entries.async_get_entry(entry_id)
            if (entry is None or entry.domain != DOMAIN
                    or entry_id not in hass.data.get(DOMAIN, {})):
                raise HomeAssistantError("Select a loaded PowerSync integration")
            try:
                return await async_set_charging_mode(
                    hass, entry, selected_mode, call.data["enabled"],
                    call.data.get("vehicle_id"),
                )
            except HomeAssistantError:
                raise
            except Exception as err:
                raise HomeAssistantError(f"EV charging setting could not be saved: {err}") from err

        fields = {vol.Required("entry_id"): cv.string, vol.Required("enabled"): cv.boolean}
        if mode == "smart_schedule":
            fields[vol.Required("vehicle_id")] = cv.string
        hass.services.async_register(
            DOMAIN, name, handle, schema=vol.Schema(fields),
            supports_response=SupportsResponse.OPTIONAL,
        )


    if not hass.services.has_service(DOMAIN, EV_RETURN_SERVICE):
        async def handle_return(call):
            entry_id = call.data["entry_id"]
            entry = hass.config_entries.async_get_entry(entry_id)
            if (entry is None or entry.domain != DOMAIN
                    or entry_id not in hass.data.get(DOMAIN, {})):
                raise HomeAssistantError("Select a loaded PowerSync integration")
            try:
                return await async_set_ev_return(
                    hass, entry, call.data["vehicle_id"], call.data["mode"],
                    call.data.get("time"),
                )
            except HomeAssistantError:
                raise
            except Exception as err:
                raise HomeAssistantError(f"EV return could not be saved: {err}") from err

        hass.services.async_register(
            DOMAIN, EV_RETURN_SERVICE, handle_return,
            schema=vol.Schema({
                vol.Required("entry_id"): cv.string,
                vol.Required("vehicle_id"): cv.string,
                vol.Required("mode"): vol.In(("auto", "not_home", "home_at")),
                vol.Optional("time"): cv.datetime,
            }),
            supports_response=SupportsResponse.OPTIONAL,
        )


def unregister_services(hass):
    """Remove actions after the last PowerSync entry has unloaded."""
    for mode in ACTION_MODES:
        hass.services.async_remove(DOMAIN, f"set_{mode}_charging")
    hass.services.async_remove(DOMAIN, EV_RETURN_SERVICE)
