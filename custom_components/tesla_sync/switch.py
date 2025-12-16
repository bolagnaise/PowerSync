"""Switch platform for Tesla Sync integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    CONF_AUTO_SYNC_ENABLED,
    SWITCH_TYPE_AUTO_SYNC,
    ATTR_LAST_SYNC,
    ATTR_SYNC_STATUS,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tesla Sync switch entities."""
    entities = [
        AutoSyncSwitch(
            entry=entry,
            description=SwitchEntityDescription(
                key=SWITCH_TYPE_AUTO_SYNC,
                name="Auto-Sync TOU Schedule",
                icon="mdi:sync",
            ),
        )
    ]

    async_add_entities(entities)


class AutoSyncSwitch(SwitchEntity):
    """Switch to enable/disable automatic TOU schedule syncing."""

    def __init__(
        self,
        entry: ConfigEntry,
        description: SwitchEntityDescription,
    ) -> None:
        """Initialize the switch."""
        self.entity_description = description
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_has_entity_name = True

        # Initialize state from config
        self._attr_is_on = entry.options.get(
            CONF_AUTO_SYNC_ENABLED,
            entry.data.get(CONF_AUTO_SYNC_ENABLED, True),
        )

    @property
    def is_on(self) -> bool:
        """Return True if the switch is on."""
        return self._attr_is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        _LOGGER.info("Enabling automatic TOU schedule syncing")
        self._attr_is_on = True

        # Update config entry options
        new_options = {**self._entry.options}
        new_options[CONF_AUTO_SYNC_ENABLED] = True
        self.hass.config_entries.async_update_entry(
            self._entry,
            options=new_options,
        )

        self.async_write_ha_state()

        # Trigger an immediate sync
        await self.hass.services.async_call(
            DOMAIN,
            "sync_tou_schedule",
            blocking=False,
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        _LOGGER.info("Disabling automatic TOU schedule syncing")
        self._attr_is_on = False

        # Update config entry options
        new_options = {**self._entry.options}
        new_options[CONF_AUTO_SYNC_ENABLED] = False
        self.hass.config_entries.async_update_entry(
            self._entry,
            options=new_options,
        )

        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        domain_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        amber_coordinator = domain_data.get("amber_coordinator")

        attrs = {}

        if amber_coordinator and amber_coordinator.data:
            attrs[ATTR_LAST_SYNC] = amber_coordinator.data.get("last_update")
            attrs[ATTR_SYNC_STATUS] = "enabled" if self.is_on else "disabled"

        return attrs
