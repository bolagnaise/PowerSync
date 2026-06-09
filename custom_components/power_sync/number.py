"""Number platform for PowerSync integration — Tesla Energy Site controls."""
from __future__ import annotations

import asyncio
import logging
import math
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
)

_LOGGER = logging.getLogger(__name__)

FORCE_POWER_FALLBACK_MAX_KW = 50.0
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


def _positive_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


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
    if _supports_force_power:
        async_add_entities([ForcePowerNumber(hass, entry)])


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
    _attr_native_step = 0.5
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
        candidates = [
            *self._configured_power_limits_kw(),
            *self._coordinator_power_limits_kw(),
            _positive_float(self._attr_native_value),
        ]
        max_kw = max((value for value in candidates if value), default=None)
        if max_kw is None:
            return FORCE_POWER_FALLBACK_MAX_KW
        return min(FORCE_POWER_FALLBACK_MAX_KW, max(0.5, math.ceil(max_kw * 2) / 2))

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
