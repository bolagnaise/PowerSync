from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
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

    select_entities = [
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

    # Tesla Energy Site selects (universally supported — no capability probe needed)
    from .const import CONF_TESLA_ENERGY_SITE_ID
    tesla_site_id = entry.options.get(
        CONF_TESLA_ENERGY_SITE_ID,
        entry.data.get(CONF_TESLA_ENERGY_SITE_ID, ""),
    )
    if tesla_site_id:
        select_entities.extend([
            TeslaOperationModeSelect(hass=hass, entry=entry),
            TeslaGridExportRuleSelect(hass=hass, entry=entry),
        ])

    async_add_entities(select_entities)


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

    @property
    def device_info(self):
        """Return device info to link to the PowerSync device."""
        return {
            "identifiers": {(DOMAIN, self._entry_id)},
        }

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


class _TeslaSiteSelectBase(SelectEntity):
    """Base for Tesla Energy Site select entities."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        key: str,
        name: str,
        icon: str,
        options: list[str],
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_suggested_object_id = f"power_sync_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_options = options

    @property
    def device_info(self):
        return {"identifiers": {(DOMAIN, self._entry.entry_id)}}

    def _tesla_coord(self):
        return (
            self.hass.data.get(DOMAIN, {})
            .get(self._entry.entry_id, {})
            .get("tesla_coordinator")
        )


class TeslaOperationModeSelect(_TeslaSiteSelectBase):
    """Powerwall operation mode: Time-of-Use (autonomous) or Self-Consumption."""

    _OPTIONS = ["autonomous", "self_consumption"]

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass, entry,
            key="tesla_operation_mode",
            name="Operation Mode",
            icon="mdi:cog-transfer",
            options=self._OPTIONS,
        )

    @property
    def current_option(self) -> str | None:
        coord = self._tesla_coord()
        site_info = getattr(coord, "_site_info_cache", None) if coord else None
        if not site_info:
            return None
        mode = site_info.get("default_real_mode")
        return mode if mode in self._OPTIONS else None

    async def async_select_option(self, option: str) -> None:
        await self.hass.services.async_call(
            DOMAIN, "set_operation_mode", {"mode": option}, blocking=False,
        )


class TeslaGridExportRuleSelect(_TeslaSiteSelectBase):
    """Grid export rule: never / pv_only / battery_ok."""

    _OPTIONS = ["never", "pv_only", "battery_ok"]

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass, entry,
            key="tesla_grid_export_rule",
            name="Grid Export Rule",
            icon="mdi:transmission-tower-export",
            options=self._OPTIONS,
        )

    @property
    def current_option(self) -> str | None:
        # Prefer the cached value written when the user last set the rule
        # via a service call — this survives even when the Tesla API's
        # site_info response omits customer_preferred_export_rule (which
        # happens on VPP / non-export-configured sites). Without this
        # fallback, the select used to show as "unknown" for any such site.
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        cached = entry_data.get("cached_export_rule")
        if cached in self._OPTIONS:
            return cached

        coord = self._tesla_coord()
        site_info = getattr(coord, "_site_info_cache", None) if coord else None
        if not site_info:
            # No site info yet and no cached value — default to battery_ok
            # so the entity is always selectable rather than stuck unknown.
            return "battery_ok"
        components = site_info.get("components", {}) or {}
        rule = components.get(
            "customer_preferred_export_rule",
            site_info.get("customer_preferred_export_rule"),
        )
        if rule in self._OPTIONS:
            return rule
        if components.get("non_export_configured"):
            return "never"
        # Same fallback as PowerwallSettingsView: when the API omits the
        # rule (typical for VPP users), default to battery_ok so the entity
        # always has a valid value rather than reporting unknown.
        return "battery_ok"

    async def async_select_option(self, option: str) -> None:
        await self.hass.services.async_call(
            DOMAIN, "set_grid_export", {"rule": option}, blocking=False,
        )
