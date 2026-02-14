"""Switch platform for PowerSync integration."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    DOMAIN,
    CONF_AUTO_SYNC_ENABLED,
    CONF_ELECTRICITY_PROVIDER,
    SWITCH_TYPE_AUTO_SYNC,
    SWITCH_TYPE_FORCE_DISCHARGE,
    SWITCH_TYPE_FORCE_CHARGE,
    DEFAULT_DISCHARGE_DURATION,
    ATTR_LAST_SYNC,
    ATTR_SYNC_STATUS,
)

# Providers that use TOU schedule syncing (Amber, Octopus, Flow Power)
# GloBird and AEMO VPP use spike detection only — no TOU sync
PROVIDERS_WITH_TOU_SYNC = {"amber", "octopus", "flow_power"}

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PowerSync switch entities."""
    # Detect Tesla by checking if tesla_energy_site_id is configured
    from .const import CONF_TESLA_ENERGY_SITE_ID
    tesla_site_id = entry.options.get(
        CONF_TESLA_ENERGY_SITE_ID,
        entry.data.get(CONF_TESLA_ENERGY_SITE_ID, "")
    )
    is_tesla = bool(tesla_site_id)

    # Detect electricity provider for TOU sync relevance
    electricity_provider = entry.options.get(
        CONF_ELECTRICITY_PROVIDER,
        entry.data.get(CONF_ELECTRICITY_PROVIDER, "amber")
    )
    has_tou_sync = electricity_provider in PROVIDERS_WITH_TOU_SYNC

    _LOGGER.info(f"🔋 Switch setup: is_tesla={is_tesla}, provider={electricity_provider}, has_tou_sync={has_tou_sync}")

    entities = []

    # Only add auto-sync switch for providers that actually sync TOU schedules
    if has_tou_sync:
        entities.append(
            AutoSyncSwitch(
                hass=hass,
                entry=entry,
                description=SwitchEntityDescription(
                    key=SWITCH_TYPE_AUTO_SYNC,
                    name="Auto-Sync TOU Schedule",
                    icon="mdi:sync",
                ),
            ),
        )

    # Add Tesla-specific switches only if Tesla is selected as battery system
    if is_tesla:
        _LOGGER.info("Tesla battery system detected - adding force charge/discharge switches")
        entities.extend([
            ForceDischargeSwitch(
                hass=hass,
                entry=entry,
                description=SwitchEntityDescription(
                    key=SWITCH_TYPE_FORCE_DISCHARGE,
                    name="Force Discharge",
                    icon="mdi:battery-arrow-up",
                ),
            ),
            ForceChargeSwitch(
                hass=hass,
                entry=entry,
                description=SwitchEntityDescription(
                    key=SWITCH_TYPE_FORCE_CHARGE,
                    name="Force Charge",
                    icon="mdi:battery-arrow-down",
                ),
            ),
        ])

    async_add_entities(entities)


class AutoSyncSwitch(SwitchEntity):
    """Switch to enable/disable automatic TOU schedule syncing."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        description: SwitchEntityDescription,
    ) -> None:
        """Initialize the switch."""
        self.hass = hass
        self.entity_description = description
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        # HA 2026.2.0+ requires lowercase suggested_object_id
        self._attr_suggested_object_id = f"power_sync_{description.key}"

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
        # Log context to help debug if triggered by automation vs user
        context = kwargs.get("context")
        if context:
            _LOGGER.info("Auto-sync switch activated (context: user_id=%s, parent_id=%s)",
                        context.user_id, context.parent_id)
        else:
            _LOGGER.info("Auto-sync switch activated (no context - likely UI action)")
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


class ForceDischargeSwitch(SwitchEntity):
    """Switch to manually force battery discharge mode."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        description: SwitchEntityDescription,
    ) -> None:
        """Initialize the switch."""
        self.hass = hass
        self.entity_description = description
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        # HA 2026.2.0+ requires lowercase suggested_object_id
        self._attr_suggested_object_id = f"power_sync_{description.key}"
        self._attr_is_on = False
        self._discharge_expires_at: datetime | None = None
        self._duration_minutes: int = DEFAULT_DISCHARGE_DURATION
        self._cancel_expiry_timer = None

    @property
    def is_on(self) -> bool:
        """Return True if force discharge is active."""
        return self._attr_is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on force discharge mode."""
        # Log context to help debug if triggered by automation vs user
        context = kwargs.get("context")
        if context:
            _LOGGER.info("Force discharge switch activated (context: user_id=%s, parent_id=%s)",
                        context.user_id, context.parent_id)
        else:
            _LOGGER.info("Force discharge switch activated (no context - likely UI action)")
        _LOGGER.info("Activating force discharge mode for %d minutes", self._duration_minutes)

        # Get the duration from service call data if provided
        duration = kwargs.get("duration", self._duration_minutes)

        # Call the force discharge service
        try:
            await self.hass.services.async_call(
                DOMAIN,
                "force_discharge",
                {"duration": duration},
                blocking=True,
            )

            self._attr_is_on = True
            self._discharge_expires_at = datetime.now() + timedelta(minutes=duration)
            self._duration_minutes = duration

            # Set up expiry timer
            self._schedule_expiry_check()

            self.async_write_ha_state()

        except Exception as err:
            _LOGGER.error("Failed to activate force discharge: %s", err)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off force discharge mode (restore normal operation)."""
        _LOGGER.info("Deactivating force discharge mode, restoring normal operation")

        try:
            await self.hass.services.async_call(
                DOMAIN,
                "restore_normal",
                {},
                blocking=True,
            )

            self._attr_is_on = False
            self._discharge_expires_at = None

            # Cancel any pending expiry timer
            if self._cancel_expiry_timer:
                self._cancel_expiry_timer()
                self._cancel_expiry_timer = None

            self.async_write_ha_state()

        except Exception as err:
            _LOGGER.error("Failed to restore normal operation: %s", err)

    def _schedule_expiry_check(self) -> None:
        """Schedule periodic check for discharge expiry."""
        # Cancel any existing timer
        if self._cancel_expiry_timer:
            self._cancel_expiry_timer()

        @callback
        def _check_expiry(now: datetime) -> None:
            """Check if discharge has expired."""
            if self._discharge_expires_at and datetime.now() >= self._discharge_expires_at:
                _LOGGER.info("Force discharge expired, restoring normal operation")
                self._attr_is_on = False
                self._discharge_expires_at = None
                self._cancel_expiry_timer = None
                self.async_write_ha_state()
            elif self._attr_is_on:
                # Schedule next check
                self._schedule_expiry_check()

        # Check every 30 seconds
        self._cancel_expiry_timer = async_track_time_interval(
            self.hass, _check_expiry, timedelta(seconds=30)
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        attrs = {
            "duration_minutes": self._duration_minutes,
        }

        if self._discharge_expires_at:
            attrs["expires_at"] = self._discharge_expires_at.isoformat()
            remaining = self._discharge_expires_at - datetime.now()
            if remaining.total_seconds() > 0:
                attrs["remaining_minutes"] = int(remaining.total_seconds() / 60)
            else:
                attrs["remaining_minutes"] = 0

        return attrs

    async def async_will_remove_from_hass(self) -> None:
        """Clean up when entity is removed."""
        if self._cancel_expiry_timer:
            self._cancel_expiry_timer()
            self._cancel_expiry_timer = None


class ForceChargeSwitch(SwitchEntity):
    """Switch to manually force battery charge mode."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        description: SwitchEntityDescription,
    ) -> None:
        """Initialize the switch."""
        self.hass = hass
        self.entity_description = description
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        # HA 2026.2.0+ requires lowercase suggested_object_id
        self._attr_suggested_object_id = f"power_sync_{description.key}"
        self._attr_is_on = False
        self._charge_expires_at: datetime | None = None
        self._duration_minutes: int = DEFAULT_DISCHARGE_DURATION  # Reuse same default
        self._cancel_expiry_timer = None

    @property
    def is_on(self) -> bool:
        """Return True if force charge is active."""
        return self._attr_is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on force charge mode."""
        _LOGGER.info("Activating force charge mode for %d minutes", self._duration_minutes)

        # Get the duration from service call data if provided
        duration = kwargs.get("duration", self._duration_minutes)

        # Call the force charge service
        try:
            await self.hass.services.async_call(
                DOMAIN,
                "force_charge",
                {"duration": duration},
                blocking=True,
            )

            self._attr_is_on = True
            self._charge_expires_at = datetime.now() + timedelta(minutes=duration)
            self._duration_minutes = duration

            # Set up expiry timer
            self._schedule_expiry_check()

            self.async_write_ha_state()

        except Exception as err:
            _LOGGER.error("Failed to activate force charge: %s", err)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off force charge mode (restore normal operation)."""
        _LOGGER.info("Deactivating force charge mode, restoring normal operation")

        try:
            await self.hass.services.async_call(
                DOMAIN,
                "restore_normal",
                {},
                blocking=True,
            )

            self._attr_is_on = False
            self._charge_expires_at = None

            # Cancel any pending expiry timer
            if self._cancel_expiry_timer:
                self._cancel_expiry_timer()
                self._cancel_expiry_timer = None

            self.async_write_ha_state()

        except Exception as err:
            _LOGGER.error("Failed to restore normal operation: %s", err)

    def _schedule_expiry_check(self) -> None:
        """Schedule periodic check for charge expiry."""
        # Cancel any existing timer
        if self._cancel_expiry_timer:
            self._cancel_expiry_timer()

        @callback
        def _check_expiry(now: datetime) -> None:
            """Check if charge has expired."""
            if self._charge_expires_at and datetime.now() >= self._charge_expires_at:
                _LOGGER.info("Force charge expired, restoring normal operation")
                self._attr_is_on = False
                self._charge_expires_at = None
                self._cancel_expiry_timer = None
                self.async_write_ha_state()
            elif self._attr_is_on:
                # Schedule next check
                self._schedule_expiry_check()

        # Check every 30 seconds
        self._cancel_expiry_timer = async_track_time_interval(
            self.hass, _check_expiry, timedelta(seconds=30)
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        attrs = {
            "duration_minutes": self._duration_minutes,
        }

        if self._charge_expires_at:
            attrs["expires_at"] = self._charge_expires_at.isoformat()
            remaining = self._charge_expires_at - datetime.now()
            if remaining.total_seconds() > 0:
                attrs["remaining_minutes"] = int(remaining.total_seconds() / 60)
            else:
                attrs["remaining_minutes"] = 0

        return attrs

    async def async_will_remove_from_hass(self) -> None:
        """Clean up when entity is removed."""
        if self._cancel_expiry_timer:
            self._cancel_expiry_timer()
            self._cancel_expiry_timer = None
