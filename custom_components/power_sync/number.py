"""Number platform for PowerSync integration — Tesla Energy Site controls."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.number import (
    NumberEntity,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, PERCENTAGE, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    CONF_TESLA_ENERGY_SITE_ID,
    CONF_FOXESS_HOST,
    CONF_GOODWE_HOST,
    CONF_SIGENERGY_STATION_ID,
    CONF_SUNGROW_HOST,
    CONF_ALPHAESS_MODBUS_HOST,
    family_device_info,
    SENSOR_FAMILY_BATTERY,
    SENSOR_FAMILY_EV_CHARGING,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PowerSync number entities."""
    tesla_site_id = entry.options.get(
        CONF_TESLA_ENERGY_SITE_ID,
        entry.data.get(CONF_TESLA_ENERGY_SITE_ID, ""),
    )

    if tesla_site_id:
        # Backup reserve is universally supported for any Tesla energy site.
        async_add_entities([BackupReserveNumber(hass, entry)])

    async def _add_capability_gated_numbers() -> None:
        entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        waited = 0.0
        while "tesla_capabilities" not in entry_data and waited < 120.0:
            await asyncio.sleep(2.0)
            waited += 2.0
            entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        caps = entry_data.get("tesla_capabilities", {})
        if caps.get("off_grid_vehicle_charging_reserve"):
            async_add_entities([OffGridEvReserveNumber(hass, entry)])

    hass.async_create_task(
        _add_capability_gated_numbers(),
        name=f"{DOMAIN}_capability_gated_numbers",
    )

    # Force power slider — for non-Tesla battery systems that accept a power_w
    # parameter on force charge/discharge (FoxESS, GoodWe, Sigenergy, Sungrow,
    # AlphaESS). Stores the user's preferred force power level in kW.
    _non_tesla_battery = any(
        entry.data.get(k) or entry.options.get(k)
        for k in (
            CONF_FOXESS_HOST, CONF_GOODWE_HOST, CONF_SIGENERGY_STATION_ID,
            CONF_SUNGROW_HOST, CONF_ALPHAESS_MODBUS_HOST,
        )
    )
    if _non_tesla_battery:
        async_add_entities([ForcePowerNumber(hass, entry)])


class _TeslaSiteNumberBase(NumberEntity):
    _attr_has_entity_name = True
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        key: str,
        name: str,
        icon: str,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_suggested_object_id = f"power_sync_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_entity_category = EntityCategory.CONFIG

    @property
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_BATTERY)

    def _tesla_coord(self):
        return (
            self.hass.data.get(DOMAIN, {})
            .get(self._entry.entry_id, {})
            .get("tesla_coordinator")
        )


class BackupReserveNumber(_TeslaSiteNumberBase):
    """Backup reserve % for Tesla Powerwall."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass, entry,
            key="tesla_backup_reserve",
            name="Backup Reserve",
            icon="mdi:battery-lock",
        )

    @property
    def native_value(self) -> float | None:
        coord = self._tesla_coord()
        site_info = getattr(coord, "_site_info_cache", None) if coord else None
        if site_info and "backup_reserve_percent" in site_info:
            return float(site_info["backup_reserve_percent"])
        stored = self._entry.options.get("_user_backup_reserve")
        return float(stored) if stored is not None else None

    async def async_set_native_value(self, value: float) -> None:
        await self.hass.services.async_call(
            DOMAIN, "set_backup_reserve", {"percent": int(value)}, blocking=False,
        )


class OffGridEvReserveNumber(_TeslaSiteNumberBase):
    """Off-grid vehicle charging reserve %."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass, entry,
            key="tesla_off_grid_ev_reserve",
            name="Off-Grid EV Reserve",
            icon="mdi:car-electric",
        )

    @property
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_EV_CHARGING)

    @property
    def native_value(self) -> float | None:
        coord = self._tesla_coord()
        if coord is None:
            return None
        val = getattr(coord, "_off_grid_reserve_percent", None)
        return float(val) if val is not None else None

    async def async_set_native_value(self, value: float) -> None:
        await self.hass.services.async_call(
            DOMAIN, "set_off_grid_ev_reserve", {"percent": int(value)}, blocking=False,
        )


class ForcePowerNumber(NumberEntity):
    """User-settable force charge/discharge power target (kW).

    Stores the preferred power level for manual force charge/discharge.
    A value of 0 means "use the inverter's rated/BMS max automatically".
    """

    _attr_has_entity_name = True
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = 50
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_icon = "mdi:lightning-bolt"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_force_power_kw"
        self._attr_suggested_object_id = "power_sync_force_power_kw"
        self._attr_name = "Force Power"
        self._attr_native_value: float = 0.0

    @property
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_BATTERY)

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()
