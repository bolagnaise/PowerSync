from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
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

    # Prefer integration-prefixed entity_ids, and migrate older un-prefixed ones.
    # We only rename entities that belong to THIS config entry (via unique_id),
    # and we never overwrite an existing entity_id.
    ent_reg = er.async_get(hass)
    desired_object_ids = {
        CONF_FORCE_CHARGE_DURATION: "power_sync_force_charge_duration",
        CONF_FORCE_DISCHARGE_DURATION: "power_sync_force_discharge_duration",
    }
    legacy_entity_ids = {
        CONF_FORCE_CHARGE_DURATION: "select.force_charge_duration",
        CONF_FORCE_DISCHARGE_DURATION: "select.force_discharge_duration",
    }

    for key, object_id in desired_object_ids.items():
        unique_id = f"{entry.entry_id}_{key}"
        current_entity_id = ent_reg.async_get_entity_id("select", entry.domain, unique_id)
        if current_entity_id is None:
            continue

        desired_entity_id = f"select.{object_id}"
        legacy_entity_id = legacy_entity_ids[key]

        # Only migrate the specific legacy ids -> desired ids.
        if current_entity_id == legacy_entity_id and current_entity_id != desired_entity_id:
            if ent_reg.async_get(desired_entity_id) is None:
                ent_reg.async_update_entity(current_entity_id, new_entity_id=desired_entity_id)

    async_add_entities(
        [
            PowerSyncDurationSelect(
                entry=entry,
                key=CONF_FORCE_CHARGE_DURATION,
                name="Force Charge Duration",
                suggested_object_id=desired_object_ids[CONF_FORCE_CHARGE_DURATION],
            ),
            PowerSyncDurationSelect(
                entry=entry,
                key=CONF_FORCE_DISCHARGE_DURATION,
                name="Force Discharge Duration",
                suggested_object_id=desired_object_ids[CONF_FORCE_DISCHARGE_DURATION],
            ),
        ]
    )


class PowerSyncDurationSelect(SelectEntity):
    """Select entity for choosing a duration in minutes."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:clock-outline"
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, key: str, name: str, suggested_object_id: str) -> None:
        self._entry_id = entry.entry_id
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_{key}"

        # Controls the default entity_id Home Assistant generates for new installs
        self._attr_suggested_object_id = suggested_object_id

        # SelectEntity options must be strings
        self._attr_options = [str(x) for x in DISCHARGE_DURATIONS]

    def _get_entry(self) -> ConfigEntry | None:
        """Get the current config entry (not a stale reference)."""
        return self.hass.config_entries.async_get_entry(self._entry_id)

    @property
    def current_option(self) -> str | None:
        """Return the currently selected option."""
        entry = self._get_entry()
        if entry is None:
            return str(DEFAULT_DISCHARGE_DURATION)
        return entry.options.get(self._key, str(DEFAULT_DISCHARGE_DURATION))

    async def async_select_option(self, option: str) -> None:
        """Handle user selecting an option."""
        if option not in self.options:
            return

        entry = self._get_entry()
        if entry is None:
            return

        new_options = dict(entry.options)
        new_options[self._key] = option
        self.hass.config_entries.async_update_entry(entry, options=new_options)
        self.async_write_ha_state()
