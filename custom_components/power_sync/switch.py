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
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    DOMAIN,
    CONF_AUTO_SYNC_ENABLED,
    CONF_AUTO_UPDATE_ENABLED,
    CONF_AUTO_UPDATE_TIME,
    CONF_BATTERY_SYSTEM,
    CONF_FORCE_CHARGE_DURATION,
    CONF_FORCE_DISCHARGE_DURATION,
    DEFAULT_AUTO_UPDATE_TIME,
    CONF_ELECTRICITY_PROVIDER,
    CONF_MONITORING_MODE,
    CONF_OPTIMIZATION_AUTO_APPLY_RESERVE,
    CONF_OPTIMIZATION_BACKUP_RESERVE,
    CONF_OPTIMIZATION_DISABLE_IDLE,
    CONF_OPTIMIZATION_ENABLED,
    CONF_OPTIMIZATION_MANUAL_RESERVE,
    CONF_OPTIMIZATION_PROVIDER,
    CONF_OPTIMIZATION_SPREAD_EXPORT_ENABLED,
    CONF_OPTIMIZATION_SPREAD_IMPORT_ENABLED,
    CONF_POWERWALL_LOCAL_PAIRED,
    CONF_TESLA_ENERGY_SITE_ID,
    BATTERY_SYSTEM_TESLA,
    OPT_PROVIDER_POWERSYNC,
    TARGET_CHARGE_POWER_BATTERY_SYSTEMS,
    TARGET_EXPORT_POWER_BATTERY_SYSTEMS,
    SWITCH_TYPE_AUTO_SYNC,
    SWITCH_TYPE_AUTO_UPDATE,
    SWITCH_TYPE_FORCE_DISCHARGE,
    SWITCH_TYPE_FORCE_CHARGE,
    SWITCH_TYPE_MONITORING_MODE,
    SWITCH_TYPE_AWAY_MODE,
    SWITCH_TYPE_PROFIT_MAX_MODE,
    SWITCH_TYPE_OPTIMIZATION_DISABLE_IDLE,
    SWITCH_TYPE_OPTIMIZATION_SPREAD_EXPORT,
    SWITCH_TYPE_OPTIMIZATION_SPREAD_IMPORT,
    SWITCH_TYPE_OPTIMIZATION_ENABLED,
    SWITCH_TYPE_OPTIMIZATION_AUTO_APPLY_RESERVE,
    DEFAULT_DISCHARGE_DURATION,
    ATTR_LAST_SYNC,
    ATTR_SYNC_STATUS,
    family_device_info,
    SENSOR_FAMILY_LP_OPTIMIZER,
    SENSOR_FAMILY_BATTERY,
    SENSOR_FAMILY_CONTROLS,
    TESLA_SITE_INFO_CONTROL_MAX_AGE_SECONDS,
    TESLA_CAPABILITY_WAIT_SECONDS,
    POWERWALL_LOCAL_POLL_INTERVAL,
)

# Providers that use TOU schedule syncing (Amber, Octopus, Flow Power)
# GloBird and AEMO VPP use spike detection only — no TOU sync
PROVIDERS_WITH_TOU_SYNC = {"amber", "octopus", "flow_power"}

_LOGGER = logging.getLogger(__name__)


def _coerce_duration(value: Any, default: int = DEFAULT_DISCHARGE_DURATION) -> int:
    """Return a valid integer duration for manual force controls."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _selected_duration(entry: ConfigEntry, key: str) -> int:
    """Read the duration selected by the matching select entity."""
    return _coerce_duration(
        entry.options.get(key, entry.data.get(key, DEFAULT_DISCHARGE_DURATION))
    )


def _state_float(hass: HomeAssistant, entity_id: str) -> float | None:
    state = hass.states.get(entity_id)
    if state is None:
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


def _selected_force_power_w(hass: HomeAssistant) -> int:
    """Read the force-power selector and convert it to service power_w."""
    watts = _state_float(hass, "number.power_sync_force_power_w")
    if watts is not None and watts > 0:
        return int(round(watts))

    kw = _state_float(hass, "number.power_sync_force_power_kw")
    if kw is not None and kw > 0:
        return int(round(kw * 1000))

    return 0


def _parse_expiry(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _datetime_now_for(expires_at: datetime) -> datetime:
    return datetime.now(expires_at.tzinfo) if expires_at.tzinfo else datetime.now()


async def _reoptimize_if_enabled(coordinator: Any, changed: bool) -> None:
    """Refresh the LP plan after an optimizer setting switch changes."""
    if (
        changed
        and bool(getattr(coordinator, "enabled", False))
        and hasattr(coordinator, "force_reoptimize")
    ):
        await coordinator.force_reoptimize()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PowerSync switch entities."""
    # Detect Tesla by checking if tesla_energy_site_id is configured
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
    battery_system = entry.options.get(
        CONF_BATTERY_SYSTEM,
        entry.data.get(CONF_BATTERY_SYSTEM, BATTERY_SYSTEM_TESLA),
    )
    auto_sync_name = (
        "Auto-Sync TOU Schedule"
        if battery_system == BATTERY_SYSTEM_TESLA
        else "Auto-Sync Tariff Prices"
    )

    _LOGGER.info(
        "🔋 Switch setup: is_tesla=%s, battery_system=%s, provider=%s, has_tou_sync=%s",
        is_tesla,
        battery_system,
        electricity_provider,
        has_tou_sync,
    )

    entities = []

    entities.append(
        AutoUpdateSwitch(
            hass=hass,
            entry=entry,
            description=SwitchEntityDescription(
                key=SWITCH_TYPE_AUTO_UPDATE,
                name="Auto-Update PowerSync",
                icon="mdi:update",
            ),
        ),
    )

    entities.append(
        OptimizationEnabledSwitch(
            hass=hass,
            entry=entry,
            description=SwitchEntityDescription(
                key=SWITCH_TYPE_OPTIMIZATION_ENABLED,
                name="Enable Smart Optimization",
                icon="mdi:chart-timeline-variant-shimmer",
            ),
        ),
    )

    entities.append(
        AutoApplyOptimizerReserveSwitch(
            hass=hass,
            entry=entry,
            description=SwitchEntityDescription(
                key=SWITCH_TYPE_OPTIMIZATION_AUTO_APPLY_RESERVE,
                name="Auto-Apply Optimizer Reserve",
                icon="mdi:battery-sync-outline",
            ),
        ),
    )

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

    # Manual force controls are service wrappers and are available for every
    # battery system supported by power_sync.force_charge/force_discharge.
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

    # Only add auto-sync switch for providers that actually sync TOU schedules
    if has_tou_sync:
        entities.append(
            AutoSyncSwitch(
                hass=hass,
                entry=entry,
                description=SwitchEntityDescription(
                    key=SWITCH_TYPE_AUTO_SYNC,
                    name=auto_sync_name,
                    icon="mdi:sync",
                ),
            ),
        )

    # Add Tesla-specific switches only if Tesla is selected as battery system
    if is_tesla:
        _LOGGER.info("Tesla battery system detected - adding Tesla-specific switches")
        entities.extend([
            GridChargingSwitch(hass=hass, entry=entry),
        ])

    # Off-grid switch — available when Powerwall is paired for local control
    if is_tesla and entry.data.get(CONF_POWERWALL_LOCAL_PAIRED):
        entities.extend([
            OffGridSwitch(hass=hass, entry=entry),
            OnGridSwitch(hass=hass, entry=entry),
        ])

    # Away Mode and Profit Max switches — added later via deferred callbacks once
    # the OptimizationCoordinator is created (it's set up after platforms start).
    def _add_away_mode_switch(coordinator: Any) -> None:
        async_add_entities([AwayModeSwitch(hass=hass, entry=entry, coordinator=coordinator)])

    hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})[
        "switch_add_away_mode"
    ] = _add_away_mode_switch

    def _add_profit_max_switch(coordinator: Any) -> None:
        async_add_entities([ProfitMaxModeSwitch(hass=hass, entry=entry, coordinator=coordinator)])

    hass.data[DOMAIN][entry.entry_id]["switch_add_profit_max"] = _add_profit_max_switch

    if electricity_provider == "flow_power":
        def _add_disable_idle_switch(coordinator: Any) -> None:
            async_add_entities([
                DisableIdleModeSwitch(hass=hass, entry=entry, coordinator=coordinator)
            ])

        hass.data[DOMAIN][entry.entry_id]["switch_add_disable_idle"] = (
            _add_disable_idle_switch
        )

    if battery_system in TARGET_EXPORT_POWER_BATTERY_SYSTEMS:
        def _add_spread_export_switch(coordinator: Any) -> None:
            async_add_entities([
                SpreadExportSwitch(hass=hass, entry=entry, coordinator=coordinator)
            ])

        hass.data[DOMAIN][entry.entry_id]["switch_add_spread_export"] = _add_spread_export_switch

    if battery_system in TARGET_CHARGE_POWER_BATTERY_SYSTEMS:
        def _add_spread_import_switch(coordinator: Any) -> None:
            async_add_entities([
                SpreadImportSwitch(hass=hass, entry=entry, coordinator=coordinator)
            ])

        hass.data[DOMAIN][entry.entry_id]["switch_add_spread_import"] = _add_spread_import_switch

    async_add_entities(entities)

    # Capability-gated Tesla entities (storm watch, VPP program switches).
    # These cannot be added until the Tesla capability probe completes,
    # which runs ~after the first site_info fetch. We wait for that in a
    # background task and add them once.
    if is_tesla:
        async def _add_capability_gated_switches() -> None:
            entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
            waited = 0.0
            while "tesla_capabilities" not in entry_data and waited < TESLA_CAPABILITY_WAIT_SECONDS:
                await asyncio.sleep(2.0)
                waited += 2.0
                entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
            caps = entry_data.get("tesla_capabilities", {})
            if not caps:
                _LOGGER.info(
                    "Tesla capability probe did not complete within %.0fs — "
                    "skipping capability-gated switch creation",
                    TESLA_CAPABILITY_WAIT_SECONDS,
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
                    len(getattr(tesla_coord, "_vpp_programs_cache", None) or []) if tesla_coord else 0,
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
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_LP_OPTIMIZER)

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


class AutoUpdateSwitch(SwitchEntity):
    """Switch to enable/disable scheduled PowerSync HACS updates."""

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
            CONF_AUTO_UPDATE_ENABLED,
            entry.data.get(CONF_AUTO_UPDATE_ENABLED, False),
        )

    @property
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_CONTROLS)

    @property
    def is_on(self) -> bool:
        """Return True if scheduled auto-update is enabled."""
        return self._attr_is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn scheduled auto-update on."""
        _LOGGER.info("Enabling scheduled PowerSync auto-update")
        self._attr_is_on = True
        new_options = {**self._entry.options}
        new_options[CONF_AUTO_UPDATE_ENABLED] = True
        new_options.setdefault(CONF_AUTO_UPDATE_TIME, DEFAULT_AUTO_UPDATE_TIME)
        self.hass.config_entries.async_update_entry(
            self._entry,
            options=new_options,
        )
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn scheduled auto-update off."""
        _LOGGER.info("Disabling scheduled PowerSync auto-update")
        self._attr_is_on = False
        new_options = {**self._entry.options}
        new_options[CONF_AUTO_UPDATE_ENABLED] = False
        self.hass.config_entries.async_update_entry(
            self._entry,
            options=new_options,
        )
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        return {
            "scheduled_time": self._entry.options.get(
                CONF_AUTO_UPDATE_TIME,
                self._entry.data.get(CONF_AUTO_UPDATE_TIME, DEFAULT_AUTO_UPDATE_TIME),
            ),
            "last_run": entry_data.get("auto_update_last_run"),
            "last_result": entry_data.get("auto_update_last_result"),
            "last_update_entity": entry_data.get("auto_update_last_entity"),
            "last_check_at": entry_data.get("auto_update_last_check_at"),
            "last_check_decision": entry_data.get("auto_update_last_check_decision"),
        }


class OptimizationEnabledSwitch(SwitchEntity):
    """Switch to enable/disable the built-in Smart Optimization coordinator."""

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
        self._attr_is_on = self._current_enabled_state()

    def _current_enabled_state(self) -> bool:
        """Return whether Smart Optimization is configured and enabled."""
        provider = self._entry.options.get(
            CONF_OPTIMIZATION_PROVIDER,
            self._entry.data.get(CONF_OPTIMIZATION_PROVIDER),
        )
        default_enabled = provider == OPT_PROVIDER_POWERSYNC
        return bool(
            self._entry.options.get(
                CONF_OPTIMIZATION_ENABLED,
                self._entry.data.get(CONF_OPTIMIZATION_ENABLED, default_enabled),
            )
            and provider == OPT_PROVIDER_POWERSYNC
        )

    @property
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_CONTROLS)

    @property
    def is_on(self) -> bool:
        """Return True if Smart Optimization is enabled."""
        return self._attr_is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable Smart Optimization and select the built-in LP provider."""
        _LOGGER.info("Enabling Smart Optimization from HA switch")
        self._attr_is_on = True
        new_data = {**self._entry.data}
        new_options = {**self._entry.options}
        new_data[CONF_OPTIMIZATION_PROVIDER] = OPT_PROVIDER_POWERSYNC
        new_options[CONF_OPTIMIZATION_PROVIDER] = OPT_PROVIDER_POWERSYNC
        new_options[CONF_OPTIMIZATION_ENABLED] = True
        self.hass.config_entries.async_update_entry(
            self._entry,
            data=new_data,
            options=new_options,
        )
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable Smart Optimization while preserving saved LP settings."""
        _LOGGER.info("Disabling Smart Optimization from HA switch")
        self._attr_is_on = False
        new_options = {**self._entry.options}
        new_options[CONF_OPTIMIZATION_ENABLED] = False
        self.hass.config_entries.async_update_entry(
            self._entry,
            options=new_options,
        )
        self.async_write_ha_state()


class AutoApplyOptimizerReserveSwitch(SwitchEntity):
    """Switch to let forecast recommendations update the optimizer reserve floor."""

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
        self._attr_is_on = self._current_state()

    def _current_state(self) -> bool:
        return bool(
            self._entry.options.get(
                CONF_OPTIMIZATION_AUTO_APPLY_RESERVE,
                self._entry.data.get(CONF_OPTIMIZATION_AUTO_APPLY_RESERVE, False),
            )
        )

    def _coordinator(self) -> Any | None:
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        if isinstance(entry_data, dict):
            return entry_data.get("optimization_coordinator")
        return None

    async def async_added_to_hass(self) -> None:
        """Register for optimizer setting changes made outside this switch."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_{self._entry.entry_id}_auto_apply_reserve",
                self._handle_auto_apply_reserve_update,
            )
        )

    @callback
    def _handle_auto_apply_reserve_update(self, enabled: bool) -> None:
        """Update the HA switch state after API/config-flow changes."""
        self._attr_is_on = bool(enabled)
        self.async_write_ha_state()

    @property
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_LP_OPTIMIZER)

    @property
    def is_on(self) -> bool:
        """Return True if forecast reserve auto-apply is enabled."""
        coordinator = self._coordinator()
        if coordinator and hasattr(coordinator, "auto_apply_reserve_enabled"):
            return bool(coordinator.auto_apply_reserve_enabled)
        return self._attr_is_on

    async def _persist_without_coordinator(self, enabled: bool) -> None:
        new_data = {**self._entry.data}
        new_options = {**self._entry.options}
        current_reserve = new_options.get(
            CONF_OPTIMIZATION_BACKUP_RESERVE,
            new_data.get(CONF_OPTIMIZATION_BACKUP_RESERVE, 0.2),
        )
        try:
            current_reserve = float(current_reserve)
        except (TypeError, ValueError):
            current_reserve = 0.2
        if current_reserve > 1:
            current_reserve = current_reserve / 100.0

        manual_reserve = new_options.get(
            CONF_OPTIMIZATION_MANUAL_RESERVE,
            new_data.get(CONF_OPTIMIZATION_MANUAL_RESERVE),
        )
        try:
            manual_reserve = (
                float(manual_reserve)
                if manual_reserve is not None
                else current_reserve
            )
        except (TypeError, ValueError):
            manual_reserve = current_reserve
        if manual_reserve > 1:
            manual_reserve = manual_reserve / 100.0

        new_data[CONF_OPTIMIZATION_AUTO_APPLY_RESERVE] = bool(enabled)
        new_options[CONF_OPTIMIZATION_AUTO_APPLY_RESERVE] = bool(enabled)
        new_data[CONF_OPTIMIZATION_MANUAL_RESERVE] = manual_reserve
        new_options[CONF_OPTIMIZATION_MANUAL_RESERVE] = manual_reserve
        if not enabled:
            new_data[CONF_OPTIMIZATION_BACKUP_RESERVE] = manual_reserve
            new_options[CONF_OPTIMIZATION_BACKUP_RESERVE] = manual_reserve

        self.hass.config_entries.async_update_entry(
            self._entry,
            data=new_data,
            options=new_options,
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable forecast-driven optimizer reserve updates."""
        self._attr_is_on = True
        coordinator = self._coordinator()
        if coordinator and hasattr(coordinator, "set_auto_apply_reserve_enabled"):
            await coordinator.set_auto_apply_reserve_enabled(True)
        else:
            await self._persist_without_coordinator(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable forecast-driven optimizer reserve updates and restore manual floor."""
        self._attr_is_on = False
        coordinator = self._coordinator()
        if coordinator and hasattr(coordinator, "set_auto_apply_reserve_enabled"):
            await coordinator.set_auto_apply_reserve_enabled(False)
        else:
            await self._persist_without_coordinator(False)
        self.async_write_ha_state()


class ForceDischargeSwitch(SwitchEntity):
    """Switch to manually force battery discharge mode."""

    _attr_has_entity_name = True
    # Primary user control — belongs in the device card's Controls section.

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
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_BATTERY)

    @property
    def is_on(self) -> bool:
        """Return True if force discharge is active."""
        return self._attr_is_on

    async def async_added_to_hass(self) -> None:
        """Keep switch state in sync with service/dashboard-triggered discharge."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_force_discharge_state",
                self._handle_force_discharge_update,
            )
        )

    @callback
    def _handle_force_discharge_update(self, state: dict[str, Any]) -> None:
        """Update switch state when force discharge is changed elsewhere."""
        active = bool(state.get("active")) if isinstance(state, dict) else False
        self._attr_is_on = active
        if isinstance(state, dict) and state.get("duration") is not None:
            self._duration_minutes = _coerce_duration(
                state.get("duration"), self._duration_minutes
            )
        self._discharge_expires_at = None
        if active and isinstance(state, dict):
            self._discharge_expires_at = _parse_expiry(state.get("expires_at"))
        if active and self._discharge_expires_at:
            self._schedule_expiry_check()
        elif self._cancel_expiry_timer:
            self._cancel_expiry_timer()
            self._cancel_expiry_timer = None
        self.async_write_ha_state()

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
        selected_duration = _selected_duration(
            self._entry,
            CONF_FORCE_DISCHARGE_DURATION,
        )
        duration = _coerce_duration(
            kwargs.get("duration", selected_duration), self._duration_minutes,
        )
        service_data = {"duration": duration}
        power_w = _selected_force_power_w(self.hass)
        if power_w > 0:
            service_data["power_w"] = power_w

        # Call the force discharge service
        try:
            await self.hass.services.async_call(
                DOMAIN,
                "force_discharge",
                service_data,
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
                and _datetime_now_for(self._discharge_expires_at) >= self._discharge_expires_at
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
            remaining = self._discharge_expires_at - _datetime_now_for(
                self._discharge_expires_at
            )
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
    # Primary user control — belongs in the device card's Controls section.

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
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_BATTERY)

    @property
    def is_on(self) -> bool:
        """Return True if force charge is active."""
        return self._attr_is_on

    async def async_added_to_hass(self) -> None:
        """Keep switch state in sync with service/dashboard-triggered charge."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_force_charge_state",
                self._handle_force_charge_update,
            )
        )

    @callback
    def _handle_force_charge_update(self, state: dict[str, Any]) -> None:
        """Update switch state when force charge is changed elsewhere."""
        active = bool(state.get("active")) if isinstance(state, dict) else False
        self._attr_is_on = active
        if isinstance(state, dict) and state.get("duration") is not None:
            self._duration_minutes = _coerce_duration(
                state.get("duration"), self._duration_minutes
            )
        self._charge_expires_at = None
        if active and isinstance(state, dict):
            self._charge_expires_at = _parse_expiry(state.get("expires_at"))
        if active and self._charge_expires_at:
            self._schedule_expiry_check()
        elif self._cancel_expiry_timer:
            self._cancel_expiry_timer()
            self._cancel_expiry_timer = None
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on force charge mode."""
        _LOGGER.info("Activating force charge mode for %d minutes", self._duration_minutes)

        # Get the duration from service call data if provided
        selected_duration = _selected_duration(
            self._entry,
            CONF_FORCE_CHARGE_DURATION,
        )
        duration = _coerce_duration(
            kwargs.get("duration", selected_duration), self._duration_minutes,
        )
        service_data = {"duration": duration}
        power_w = _selected_force_power_w(self.hass)
        if power_w > 0:
            service_data["power_w"] = power_w

        # Call the force charge service
        try:
            await self.hass.services.async_call(
                DOMAIN,
                "force_charge",
                service_data,
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
            if (
                self._charge_expires_at
                and _datetime_now_for(self._charge_expires_at) >= self._charge_expires_at
            ):
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
            remaining = self._charge_expires_at - _datetime_now_for(
                self._charge_expires_at
            )
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

        self._attr_is_on = self._current_value()

    def _current_value(self) -> bool:
        """Read the current config-entry option instead of a startup cache."""
        return bool(
            self._entry.options.get(
                CONF_MONITORING_MODE,
                self._entry.data.get(CONF_MONITORING_MODE, False),
            )
        )

    @property
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_CONTROLS)

    @property
    def is_on(self) -> bool:
        """Return True if monitoring mode is active."""
        return self._current_value()

    async def async_added_to_hass(self) -> None:
        """Refresh state when monitoring mode is changed through the API/app."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_{self._entry.entry_id}_monitoring_mode",
                self._handle_monitoring_mode_update,
            )
        )

    @callback
    def _handle_monitoring_mode_update(self, enabled: bool) -> None:
        """Update the HA state machine after API-driven changes."""
        self._attr_is_on = bool(enabled)
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable monitoring mode — all control commands will be logged but not executed."""
        _LOGGER.info("Monitoring mode ENABLED — all battery/inverter commands will be blocked")
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
        _LOGGER.info("Monitoring mode DISABLED — normal battery/inverter control resumed")
        self._attr_is_on = False

        new_options = {**self._entry.options}
        new_options[CONF_MONITORING_MODE] = False
        self.hass.config_entries.async_update_entry(
            self._entry,
            options=new_options,
        )

        self.async_write_ha_state()


class AwayModeSwitch(SwitchEntity):
    """Switch to activate away mode — makes the load forecaster use pre-vacation history."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator: Any) -> None:
        """Initialize the switch."""
        self.hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_{SWITCH_TYPE_AWAY_MODE}"
        self._attr_suggested_object_id = f"power_sync_{SWITCH_TYPE_AWAY_MODE}"
        self._attr_name = "Away Mode"
        self._attr_icon = "mdi:home-export-outline"
        # Restore from persisted config entry options — switch is ON when
        # away_enabled_at is set and away_disabled_at is not.
        from .const import CONF_AWAY_ENABLED_AT, CONF_AWAY_DISABLED_AT
        en = entry.options.get(CONF_AWAY_ENABLED_AT) or entry.data.get(CONF_AWAY_ENABLED_AT)
        dis = entry.options.get(CONF_AWAY_DISABLED_AT) or entry.data.get(CONF_AWAY_DISABLED_AT)
        self._attr_is_on = bool(en and not dis)

    @property
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_CONTROLS)

    @property
    def is_on(self) -> bool:
        """Return True if away mode is active."""
        return self._attr_is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable away mode."""
        _LOGGER.info("Away mode ENABLED — load forecaster will use pre-vacation history")
        self._attr_is_on = True
        self._coordinator.set_away_mode(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable away mode."""
        _LOGGER.info("Away mode DISABLED — load forecaster using recent history")
        self._attr_is_on = False
        self._coordinator.set_away_mode(False)
        self.async_write_ha_state()


class ProfitMaxModeSwitch(SwitchEntity):
    """Switch to activate profit maximisation mode — drives the LP to export more aggressively."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator: Any) -> None:
        """Initialize the switch."""
        self.hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_{SWITCH_TYPE_PROFIT_MAX_MODE}"
        self._attr_suggested_object_id = f"power_sync_{SWITCH_TYPE_PROFIT_MAX_MODE}"
        self._attr_name = "Profit Maximisation Mode"
        self._attr_icon = "mdi:cash-plus"
        from .const import CONF_PROFIT_MAX_ENABLED
        enabled = entry.options.get(
            CONF_PROFIT_MAX_ENABLED,
            entry.data.get(CONF_PROFIT_MAX_ENABLED, False),
        )
        self._attr_is_on = bool(enabled)

    async def async_added_to_hass(self) -> None:
        """Register for optimizer setting changes made outside this switch."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_{self._entry.entry_id}_profit_max_mode",
                self._handle_profit_max_update,
            )
        )

    @callback
    def _handle_profit_max_update(self, enabled: bool) -> None:
        """Update the HA switch state after API-driven changes."""
        self._attr_is_on = bool(enabled)
        self.async_write_ha_state()

    @property
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_LP_OPTIMIZER)

    @property
    def is_on(self) -> bool:
        """Return True if profit maximisation mode is active."""
        return self._coordinator.profit_max_mode

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable profit maximisation mode."""
        self._attr_is_on = True
        changed = self._coordinator.set_profit_max_mode(True)
        await _reoptimize_if_enabled(self._coordinator, changed)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable profit maximisation mode."""
        self._attr_is_on = False
        changed = self._coordinator.set_profit_max_mode(False)
        await _reoptimize_if_enabled(self._coordinator, changed)
        self.async_write_ha_state()


class DisableIdleModeSwitch(SwitchEntity):
    """Switch to replace Flow Power optimizer idle holds with self-consumption."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator: Any) -> None:
        """Initialize the switch."""
        self.hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_{SWITCH_TYPE_OPTIMIZATION_DISABLE_IDLE}"
        self._attr_suggested_object_id = (
            f"power_sync_{SWITCH_TYPE_OPTIMIZATION_DISABLE_IDLE}"
        )
        self._attr_name = "No Idle Mode"
        self._attr_icon = "mdi:sleep-off"
        enabled = entry.options.get(
            CONF_OPTIMIZATION_DISABLE_IDLE,
            entry.data.get(CONF_OPTIMIZATION_DISABLE_IDLE, False),
        )
        self._attr_is_on = bool(enabled)

    async def async_added_to_hass(self) -> None:
        """Register for optimizer setting changes made outside this switch."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_{self._entry.entry_id}_disable_idle",
                self._handle_disable_idle_update,
            )
        )

    @callback
    def _handle_disable_idle_update(self, enabled: bool) -> None:
        """Update the HA switch state after API-driven changes."""
        self._attr_is_on = bool(enabled)
        self.async_write_ha_state()

    @property
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_LP_OPTIMIZER)

    @property
    def is_on(self) -> bool:
        """Return True if Flow Power no-idle mode is active."""
        return self._coordinator.disable_idle_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable Flow Power no-idle mode."""
        self._attr_is_on = True
        changed = self._coordinator.set_disable_idle_enabled(True)
        await _reoptimize_if_enabled(self._coordinator, changed)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable Flow Power no-idle mode."""
        self._attr_is_on = False
        changed = self._coordinator.set_disable_idle_enabled(False)
        await _reoptimize_if_enabled(self._coordinator, changed)
        self.async_write_ha_state()


class SpreadExportSwitch(SwitchEntity):
    """Switch to spread optimizer export across the full eligible window."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator: Any) -> None:
        """Initialize the switch."""
        self.hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_{SWITCH_TYPE_OPTIMIZATION_SPREAD_EXPORT}"
        self._attr_suggested_object_id = f"power_sync_{SWITCH_TYPE_OPTIMIZATION_SPREAD_EXPORT}"
        self._attr_name = "Spread Export Across Window"
        self._attr_icon = "mdi:timeline-clock-outline"
        enabled = entry.options.get(
            CONF_OPTIMIZATION_SPREAD_EXPORT_ENABLED,
            entry.data.get(CONF_OPTIMIZATION_SPREAD_EXPORT_ENABLED, False),
        )
        self._attr_is_on = bool(enabled)

    async def async_added_to_hass(self) -> None:
        """Register for optimizer setting changes made outside this switch."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_{self._entry.entry_id}_spread_export",
                self._handle_spread_export_update,
            )
        )

    @callback
    def _handle_spread_export_update(self, enabled: bool) -> None:
        """Update the HA switch state after API-driven changes."""
        self._attr_is_on = bool(enabled)
        self.async_write_ha_state()

    @property
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_LP_OPTIMIZER)

    @property
    def is_on(self) -> bool:
        """Return True if spread export mode is active."""
        return self._coordinator.spread_export_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable spread export mode."""
        self._attr_is_on = True
        changed = self._coordinator.set_spread_export_enabled(True)
        await _reoptimize_if_enabled(self._coordinator, changed)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable spread export mode."""
        self._attr_is_on = False
        changed = self._coordinator.set_spread_export_enabled(False)
        await _reoptimize_if_enabled(self._coordinator, changed)
        self.async_write_ha_state()


class SpreadImportSwitch(SwitchEntity):
    """Switch to spread optimizer import charge across same-price windows."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator: Any) -> None:
        """Initialize the switch."""
        self.hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_{SWITCH_TYPE_OPTIMIZATION_SPREAD_IMPORT}"
        self._attr_suggested_object_id = f"power_sync_{SWITCH_TYPE_OPTIMIZATION_SPREAD_IMPORT}"
        self._attr_name = "Spread Import Across Window"
        self._attr_icon = "mdi:timeline-clock"
        enabled = entry.options.get(
            CONF_OPTIMIZATION_SPREAD_IMPORT_ENABLED,
            entry.data.get(CONF_OPTIMIZATION_SPREAD_IMPORT_ENABLED, False),
        )
        self._attr_is_on = bool(enabled)

    async def async_added_to_hass(self) -> None:
        """Register for optimizer setting changes made outside this switch."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_{self._entry.entry_id}_spread_import",
                self._handle_spread_import_update,
            )
        )

    @callback
    def _handle_spread_import_update(self, enabled: bool) -> None:
        """Update the HA switch state after API-driven changes."""
        self._attr_is_on = bool(enabled)
        self.async_write_ha_state()

    @property
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_LP_OPTIMIZER)

    @property
    def is_on(self) -> bool:
        """Return True if spread import mode is active."""
        return self._coordinator.spread_import_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable spread import mode."""
        self._attr_is_on = True
        changed = self._coordinator.set_spread_import_enabled(True)
        await _reoptimize_if_enabled(self._coordinator, changed)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable spread import mode."""
        self._attr_is_on = False
        changed = self._coordinator.set_spread_import_enabled(False)
        await _reoptimize_if_enabled(self._coordinator, changed)
        self.async_write_ha_state()


class _TeslaSiteSwitchBase(SwitchEntity):
    """Base for Tesla Energy Site switches that call coordinator methods."""

    _attr_has_entity_name = True
    _attr_should_poll = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, key: str, name: str, icon: str) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_suggested_object_id = f"power_sync_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_is_on: bool | None = None

    @property
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_BATTERY)

    def _tesla_coord(self):
        return self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {}).get("tesla_coordinator")

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
                "Could not refresh Tesla site_info for switch entity",
                exc_info=True,
            )


class GridChargingSwitch(_TeslaSiteSwitchBase):
    """Toggle whether the Powerwall may charge from grid (TOU arbitrage)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass, entry,
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
            {"enabled": True, "source": "user"},
            blocking=False,
        )
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.hass.services.async_call(
            DOMAIN,
            "set_grid_charging",
            {"enabled": False, "source": "user"},
            blocking=False,
        )
        self._attr_is_on = False
        self.async_write_ha_state()


class StormWatchSwitch(_TeslaSiteSwitchBase):
    """Toggle Tesla Storm Watch (predictive pre-charging before severe weather)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass, entry,
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
            {"enabled": True, "source": "user"},
            blocking=False,
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.hass.services.async_call(
            DOMAIN,
            "set_storm_watch",
            {"enabled": False, "source": "user"},
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
            hass, entry,
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
            if str(p.get("id") or p.get("program_id") or p.get("name")) == self._program_id:
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
            DOMAIN, "set_vpp_enrollment",
            {"program_id": self._program_id, "enrolled": True, "source": "user"},
            blocking=False,
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.hass.services.async_call(
            DOMAIN, "set_vpp_enrollment",
            {"program_id": self._program_id, "enrolled": False, "source": "user"},
            blocking=False,
        )


class _PowerwallGridModeSwitch(SwitchEntity):
    """Mutually-exclusive Powerwall grid connection mode switch."""

    _attr_has_entity_name = True
    _PENDING_SECONDS = 120

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        key: str,
        name: str,
        icon: str,
        mode_is_off_grid: bool,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_suggested_object_id = f"power_sync_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._mode_is_off_grid = mode_is_off_grid

    @property
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_BATTERY)

    def _entry_data(self) -> dict[str, Any]:
        return self.hass.data.setdefault(DOMAIN, {}).setdefault(self._entry.entry_id, {})

    def _current_is_off_grid(self) -> bool | None:
        """Return actual islanding state from local data, falling back to cloud."""
        runtime = self._entry_data().get("powerwall_local") or {}
        coord = runtime.get("coordinator")
        if coord is not None:
            snap = coord.data
            if snap is not None and snap.grid_status is not None:
                return "island" in snap.grid_status.lower()

        # Fall back to cloud grid_status sensor
        state = self.hass.states.get("sensor.power_sync_grid_status")
        if state is not None and state.state not in (None, "unknown", "unavailable"):
            return state.state.lower() != "active"

        return None

    def _pending_state(self) -> tuple[bool | None, datetime | None]:
        pending = self._entry_data().get("powerwall_grid_mode_pending") or {}
        is_off_grid = pending.get("is_off_grid")
        expires_at = pending.get("expires_at")
        if isinstance(is_off_grid, bool) and isinstance(expires_at, datetime):
            return is_off_grid, expires_at
        return None, None

    def _set_pending_state(self, off_grid: bool) -> None:
        self._entry_data()["powerwall_grid_mode_pending"] = {
            "is_off_grid": off_grid,
            "expires_at": datetime.now() + timedelta(seconds=self._PENDING_SECONDS),
        }

    def _clear_pending_state(self) -> None:
        self._entry_data().pop("powerwall_grid_mode_pending", None)

    def _effective_is_off_grid(self) -> bool | None:
        """Return actual mode, with a short optimistic window after commands."""
        actual = self._current_is_off_grid()
        pending_is_off_grid, pending_expires_at = self._pending_state()
        if pending_is_off_grid is None:
            return actual

        if actual is not None and actual == pending_is_off_grid:
            self._clear_pending_state()
            return actual

        if pending_expires_at and datetime.now() < pending_expires_at:
            return pending_is_off_grid

        self._clear_pending_state()
        return actual

    @property
    def is_on(self) -> bool | None:
        """Return True when this mode is active."""
        is_off_grid = self._effective_is_off_grid()
        if is_off_grid is None:
            return None
        return is_off_grid == self._mode_is_off_grid

    async def _set_grid_mode(self, off_grid: bool) -> None:
        service = "powerwall_go_off_grid" if off_grid else "powerwall_reconnect_grid"
        mode = "off-grid" if off_grid else "on-grid"
        _LOGGER.info("Powerwall grid mode switch: setting %s", mode)
        await self.hass.services.async_call(
            DOMAIN,
            service,
            {},
            blocking=True,
        )
        self._set_pending_state(off_grid)
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Activate this grid mode."""
        await self._set_grid_mode(self._mode_is_off_grid)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Activate the opposite grid mode."""
        await self._set_grid_mode(not self._mode_is_off_grid)

    async def async_added_to_hass(self) -> None:
        """Keep the mutually-exclusive mode switches fresh during transitions."""
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._handle_state_tick,
                timedelta(seconds=POWERWALL_LOCAL_POLL_INTERVAL),
            )
        )

    @callback
    def _handle_state_tick(self, now: datetime) -> None:
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return command-transition details."""
        actual = self._current_is_off_grid()
        pending_is_off_grid, pending_expires_at = self._pending_state()
        attrs: dict[str, Any] = {}
        if actual is not None:
            attrs["actual_grid_mode"] = "off_grid" if actual else "on_grid"
        if pending_is_off_grid is not None:
            attrs["pending_grid_mode"] = (
                "off_grid" if pending_is_off_grid else "on_grid"
            )
            if pending_expires_at is not None:
                attrs["pending_expires_at"] = pending_expires_at.isoformat()
        return attrs


class OffGridSwitch(_PowerwallGridModeSwitch):
    """Switch that is on while the Powerwall is intentionally islanded."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            entry,
            key="off_grid",
            name="Off-Grid",
            icon="mdi:transmission-tower-off",
            mode_is_off_grid=True,
        )


class OnGridSwitch(_PowerwallGridModeSwitch):
    """Switch that is on while the Powerwall is connected to grid."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            entry,
            key="on_grid",
            name="On-Grid",
            icon="mdi:transmission-tower",
            mode_is_off_grid=False,
        )
