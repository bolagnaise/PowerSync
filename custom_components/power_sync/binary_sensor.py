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

from .const import (
    DOMAIN,
    CONF_TESLA_ENERGY_SITE_ID,
    CONF_POWERWALL_LOCAL_PAIRED,
    family_device_info,
    powerwall_device_info,
    SENSOR_FAMILY_BATTERY,
    SENSOR_FAMILY_CONTROLS,
    SENSOR_FAMILY_GRID_HOME,
    TESLA_CAPABILITY_WAIT_SECONDS,
)

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

    # Local pairing state — always exposed so the dashboard can gate the
    # Local Control section on it, and the mobile app can subscribe.
    async_add_entities([PowerwallLocalPairedBinarySensor(hass, entry)])
    async_add_entities([PowerwallLocalIslandedBinarySensor(hass, entry)])
    # Surface "paired but no LAN IP" as a diagnostic — mobile app uses this
    # to show a banner directing the user to set the gateway IP, since
    # without it the local-only features (snapshot, curtailment, fast writes)
    # silently won't work even though pairing succeeded.
    async_add_entities([PowerwallLocalIPMissingBinarySensor(hass, entry)])

    # Critical Alert sensor only makes sense once the gateway is paired
    # (its data source is the local TEDAPI snapshot).
    if entry.data.get(CONF_POWERWALL_LOCAL_PAIRED):
        async_add_entities([PowerwallCriticalAlertBinarySensor(hass, entry)])

    # Universally-available Tesla site sensors (no capability probe needed).
    async_add_entities([
        GridServicesActiveBinarySensor(hass, entry),
        CalibrationActiveBinarySensor(hass, entry),
        PermissionToOperateBinarySensor(hass, entry),
    ])

    async def _add_capability_gated_binary_sensors() -> None:
        entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        waited = 0.0
        while "tesla_capabilities" not in entry_data and waited < TESLA_CAPABILITY_WAIT_SECONDS:
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
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_BATTERY)

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
            hass, entry,
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
            hass, entry,
            key="tesla_manual_export_override",
            name="Manual Export Override",
            icon="mdi:hand-back-right",
        )

    @property
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_CONTROLS)

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


class PowerwallCriticalAlertBinarySensor(_TeslaBinarySensorBase):
    """True when at least one Powerwall alert is active.

    Reads the local TEDAPI snapshot's ``alerts`` list. Severity strings vary
    by firmware (``warning`` / ``critical`` / ``error``); we treat any active
    entry as a problem rather than guessing the severity taxonomy.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass, entry,
            key="pw_critical_alert",
            name="Powerwall Alert Active",
            icon="mdi:alert-octagon",
        )

    @property
    def device_info(self):
        return powerwall_device_info(self._entry.entry_id)

    @property
    def is_on(self) -> bool | None:
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        runtime = entry_data.get("powerwall_local") or {}
        coord = runtime.get("coordinator")
        if coord is None:
            return None
        snap = coord.data
        if snap is None or snap.alerts is None:
            return None
        return len(snap.alerts) > 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        runtime = entry_data.get("powerwall_local") or {}
        coord = runtime.get("coordinator")
        snap = getattr(coord, "data", None)
        if snap is None or not snap.alerts:
            return {}
        return {
            "alerts": [
                a.get("name") or a.get("alert_name") or "Unknown" for a in snap.alerts
            ],
        }


class PowerwallLocalPairedBinarySensor(_TeslaBinarySensorBase):
    """True when the Powerwall gateway has a verified local-control key.

    Driven purely from ``entry.data`` so the state is available immediately
    on entry load (no polling required). The strategy dashboard uses this
    entity to gate the Local Control section.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass, entry,
            key="powerwall_local_paired",
            name="Powerwall Local Paired",
            icon="mdi:key-variant",
        )

    @property
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_GRID_HOME)

    @property
    def is_on(self) -> bool | None:
        return bool(self._entry.data.get(CONF_POWERWALL_LOCAL_PAIRED, False))


class GridServicesActiveBinarySensor(_TeslaBinarySensorBase):
    """True while Tesla is dispatching the Powerwall for VPP / grid services."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass, entry,
            key="tesla_grid_services_active",
            name="Grid Services Active",
            icon="mdi:transmission-tower-export",
        )

    @property
    def device_info(self):
        return powerwall_device_info(self._entry.entry_id)

    @property
    def is_on(self) -> bool | None:
        coord = self._tesla_coord()
        if coord is None or coord.data is None:
            return None
        return bool(coord.data.get("grid_services_active", False))


class CalibrationActiveBinarySensor(_TeslaBinarySensorBase):
    """True when PowerSync has detected a Powerwall calibration cycle.

    The optimiser flips ``calibration_suspected`` after repeated mode-toggle
    failures (Powerwall ignoring commands while it self-calibrates). Surfacing
    this lets the dashboard show why automatic control is paused.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass, entry,
            key="tesla_calibration_active",
            name="Calibration Active",
            icon="mdi:battery-sync",
        )

    @property
    def device_info(self):
        return powerwall_device_info(self._entry.entry_id)

    @property
    def is_on(self) -> bool | None:
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        return bool(entry_data.get("calibration_suspected", False))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        detected_at = entry_data.get("calibration_detected_at")
        return {
            "detected_at": detected_at,
        }


class PermissionToOperateBinarySensor(_TeslaBinarySensorBase):
    """True when the Powerwall is commissioned for grid export by the utility."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass, entry,
            key="tesla_permission_to_operate",
            name="Permission to Operate",
            icon="mdi:check-decagram",
        )

    @property
    def device_info(self):
        return powerwall_device_info(self._entry.entry_id)

    @property
    def is_on(self) -> bool | None:
        coord = self._tesla_coord()
        if coord is None:
            return None
        site_info = getattr(coord, "_site_info_cache", None) or {}
        # Tesla exposes the commissioning state under several keys depending
        # on region / firmware. Check the common ones; default to None so the
        # entity reads "unknown" rather than misleadingly "not commissioned".
        for key in ("permission_to_export", "permission_to_operate", "pto"):
            if key in site_info:
                return bool(site_info[key])
            components = site_info.get("components") or {}
            if key in components:
                return bool(components[key])
        return None


class PowerwallLocalIPMissingBinarySensor(_TeslaBinarySensorBase):
    """True when the entry is paired but no local gateway IP is configured.

    This signals "you finished cloud-side pairing but local-only features
    (per-PW snapshot, automated curtailment, fast operation-mode toggles)
    won't work until you set ``CONF_POWERWALL_LOCAL_IP``". Off-grid still
    works in this state because it goes through the cloud signed
    ``device_command`` path.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass, entry,
            key="powerwall_local_ip_missing",
            name="Powerwall Gateway IP Missing",
            icon="mdi:lan-disconnect",
        )

    @property
    def is_on(self) -> bool | None:
        from .const import CONF_POWERWALL_LOCAL_IP
        from .powerwall_host import normalize_powerwall_gateway_host

        paired = bool(self._entry.data.get(CONF_POWERWALL_LOCAL_PAIRED, False))
        if not paired:
            # Not paired at all — this banner doesn't apply. Returning False
            # (not None) so it shows as "OK" rather than "unknown" in the
            # device's diagnostic panel.
            return False
        try:
            host = normalize_powerwall_gateway_host(
                self._entry.data.get(CONF_POWERWALL_LOCAL_IP)
            )
        except ValueError:
            host = ""
        return not host


class PowerwallLocalIslandedBinarySensor(_TeslaBinarySensorBase):
    """True when the Powerwall reports it is running off-grid (islanded).

    Reads the latest snapshot from ``PowerwallLocalCoordinator``. None when
    the coordinator hasn't produced a sample yet (eg before first refresh or
    when the gateway is unreachable).
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass, entry,
            key="powerwall_local_islanded",
            name="Powerwall Off-Grid",
            icon="mdi:transmission-tower-off",
        )

    @property
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_GRID_HOME)

    @property
    def is_on(self) -> bool | None:
        # Try local coordinator snapshot first
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        runtime = entry_data.get("powerwall_local") or {}
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
