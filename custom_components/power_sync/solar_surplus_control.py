"""Shared Solar Surplus settings writes for HA entities and the app API."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import DOMAIN
from .solar_surplus_config import normalize_solar_surplus_config

_LOGGER = logging.getLogger(__name__)
SIGNAL_SOLAR_SURPLUS_CONFIG_UPDATED = f"{DOMAIN}_solar_surplus_config_updated"


async def async_update_solar_surplus_config(
    hass: Any, entry: Any, store: Any, updates: dict[str, Any]
) -> dict[str, Any]:
    """Persist settings before stopping Solar Surplus sessions on disable.

    Serialize API and entity writes so toggling cannot discard other settings
    or race an earlier disable's charger cleanup.
    """
    if store is None or not isinstance(getattr(store, "_data", None), dict):
        raise RuntimeError("Solar Surplus storage is not available")
    entry_data = hass.data[DOMAIN][entry.entry_id]
    lock = entry_data.setdefault("solar_surplus_config_lock", asyncio.Lock())
    async with lock:
        return await _async_update_locked(hass, entry, store, updates)


async def _async_update_locked(
    hass: Any, entry: Any, store: Any, updates: dict[str, Any]
) -> dict[str, Any]:
    """Apply the existing API validation and disable behavior under the lock."""
    updates = dict(updates)
    if "min_battery_soc" in updates and "home_battery_minimum" not in updates:
        updates["home_battery_minimum"] = updates["min_battery_soc"]
    previous = store._data.get("solar_surplus_config")
    current_config = previous or {}
    current_enabled = normalize_solar_surplus_config(current_config).get(
        "enabled", False
    )
    updated_config = {**current_config, **updates}
    # Validate config values
    if "household_buffer_kw" in updated_config:
        updated_config["household_buffer_kw"] = max(
            0, min(5, float(updated_config["household_buffer_kw"]))
        )
    if "sustained_surplus_minutes" in updated_config:
        updated_config["sustained_surplus_minutes"] = max(
            1, min(30, int(updated_config["sustained_surplus_minutes"]))
        )
    if "stop_delay_minutes" in updated_config:
        updated_config["stop_delay_minutes"] = max(
            1, min(30, int(updated_config["stop_delay_minutes"]))
        )
    if "surplus_calculation" in updated_config:
        if updated_config["surplus_calculation"] not in ("grid_based", "direct"):
            updated_config["surplus_calculation"] = "grid_based"
    if "dual_vehicle_strategy" in updated_config:
        if updated_config["dual_vehicle_strategy"] not in (
            "even", "priority_first", "priority_only"
        ):
            updated_config["dual_vehicle_strategy"] = "priority_first"
    if "home_battery_minimum" in updated_config:
        updated_config["home_battery_minimum"] = max(
            0, min(100, int(updated_config["home_battery_minimum"]))
        )
    if "allow_parallel_charging" in updated_config:
        updated_config["allow_parallel_charging"] = bool(updated_config["allow_parallel_charging"])
    if "max_battery_charge_rate_kw" in updated_config:
        updated_config["max_battery_charge_rate_kw"] = max(
            1, min(30, float(updated_config["max_battery_charge_rate_kw"]))
        )
    updated_config = normalize_solar_surplus_config(updated_config)

    store._data["solar_surplus_config"] = updated_config
    try:
        await store.async_save()
    except Exception:
        if previous is None:
            store._data.pop("solar_surplus_config", None)
        else:
            store._data["solar_surplus_config"] = previous
        raise

    # Publish the persisted setting even if the hardware stop subsequently fails.
    async_dispatcher_send(hass, SIGNAL_SOLAR_SURPLUS_CONFIG_UPDATED, entry.entry_id)
    if current_enabled and not updated_config.get("enabled", False):
        from .automations.actions import stop_solar_surplus_ev_charging

        stopped = await stop_solar_surplus_ev_charging(hass, entry)
        if not stopped:
            _LOGGER.warning(
                "Solar Surplus was disabled and ownership released, "
                "but one or more charger stop commands failed"
            )
    return updated_config
