from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_FORCE_CHARGE_DURATION,
    CONF_FORCE_DISCHARGE_DURATION,
    DEFAULT_DISCHARGE_DURATION,
    DISCHARGE_DURATIONS,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PowerSync select entities."""
    default_value = str(DEFAULT_DISCHARGE_DURATION)

    # Persist defaults immediately (so values survive restart even before user changes them)
    options = dict(entry.options)
    changed = False
    for key in (CONF_FORCE_CHARGE_DURATION, CONF_FORCE_DISCHARGE_DURATION):
        if key not in options:
            options[key] = default_value
            changed = True

    if changed:
        hass.config_entries.async_update_entry(entry, options=options)

    async_add_entities(
        [
            PowerSyncDurationSelect(
                entry=entry,
                key=CONF_FORCE_CHARGE_DURATION,
                name="Force Charge Duration",
            ),
            PowerSyncDurationSelect(
                entry=entry,
                key=CONF_FORCE_DISCHARGE_DURATION,
                name="Force Discharge Duration",
            ),
        ]
    )


class PowerSyncDurationSelect(SelectEntity):
    """Select entity for choosing a duration in minutes."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:clock-outline"
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, key: str, name: str) -> None:
        self._entry = entry
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_{key}"

        # SelectEntity options must be strings
        self._attr_options = [str(x) for x in DISCHARGE_DURATIONS]

    @property
    def current_option(self) -> str | None:
        """Return the currently selected option."""
        return self._entry.options.get(self._key, str(DEFAULT_DISCHARGE_DURATION))

    async def async_select_option(self, option: str) -> None:
        """Handle user selecting an option."""
        if option not in self.options:
            return

        new_options = dict(self._entry.options)
        new_options[self._key] = option
        self.hass.config_entries.async_update_entry(self._entry, options=new_options)
        self.async_write_ha_state()
