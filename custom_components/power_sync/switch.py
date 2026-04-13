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
    CONF_MONITORING_MODE,
    CONF_POWERWALL_LOCAL_PAIRED,
    SWITCH_TYPE_AUTO_SYNC,
    SWITCH_TYPE_FORCE_DISCHARGE,
    SWITCH_TYPE_FORCE_CHARGE,
    SWITCH_TYPE_MONITORING_MODE,
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
        CONF_TESLA_ENERGY_SITE_ID, entry.data.get(CONF_TESLA_ENERGY_SITE_ID, "")
    )
    is_tesla = bool(tesla_site_id)

    # Detect electricity provider for TOU sync relevance
    electricity_provider = entry.options.get(
        CONF_ELECTRICITY_PROVIDER, entry.data.get(CONF_ELECTRICITY_PROVIDER, "amber")
    )
    has_tou_sync = electricity_provider in PROVIDERS_WITH_TOU_SYNC

    _LOGGER.info(
        f"🔋 Switch setup: is_tesla={is_tesla}, provider={electricity_provider}, has_tou_sync={has_tou_sync}"
    )

    entities = []

    # Monitoring mode switch — always available for all battery systems
    entities.append(
        MonitoringModeSwitch(
            hass=hass,
            entry=entry,
            description=SwitchEntityDescription(
                key=SWITCH_TYPE_MONITORING_MODE,
                name="Monitoring Mode",
                icon="mdi:eye-outline",
            ),
        ),
    )

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
        _LOGGER.info(
            "Tesla battery system detected - adding force charge/discharge switches"
        )
        entities.extend(
            [
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
                GridChargingSwitch(hass=hass, entry=entry),
            ]
        )

    # Off-grid switch — available when Powerwall is paired for local control
    if is_tesla and entry.data.get(CONF_POWERWALL_LOCAL_PAIRED):
        entities.append(OffGridSwitch(hass=hass, entry=entry))

    async_add_entities(entities)

    # Capability-gated Tesla entities (storm watch, VPP program switches).
    # These cannot be added until the Tesla capability probe completes,
    # which runs ~after the first site_info fetch. We wait for that in a
    # background task and add them once.
    if is_tesla:

        async def _add_capability_gated_switches() -> None:
            entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
            waited = 0.0
            while "tesla_capabilities" not in entry_data and waited < 120.0:
                await asyncio.sleep(2.0)
                waited += 2.0
                entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
            caps = entry_data.get("tesla_capabilities", {})
            if not caps:
                _LOGGER.info(
                    "Tesla capability probe did not complete within 120s — "
                    "skipping capability-gated switch creation"
                )
                return

            tesla_coord = entry_data.get("tesla_coordinator")
            to_add: list[SwitchEntity] = []
            if caps.get("storm_mode"):
                to_add.append(StormWatchSwitch(hass=hass, entry=entry))
            if caps.get("vpp_programs") and tesla_coord is not None:
                programs = getattr(tesla_coord, "_vpp_programs_cache", None) or []
                for program in programs:
                    to_add.append(
                        VppProgramSwitch(hass=hass, entry=entry, program=program)
                    )
            if to_add:
                _LOGGER.info(
                    "Adding %d capability-gated Tesla switches (storm_mode=%s, vpp=%d)",
                    len(to_add),
                    caps.get("storm_mode"),
                    len(getattr(tesla_coord, "_vpp_programs_cache", None) or [])
                    if tesla_coord
                    else 0,
                )
                async_add_entities(to_add)

        hass.async_create_task(
            _add_capability_gated_switches(),
            name=f"{DOMAIN}_capability_gated_switches",
        )


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
    def device_info(self):
        """Return device info to link to the PowerSync device."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
        }

    @property
    def is_on(self) -> bool:
        """Return True if the switch is on."""
        return self._attr_is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        # Log context to help debug if triggered by automation vs user
        context = kwargs.get("context")
        if context:
            _LOGGER.info(
                "Auto-sync switch activated (context: user_id=%s, parent_id=%s)",
                context.user_id,
                context.parent_id,
            )
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
    def device_info(self):
        """Return device info to link to the PowerSync device."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
        }

    @property
    def is_on(self) -> bool:
        """Return True if force discharge is active."""
        return self._attr_is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on force discharge mode."""
        # Log context to help debug if triggered by automation vs user
        context = kwargs.get("context")
        if context:
            _LOGGER.info(
                "Force discharge switch activated (context: user_id=%s, parent_id=%s)",
                context.user_id,
                context.parent_id,
            )
        else:
            _LOGGER.info(
                "Force discharge switch activated (no context - likely UI action)"
            )
        _LOGGER.info(
            "Activating force discharge mode for %d minutes", self._duration_minutes
        )

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
            if (
                self._discharge_expires_at
                and datetime.now() >= self._discharge_expires_at
            ):
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
    def device_info(self):
        """Return device info to link to the PowerSync device."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
        }

    @property
    def is_on(self) -> bool:
        """Return True if force charge is active."""
        return self._attr_is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on force charge mode."""
        _LOGGER.info(
            "Activating force charge mode for %d minutes", self._duration_minutes
        )

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


class MonitoringModeSwitch(SwitchEntity):
    """Switch to enable monitoring-only mode (blocks all battery/inverter control)."""

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
        self._attr_suggested_object_id = f"power_sync_{description.key}"

        self._attr_is_on = entry.options.get(
            CONF_MONITORING_MODE,
            entry.data.get(CONF_MONITORING_MODE, False),
        )

    @property
    def device_info(self):
        """Return device info to link to the PowerSync device."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
        }

    @property
    def is_on(self) -> bool:
        """Return True if monitoring mode is active."""
        return self._attr_is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable monitoring mode — all control commands will be logged but not executed."""
        _LOGGER.info(
            "Monitoring mode ENABLED — all battery/inverter commands will be blocked"
        )
        self._attr_is_on = True

        new_options = {**self._entry.options}
        new_options[CONF_MONITORING_MODE] = True
        self.hass.config_entries.async_update_entry(
            self._entry,
            options=new_options,
        )

        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable monitoring mode — resume normal battery/inverter control."""
        _LOGGER.info(
            "Monitoring mode DISABLED — normal battery/inverter control resumed"
        )
        self._attr_is_on = False

        new_options = {**self._entry.options}
        new_options[CONF_MONITORING_MODE] = False
        self.hass.config_entries.async_update_entry(
            self._entry,
            options=new_options,
        )

        self.async_write_ha_state()


class _TeslaSiteSwitchBase(SwitchEntity):
    """Base for Tesla Energy Site switches that call coordinator methods."""

    _attr_has_entity_name = True

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, key: str, name: str, icon: str
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_suggested_object_id = f"power_sync_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_is_on: bool | None = None

    @property
    def device_info(self):
        return {"identifiers": {(DOMAIN, self._entry.entry_id)}}

    def _tesla_coord(self):
        return (
            self.hass.data.get(DOMAIN, {})
            .get(self._entry.entry_id, {})
            .get("tesla_coordinator")
        )


class GridChargingSwitch(_TeslaSiteSwitchBase):
    """Toggle whether the Powerwall may charge from grid (TOU arbitrage)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            entry,
            key="tesla_grid_charging",
            name="Grid Charging",
            icon="mdi:transmission-tower-import",
        )

    @property
    def is_on(self) -> bool | None:
        coord = self._tesla_coord()
        site_info = getattr(coord, "_site_info_cache", None) if coord else None
        if not site_info:
            return self._attr_is_on
        components = site_info.get("components", {}) or {}
        disallow = components.get(
            "disallow_charge_from_grid_with_solar_installed",
            site_info.get("disallow_charge_from_grid_with_solar_installed", False),
        )
        return not bool(disallow)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.hass.services.async_call(
            DOMAIN,
            "set_grid_charging",
            {"enabled": True},
            blocking=False,
        )
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.hass.services.async_call(
            DOMAIN,
            "set_grid_charging",
            {"enabled": False},
            blocking=False,
        )
        self._attr_is_on = False
        self.async_write_ha_state()


class StormWatchSwitch(_TeslaSiteSwitchBase):
    """Toggle Tesla Storm Watch (predictive pre-charging before severe weather)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            entry,
            key="tesla_storm_watch",
            name="Storm Watch",
            icon="mdi:weather-lightning",
        )

    @property
    def is_on(self) -> bool | None:
        coord = self._tesla_coord()
        if coord is None:
            return None
        return getattr(coord, "_storm_mode_enabled", None)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.hass.services.async_call(
            DOMAIN,
            "set_storm_watch",
            {"enabled": True},
            blocking=False,
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.hass.services.async_call(
            DOMAIN,
            "set_storm_watch",
            {"enabled": False},
            blocking=False,
        )


class VppProgramSwitch(_TeslaSiteSwitchBase):
    """Enrollment toggle for a single Tesla VPP / grid-services program."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, program: dict) -> None:
        pid = (
            program.get("id")
            or program.get("program_id")
            or program.get("name")
            or "unknown"
        )
        pid_str = str(pid)
        safe_key = "".join(c if c.isalnum() else "_" for c in pid_str.lower())
        display_name = program.get("display_name") or program.get("name") or pid_str
        super().__init__(
            hass,
            entry,
            key=f"tesla_vpp_{safe_key}",
            name=f"VPP: {display_name}",
            icon="mdi:transmission-tower",
        )
        self._program_id = pid_str
        self._program = program

    def _current_program(self) -> dict | None:
        coord = self._tesla_coord()
        if coord is None:
            return self._program
        programs = getattr(coord, "_vpp_programs_cache", None) or []
        for p in programs:
            if (
                str(p.get("id") or p.get("program_id") or p.get("name"))
                == self._program_id
            ):
                return p
        return self._program

    @property
    def is_on(self) -> bool | None:
        p = self._current_program()
        if not p:
            return None
        val = p.get("enrolled")
        if val is None:
            val = p.get("is_enrolled")
        if val is None:
            val = p.get("user_enrolled")
        return bool(val) if val is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        p = self._current_program() or {}
        return {
            "program_id": self._program_id,
            "display_name": p.get("display_name") or p.get("name"),
            "description": p.get("description"),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.hass.services.async_call(
            DOMAIN,
            "set_vpp_enrollment",
            {"program_id": self._program_id, "enrolled": True},
            blocking=False,
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.hass.services.async_call(
            DOMAIN,
            "set_vpp_enrollment",
            {"program_id": self._program_id, "enrolled": False},
            blocking=False,
        )


class OffGridSwitch(SwitchEntity):
    """Switch to take the Powerwall off-grid (islanding) or reconnect.

    ON  = off-grid (contactor open, running on battery)
    OFF = on-grid  (contactor closed, normal operation)

    State is read from the PowerwallLocalCoordinator's snapshot so it
    reflects the actual gateway state, not just what we last commanded.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:transmission-tower-off"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_off_grid"
        self._attr_suggested_object_id = "power_sync_off_grid"
        self._attr_name = "Off-Grid"

    @property
    def device_info(self):
        return {"identifiers": {(DOMAIN, self._entry.entry_id)}}

    @property
    def is_on(self) -> bool | None:
        """True when the Powerwall is islanded (off-grid)."""
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        runtime = entry_data.get("powerwall_local") or {}
        coord = runtime.get("coordinator")
        if coord is None:
            return None
        snap = coord.data
        if snap is None or snap.grid_status is None:
            return None
        return "island" in snap.grid_status.lower()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Go off-grid."""
        _LOGGER.info("Off-grid switch: going off-grid")
        await self.hass.services.async_call(
            DOMAIN,
            "powerwall_go_off_grid",
            {},
            blocking=True,
        )
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Reconnect to grid."""
        _LOGGER.info("Off-grid switch: reconnecting to grid")
        await self.hass.services.async_call(
            DOMAIN,
            "powerwall_reconnect_grid",
            {},
            blocking=True,
        )
        self.async_write_ha_state()
