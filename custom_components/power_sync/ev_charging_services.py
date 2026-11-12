"""HA automation actions for the existing EV charging mode settings."""

from __future__ import annotations

import asyncio

import voluptuous as vol

from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .solar_surplus_control import async_update_solar_surplus_config

ACTION_MODES = ("solar_surplus", "scheduled", "price_level", "smart_schedule")

MODE_DEFAULTS = {
    "scheduled_charging": {
        "enabled": False, "start_time": "00:00", "end_time": "06:00",
        "max_price_cents": 30, "preserve_home_battery": False, "no_grid_import": False,
    },
    "price_level_charging": {
        "enabled": False, "recovery_soc": 40, "recovery_price_cents": 30,
        "opportunity_price_cents": 10, "preserve_home_battery": False,
        "no_grid_import": False, "home_battery_minimum": 20,
    },
}


async def async_update_charging_config(hass, entry, store, mode, updates):
    """Share serialized, durable app/action writes without resetting other options."""
    if store is None or not isinstance(getattr(store, "_data", None), dict):
        raise HomeAssistantError("EV charging storage is not available")
    entry_data = hass.data[DOMAIN][entry.entry_id]
    lock = entry_data.setdefault(f"{mode}_config_lock", asyncio.Lock())
    async with lock:
        previous = store._data.get(mode)
        settings = {**MODE_DEFAULTS[mode], **(previous or {})}
        settings.update({k: v for k, v in updates.items() if k in MODE_DEFAULTS[mode]})
        if settings.get("preserve_home_battery"):
            settings["no_grid_import"] = False
        elif settings.get("no_grid_import"):
            settings["preserve_home_battery"] = False
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
    lock = entry_data.setdefault("smart_schedule_action_lock", asyncio.Lock())
    async with lock:
        settings = executor._settings[vehicle_id]
        previous = settings.enabled
        executor.update_settings(vehicle_id, {"enabled": enabled})
        try:
            await executor.save_settings(store, raise_errors=True)
        except Exception:
            settings.enabled = previous
            raise
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


def unregister_services(hass):
    """Remove actions after the last PowerSync entry has unloaded."""
    for mode in ACTION_MODES:
        hass.services.async_remove(DOMAIN, f"set_{mode}_charging")
