"""Number platform for PowerSync integration — Tesla Energy Site controls."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any

from homeassistant.components.number import (
    NumberEntity,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, PERCENTAGE, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    CONF_TESLA_ENERGY_SITE_ID,
    CONF_POWERWALL_LOCAL_PAIRED,
    CONF_FOXESS_HOST,
    CONF_FOXESS_SERIAL_PORT,
    CONF_FOXESS_CLOUD_API_KEY,
    CONF_GOODWE_HOST,
    CONF_SIGENERGY_STATION_ID,
    CONF_SUNGROW_HOST,
    CONF_ALPHAESS_MODBUS_HOST,
    CONF_ESY_CONFIG_ENTRY_ID,
    CONF_SOLAX_CONFIG_ENTRY_ID,
    CONF_SOLAX_ENTITY_PREFIX,
    CONF_SAJ_CONFIG_ENTRY_ID,
    CONF_NEOVOLT_CONFIG_ENTRY_ID,
    CONF_NEOVOLT_CONFIG_ENTRY_IDS,
    CONF_FRONIUS_RESERVA_MAX_CHARGE_KW,
    CONF_FRONIUS_RESERVA_MAX_DISCHARGE_KW,
    CONF_NEOVOLT_MAX_CHARGE_KW,
    CONF_NEOVOLT_MAX_DISCHARGE_KW,
    CONF_OPTIMIZATION_MAX_CHARGE_W,
    CONF_OPTIMIZATION_MAX_DISCHARGE_W,
    CONF_SAJ_INVERTER_RATED_KW,
    CONF_SOLAX_BATTERY_NOMINAL_V,
    CONF_SOLAX_MAX_CHARGE_CURRENT_A,
    CONF_SOLAX_MAX_DISCHARGE_CURRENT_A,
    family_device_info,
    SENSOR_FAMILY_BATTERY,
    SENSOR_FAMILY_EV_CHARGING,
    TESLA_SITE_INFO_CONTROL_MAX_AGE_SECONDS,
    TESLA_CAPABILITY_WAIT_SECONDS,
    TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

from .force_discharge_duration import (
    CUSTOM_DISCHARGE_DURATION_KEY, MAX_FORCE_DISCHARGE_MINUTES,
    validate_discharge_duration,
)

FORCE_POWER_FALLBACK_MAX_KW = 50.0
FORCE_POWER_STEP_KW = 0.05
FORCE_POWER_COORDINATOR_KEYS = (
    "foxess_coordinator",
    "goodwe_coordinator",
    "sigenergy_coordinator",
    "sungrow_coordinator",
    "alphaess_coordinator",
    "esy_sunhome_coordinator",
    "solax_coordinator",
    "saj_h2_coordinator",
    "fronius_reserva_coordinator",
    "neovolt_coordinator",
    "anker_solix_coordinator",
)


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
    last_success_monotonic = getattr(coordinator, "last_success_monotonic", None)
    if data is None or last_success_monotonic is None:
        return None
    if time.monotonic() - last_success_monotonic > TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS:
        return None
    return data


def _positive_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _entry_value(entry: ConfigEntry, key: str) -> Any:
    if key in entry.options and entry.options.get(key) is not None:
        return entry.options.get(key)
    return entry.data.get(key)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PowerSync number entities."""
    async_add_entities([ForceDischargeCustomDurationNumber(hass, entry)])
    tesla_site_id = entry.options.get(
        CONF_TESLA_ENERGY_SITE_ID,
        entry.data.get(CONF_TESLA_ENERGY_SITE_ID, ""),
    )

    if tesla_site_id:
        # Backup reserve is universally supported for any Tesla energy site.
        async_add_entities([BackupReserveNumber(hass, entry)])

    async def _add_capability_gated_numbers() -> None:
        entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        waited = 0.0
        while "tesla_capabilities" not in entry_data and waited < TESLA_CAPABILITY_WAIT_SECONDS:
            await asyncio.sleep(2.0)
            waited += 2.0
            entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        caps = entry_data.get("tesla_capabilities", {})
        if caps.get("off_grid_vehicle_charging_reserve"):
            async_add_entities([OffGridEvReserveNumber(hass, entry)])

    if tesla_site_id:
        hass.async_create_task(
            _add_capability_gated_numbers(),
            name=f"{DOMAIN}_capability_gated_numbers",
        )

    # Force power slider — for battery systems whose force charge/discharge
    # service path accepts a power_w parameter. The entity stores the user's
    # preferred force power level in kW and service callers convert it to W.
    _supports_force_power = any(
        entry.data.get(k) or entry.options.get(k)
        for k in (
            CONF_FOXESS_HOST,
            CONF_FOXESS_SERIAL_PORT,
            CONF_FOXESS_CLOUD_API_KEY,
            CONF_GOODWE_HOST,
            CONF_SIGENERGY_STATION_ID,
            CONF_SUNGROW_HOST,
            CONF_ALPHAESS_MODBUS_HOST,
            CONF_ESY_CONFIG_ENTRY_ID,
            CONF_SOLAX_CONFIG_ENTRY_ID,
            CONF_SOLAX_ENTITY_PREFIX,
            CONF_SAJ_CONFIG_ENTRY_ID,
            CONF_NEOVOLT_CONFIG_ENTRY_ID,
            CONF_NEOVOLT_CONFIG_ENTRY_IDS,
        )
    )
    from .powerwall_host import local_battery_dispatch_configured
    _supports_force_power = _supports_force_power or (
        bool(tesla_site_id) and local_battery_dispatch_configured(entry.data)
    )
    if _supports_force_power:
        async_add_entities([ForcePowerNumber(hass, entry)])


class ForceDischargeCustomDurationNumber(NumberEntity):
    """Persist the whole-minute value used when Discharge Duration is Custom."""

    _attr_has_entity_name = True
    _attr_name = "Force Discharge Custom Duration"
    _attr_icon = "mdi:timer-edit-outline"
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 1
    _attr_native_max_value = MAX_FORCE_DISCHARGE_MINUTES
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "min"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry_id = entry.entry_id
        self._attr_unique_id = f"{entry.entry_id}_{CUSTOM_DISCHARGE_DURATION_KEY}"
        self._attr_suggested_object_id = "power_sync_force_discharge_custom_duration"

    @property
    def device_info(self):
        return family_device_info(self._entry_id, SENSOR_FAMILY_BATTERY)

    @property
    def native_value(self) -> int | None:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        try:
            return validate_discharge_duration(entry.options.get(CUSTOM_DISCHARGE_DURATION_KEY, 30)) if entry else 30
        except ValueError:
            return None

    async def async_set_native_value(self, value: float) -> None:
        minutes = validate_discharge_duration(value)
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return
        if entry.options.get(CUSTOM_DISCHARGE_DURATION_KEY) == minutes:
            self.async_write_ha_state()
            return
        options = {**entry.options, CUSTOM_DISCHARGE_DURATION_KEY: minutes}
        self.hass.data.setdefault(DOMAIN, {}).setdefault(self._entry_id, {})["_skip_reload"] = True
        self.hass.config_entries.async_update_entry(entry, options=options)
        self.async_write_ha_state()


class _TeslaSiteNumberBase(NumberEntity):
    _attr_has_entity_name = True
    _attr_should_poll = True
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE

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
        # No EntityCategory — these are user-facing controls (Backup Reserve etc),
        # belong in the device card's main Controls section, not Configuration.

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
                "Could not refresh Tesla site_info for number entity",
                exc_info=True,
            )


class BackupReserveNumber(_TeslaSiteNumberBase):
    """Backup reserve % for Tesla Powerwall."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass, entry,
            key="tesla_backup_reserve",
            name="Backup Reserve",
            icon="mdi:battery-lock",
        )

    @property
    def native_value(self) -> float | None:
        # Local raw reserve has no authoritative user-scale conversion.
        coord = self._tesla_coord()
        site_info = getattr(coord, "_site_info_cache", None) if coord else None
        if site_info and "backup_reserve_percent" in site_info:
            reserve = site_info["backup_reserve_percent"]
            if reserve is None:
                return None
            return float(reserve)
        stored = self._entry.options.get("_user_backup_reserve")
        return float(stored) if stored is not None else None

    async def async_set_native_value(self, value: float) -> None:
        await self.hass.services.async_call(
            DOMAIN,
            "set_backup_reserve",
            {"percent": int(value), "source": "user"},
            blocking=False,
        )


class OffGridEvReserveNumber(_TeslaSiteNumberBase):
    """Off-grid vehicle charging reserve %."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass, entry,
            key="tesla_off_grid_ev_reserve",
            name="Off-Grid EV Reserve",
            icon="mdi:car-electric",
        )

    @property
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_EV_CHARGING)

    @property
    def native_value(self) -> float | None:
        coord = self._tesla_coord()
        if coord is None:
            return None
        val = getattr(coord, "_off_grid_reserve_percent", None)
        return float(val) if val is not None else None

    async def async_set_native_value(self, value: float) -> None:
        await self.hass.services.async_call(
            DOMAIN,
            "set_off_grid_ev_reserve",
            {"percent": int(value), "source": "user"},
            blocking=False,
        )


class ForcePowerNumber(NumberEntity):
    """User-settable force charge/discharge power target (kW).

    Stores the preferred power level for manual force charge/discharge.
    A value of 0 means "use the inverter's rated/BMS max automatically".
    """

    _attr_has_entity_name = True
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_step = FORCE_POWER_STEP_KW
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_icon = "mdi:lightning-bolt"
    # User-facing power input for force charge/discharge — Controls, not Configuration.

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_force_power_kw"
        self._attr_suggested_object_id = "power_sync_force_power_kw"
        self._attr_name = "Force Power"
        self._attr_native_value: float = 0.0

    @property
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_BATTERY)

    @property
    def native_max_value(self) -> float:
        """Scale the force-power slider to the site instead of a universal 50 kW."""
        local = self._local_power_limits_kw()
        if local is not None:
            return min(FORCE_POWER_FALLBACK_MAX_KW, max(local.values()))
        candidates = [
            *self._configured_power_limits_kw(),
            *self._coordinator_power_limits_kw(),
            _positive_float(self._attr_native_value),
        ]
        max_kw = max((value for value in candidates if value), default=None)
        if max_kw is None:
            return FORCE_POWER_FALLBACK_MAX_KW
        rounded_max = math.ceil(max_kw / FORCE_POWER_STEP_KW) * FORCE_POWER_STEP_KW
        return min(
            FORCE_POWER_FALLBACK_MAX_KW,
            max(FORCE_POWER_STEP_KW, round(rounded_max, 2)),
        )

    def _local_power_limits_kw(self) -> dict[str, float] | None:
        """Separate battery charge input and grid export from fresh LAN data."""
        snapshot = _fresh_powerwall_local_snapshot(self.hass, self._entry)
        if snapshot is None:
            return ({"charge_input_max_kw": 0.0, "grid_export_max_kw": 0.0}
                    if self._entry.data.get(CONF_POWERWALL_LOCAL_PAIRED) else None)
        from .powerwall_local.battery_dispatch import manual_power_limits_kw
        try:
            runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
            client = runtime.get("powerwall_local", {}).get("client")
            return manual_power_limits_kw(snapshot, {**self._entry.data, **self._entry.options},
                                          getattr(client, "dispatch_discharge_limit_w", None))
        except (TypeError, ValueError):
            return {"charge_input_max_kw": 0.0, "grid_export_max_kw": 0.0}

    @property
    def native_value(self) -> float:
        local = self._local_power_limits_kw()
        maximum = max(local.values()) if local is not None else None
        # Zero means Max to the force switches. Never erase a positive user
        # target merely because telemetry or directional headroom is absent.
        return min(self._attr_native_value, maximum) if maximum is not None and maximum > 0 else self._attr_native_value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._local_power_limits_kw() or {}

    def _configured_power_limits_kw(self) -> list[float]:
        entry = self._entry
        candidates: list[float] = []

        for key in (
            CONF_OPTIMIZATION_MAX_CHARGE_W,
            CONF_OPTIMIZATION_MAX_DISCHARGE_W,
        ):
            watts = _positive_float(_entry_value(entry, key))
            if watts:
                candidates.append(watts / 1000.0)

        for key in (
            CONF_SAJ_INVERTER_RATED_KW,
            CONF_FRONIUS_RESERVA_MAX_CHARGE_KW,
            CONF_FRONIUS_RESERVA_MAX_DISCHARGE_KW,
            CONF_NEOVOLT_MAX_CHARGE_KW,
            CONF_NEOVOLT_MAX_DISCHARGE_KW,
        ):
            kw = _positive_float(_entry_value(entry, key))
            if kw:
                candidates.append(kw)

        solax_voltage = _positive_float(_entry_value(entry, CONF_SOLAX_BATTERY_NOMINAL_V))
        if solax_voltage:
            for key in (
                CONF_SOLAX_MAX_CHARGE_CURRENT_A,
                CONF_SOLAX_MAX_DISCHARGE_CURRENT_A,
            ):
                amps = _positive_float(_entry_value(entry, key))
                if amps:
                    candidates.append((amps * solax_voltage) / 1000.0)

        return candidates

    def _coordinator_power_limits_kw(self) -> list[float]:
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        candidates: list[float] = []

        opt_coord = entry_data.get("optimization_coordinator")
        opt_config = getattr(opt_coord, "_config", None)
        for attr in ("max_charge_w", "max_discharge_w"):
            watts = _positive_float(getattr(opt_config, attr, None))
            if watts:
                candidates.append(watts / 1000.0)

        for coord_key in FORCE_POWER_COORDINATOR_KEYS:
            coord = entry_data.get(coord_key)
            data = getattr(coord, "data", None) or {}
            for field in (
                "battery_max_charge_power_w",
                "battery_max_discharge_power_w",
            ):
                watts = _positive_float(data.get(field))
                if watts:
                    candidates.append(watts / 1000.0)
            for field in ("battery_max_charge_power", "battery_max_discharge_power"):
                kw = _positive_float(data.get(field))
                if kw:
                    candidates.append(kw)

        return candidates

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()
