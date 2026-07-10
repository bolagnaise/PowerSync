from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    CONF_BATTERY_CURTAILMENT_ENABLED,
    CONF_FORCE_CHARGE_DURATION,
    CONF_FORCE_DISCHARGE_DURATION,
    CONF_POWERWALL_LOCAL_PAIRED,
    DEFAULT_DISCHARGE_DURATION,
    DISCHARGE_DURATIONS,
    family_device_info,
    SENSOR_FAMILY_BATTERY,
    SENSOR_FAMILY_GRID_HOME,
    TESLA_SITE_INFO_CONTROL_MAX_AGE_SECONDS,
)

_LOGGER = logging.getLogger(__name__)
TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS = 30


def _fresh_powerwall_local_snapshot(hass: HomeAssistant, entry: ConfigEntry) -> Any | None:
    """Return fresh local Powerwall data when paired, otherwise None."""
    if not entry.data.get(CONF_POWERWALL_LOCAL_PAIRED):
        return None
    coordinator = (
        hass.data.get(DOMAIN, {})
        .get(entry.entry_id, {})
        .get("powerwall_local", {})
        .get("coordinator")
    )
    data = getattr(coordinator, "data", None)
    last_success_ts = getattr(coordinator, "last_success_ts", None)
    if data is None or last_success_ts is None:
        return None
    if time.time() - last_success_ts > TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS:
        return None
    return data


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

    # User-facing duration picker for force charge/discharge — Controls, not Configuration.
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
        return family_device_info(self._entry_id, SENSOR_FAMILY_BATTERY)

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

        if entry.options.get(self._key, str(DEFAULT_DISCHARGE_DURATION)) == option:
            self.async_write_ha_state()
            return

        entry_data = self.hass.data.setdefault(DOMAIN, {}).setdefault(self._entry_id, {})
        entry_data["_skip_reload"] = True

        new_options = dict(entry.options)
        new_options[self._key] = option
        self.hass.config_entries.async_update_entry(entry, options=new_options)
        self.async_write_ha_state()


class _TeslaSiteSelectBase(SelectEntity):
    """Base for Tesla Energy Site select entities (Operation Mode, Grid Export)."""

    _attr_has_entity_name = True
    _attr_should_poll = True
    # User-facing — these are primary controls, belong in Controls section.

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
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_BATTERY)

    def _tesla_coord(self):
        return (
            self.hass.data.get(DOMAIN, {})
            .get(self._entry.entry_id, {})
            .get("tesla_coordinator")
        )

    async def async_update(self) -> None:
        """Refresh Tesla site_info often enough for controls changed elsewhere."""
        coord = self._tesla_coord()
        if coord is None:
            return
        try:
            await coord.async_get_site_info(
                max_age=TESLA_SITE_INFO_CONTROL_MAX_AGE_SECONDS,
            )
        except Exception:
            _LOGGER.debug(
                "Could not refresh Tesla site_info for select entity",
                exc_info=True,
            )


class TeslaOperationModeSelect(_TeslaSiteSelectBase):
    """Powerwall operation mode: Time-of-Use, Self-Consumption, or Backup-Only."""

    _OPTIONS = ["autonomous", "self_consumption", "backup"]

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
        local_snap = _fresh_powerwall_local_snapshot(self.hass, self._entry)
        local_mode = getattr(local_snap, "operation_mode", None)
        if local_mode in self._OPTIONS:
            return local_mode

        coord = self._tesla_coord()
        site_info = getattr(coord, "_site_info_cache", None) if coord else None
        if not site_info:
            return None
        mode = site_info.get("default_real_mode")
        return mode if mode in self._OPTIONS else None

    async def async_select_option(self, option: str) -> None:
        await self.hass.services.async_call(
            DOMAIN,
            "set_operation_mode",
            {"mode": option, "source": "user"},
            blocking=False,
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
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_GRID_HOME)

    @property
    def current_option(self) -> str | None:
        # Prefer the cached value written when the user last set the rule
        # via a service call — this survives even when the Tesla API's
        # site_info response omits customer_preferred_export_rule (which
        # happens on VPP / non-export-configured sites). Without this
        # fallback, the select used to show as "unknown" for any such site.
        # Only trust the cache while curtailment is actively managing the
        # rule (mirrors PowerwallSettingsView's gating in __init__.py) — a
        # manual set_grid_export call otherwise pins this cache forever, so
        # once curtailment is off the entity must fall through to the live
        # site_info value instead of a permanently stale rule.
        solar_curtailment_enabled = self._entry.options.get(
            CONF_BATTERY_CURTAILMENT_ENABLED,
            self._entry.data.get(CONF_BATTERY_CURTAILMENT_ENABLED, False),
        )
        if solar_curtailment_enabled:
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
            DOMAIN,
            "set_grid_export",
            {"rule": option, "source": "user"},
            blocking=False,
        )
