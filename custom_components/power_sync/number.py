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
from homeassistant.const import EntityCategory, PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    CONF_OPTIMIZATION_ENABLED,
    CONF_OPTIMIZATION_MAX_SOC,
    CONF_OPTIMIZATION_PROVIDER,
    CONF_TESLA_ENERGY_SITE_ID,
    DEFAULT_OPTIMIZATION_MAX_SOC,
    OPT_PROVIDER_POWERSYNC,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PowerSync number entities for Tesla Energy Site."""
    tesla_site_id = entry.options.get(
        CONF_TESLA_ENERGY_SITE_ID,
        entry.data.get(CONF_TESLA_ENERGY_SITE_ID, ""),
    )
    if not tesla_site_id:
        return

    # Backup reserve is universally supported for any Tesla energy site, so
    # we add it immediately without waiting for the capability probe.
    entities: list[NumberEntity] = [BackupReserveNumber(hass, entry)]

    # Add MaxSOCNumber when Smart Optimization is enabled
    optimization_enabled = (
        entry.options.get(
            CONF_OPTIMIZATION_PROVIDER,
            entry.data.get(CONF_OPTIMIZATION_PROVIDER),
        ) == OPT_PROVIDER_POWERSYNC
        and entry.options.get(CONF_OPTIMIZATION_ENABLED, entry.data.get(CONF_OPTIMIZATION_ENABLED, False))
    )
    if optimization_enabled:
        entities.append(MaxSOCNumber(hass, entry))

    async_add_entities(entities)

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
        return {"identifiers": {(DOMAIN, self._entry.entry_id)}}

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
            hass,
            entry,
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
            DOMAIN,
            "set_backup_reserve",
            {"percent": int(value)},
            blocking=False,
        )


class OffGridEvReserveNumber(_TeslaSiteNumberBase):
    """Off-grid vehicle charging reserve %."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            entry,
            key="tesla_off_grid_ev_reserve",
            name="Off-Grid EV Reserve",
            icon="mdi:car-electric",
        )

    @property
    def native_value(self) -> float | None:
        coord = self._tesla_coord()
        if coord is None:
            return None
        val = getattr(coord, "_off_grid_reserve_percent", None)
        return float(val) if val is not None else None

    async def async_set_native_value(self, value: float) -> None:
        await self.hass.services.async_call(
            DOMAIN,
            "set_off_grid_ev_reserve",
            {"percent": int(value)},
            blocking=False,
        )


class MaxSOCNumber(_TeslaSiteNumberBase):
    """Max charge SOC % for LP optimizer."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            entry,
            key="max_soc",
            name="Max Charge SOC",
            icon="mdi:battery-arrow-up",
        )
        self._attr_native_min_value = 50
        self._attr_native_max_value = 100
        self._attr_native_step = 5

    @property
    def native_value(self) -> float | None:
        stored = self._entry.options.get(CONF_OPTIMIZATION_MAX_SOC)
        if stored is not None:
            # Stored as percentage (50-100) or fraction (0.5-1.0)
            val = float(stored)
            if val <= 1.0:
                return val * 100
            return val
        return DEFAULT_OPTIMIZATION_MAX_SOC * 100

    async def async_set_native_value(self, value: float) -> None:
        await self.hass.services.async_call(
            DOMAIN,
            "set_max_soc",
            {"percent": int(value), "entry_id": self._entry.entry_id},
            blocking=False,
        )
