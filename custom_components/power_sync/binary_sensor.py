"""Binary sensor platform for PowerSync integration — Tesla Energy Site status."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorDeviceClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, CONF_TESLA_ENERGY_SITE_ID

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PowerSync binary sensors."""
    tesla_site_id = entry.options.get(
        CONF_TESLA_ENERGY_SITE_ID,
        entry.data.get(CONF_TESLA_ENERGY_SITE_ID, ""),
    )
    if not tesla_site_id:
        return

    # Manual export override is always available for Tesla sites.
    async_add_entities([ManualExportOverrideBinarySensor(hass, entry)])

    async def _add_capability_gated_binary_sensors() -> None:
        entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        waited = 0.0
        while "tesla_capabilities" not in entry_data and waited < 120.0:
            await asyncio.sleep(2.0)
            waited += 2.0
            entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        caps = entry_data.get("tesla_capabilities", {})
        if caps.get("storm_mode"):
            async_add_entities([StormWatchActiveBinarySensor(hass, entry)])

    hass.async_create_task(
        _add_capability_gated_binary_sensors(),
        name=f"{DOMAIN}_capability_gated_binary_sensors",
    )


class _TeslaBinarySensorBase(BinarySensorEntity):
    _attr_has_entity_name = True

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

    @property
    def device_info(self):
        return {"identifiers": {(DOMAIN, self._entry.entry_id)}}

    def _tesla_coord(self):
        return (
            self.hass.data.get(DOMAIN, {})
            .get(self._entry.entry_id, {})
            .get("tesla_coordinator")
        )


class StormWatchActiveBinarySensor(_TeslaBinarySensorBase):
    """True while Tesla reports a storm is actively being predicted/prepared for."""

    _attr_device_class = BinarySensorDeviceClass.SAFETY

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            entry,
            key="tesla_storm_watch_active",
            name="Storm Watch Active",
            icon="mdi:weather-lightning-rainy",
        )

    @property
    def is_on(self) -> bool | None:
        coord = self._tesla_coord()
        if coord is None:
            return None
        site_info = getattr(coord, "_site_info_cache", None) or {}
        if "storm_mode_active" in site_info:
            return bool(site_info["storm_mode_active"])
        components = site_info.get("components", {}) or {}
        if "storm_mode_active" in components:
            return bool(components["storm_mode_active"])
        return None


class ManualExportOverrideBinarySensor(_TeslaBinarySensorBase):
    """True when the user has taken manual control of grid export rules,
    bypassing automatic optimiser control."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            entry,
            key="tesla_manual_export_override",
            name="Manual Export Override",
            icon="mdi:hand-back-right",
        )

    @property
    def is_on(self) -> bool | None:
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        return bool(entry_data.get("manual_export_override", False))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        return {
            "manual_export_rule": entry_data.get("manual_export_rule"),
        }
