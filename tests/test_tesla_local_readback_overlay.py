from __future__ import annotations

import importlib
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"


def _load_platform_module(module_name: str):
    saved = {
        name: sys.modules.get(name)
        for name in (
            "homeassistant",
            "homeassistant.components",
            "homeassistant.components.number",
            "homeassistant.components.select",
            "homeassistant.config_entries",
            "homeassistant.const",
            "homeassistant.core",
            "homeassistant.helpers",
            "homeassistant.helpers.entity",
            "homeassistant.helpers.entity_platform",
            "homeassistant.helpers.entity_registry",
            "power_sync",
            "power_sync.const",
            f"power_sync.{module_name}",
        )
    }

    ha_root = types.ModuleType("homeassistant")
    ha_components = types.ModuleType("homeassistant.components")
    ha_number = types.ModuleType("homeassistant.components.number")
    ha_select = types.ModuleType("homeassistant.components.select")
    ha_config_entries = types.ModuleType("homeassistant.config_entries")
    ha_const = types.ModuleType("homeassistant.const")
    ha_core = types.ModuleType("homeassistant.core")
    ha_helpers = types.ModuleType("homeassistant.helpers")
    ha_entity = types.ModuleType("homeassistant.helpers.entity")
    ha_entity_platform = types.ModuleType("homeassistant.helpers.entity_platform")
    ha_entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")

    class _Entity:
        pass

    ha_number.NumberEntity = _Entity
    ha_number.NumberMode = SimpleNamespace(SLIDER="slider")
    ha_select.SelectEntity = _Entity
    ha_config_entries.ConfigEntry = type("ConfigEntry", (), {})
    ha_const.EntityCategory = SimpleNamespace(CONFIG="config", DIAGNOSTIC="diagnostic")
    ha_const.PERCENTAGE = "%"
    ha_const.UnitOfPower = SimpleNamespace(KILO_WATT="kW")
    ha_core.HomeAssistant = type("HomeAssistant", (), {})
    ha_entity.EntityCategory = ha_const.EntityCategory
    ha_entity_platform.AddEntitiesCallback = object
    ha_entity_registry.async_get = lambda hass: SimpleNamespace(
        async_get_entity_id=lambda *args, **kwargs: None,
        async_get=lambda *args, **kwargs: None,
        async_update_entity=lambda *args, **kwargs: None,
    )

    ps_module = types.ModuleType("power_sync")
    ps_module.__path__ = [str(COMPONENT_ROOT)]
    const_module = types.ModuleType("power_sync.const")
    const_module.DOMAIN = "power_sync"
    const_module.CONF_TESLA_ENERGY_SITE_ID = "tesla_energy_site_id"
    const_module.CONF_POWERWALL_LOCAL_PAIRED = "powerwall_local_paired"
    const_module.CONF_FORCE_CHARGE_DURATION = "force_charge_duration"
    const_module.CONF_FORCE_DISCHARGE_DURATION = "force_discharge_duration"
    const_module.DEFAULT_DISCHARGE_DURATION = 60
    const_module.DISCHARGE_DURATIONS = [15, 30, 60]
    const_module.TESLA_SITE_INFO_CONTROL_MAX_AGE_SECONDS = 30
    const_module.TESLA_CAPABILITY_WAIT_SECONDS = 30
    const_module.SENSOR_FAMILY_BATTERY = "battery"
    const_module.SENSOR_FAMILY_GRID_HOME = "grid_home"
    const_module.SENSOR_FAMILY_EV_CHARGING = "ev_charging"
    const_module.family_device_info = lambda entry_id, family: {
        "identifiers": {("power_sync", f"{entry_id}_{family}")},
    }
    for name in (
        "CONF_FOXESS_HOST",
        "CONF_FOXESS_SERIAL_PORT",
        "CONF_FOXESS_CLOUD_API_KEY",
        "CONF_GOODWE_HOST",
        "CONF_SIGENERGY_STATION_ID",
        "CONF_SUNGROW_HOST",
        "CONF_ALPHAESS_MODBUS_HOST",
        "CONF_ESY_CONFIG_ENTRY_ID",
        "CONF_SOLAX_CONFIG_ENTRY_ID",
        "CONF_SOLAX_ENTITY_PREFIX",
        "CONF_SAJ_CONFIG_ENTRY_ID",
        "CONF_NEOVOLT_CONFIG_ENTRY_ID",
        "CONF_NEOVOLT_CONFIG_ENTRY_IDS",
        "CONF_FRONIUS_RESERVA_MAX_CHARGE_KW",
        "CONF_FRONIUS_RESERVA_MAX_DISCHARGE_KW",
        "CONF_NEOVOLT_MAX_CHARGE_KW",
        "CONF_NEOVOLT_MAX_DISCHARGE_KW",
        "CONF_OPTIMIZATION_MAX_CHARGE_W",
        "CONF_OPTIMIZATION_MAX_DISCHARGE_W",
        "CONF_SAJ_INVERTER_RATED_KW",
        "CONF_SOLAX_BATTERY_NOMINAL_V",
        "CONF_SOLAX_MAX_CHARGE_CURRENT_A",
        "CONF_SOLAX_MAX_DISCHARGE_CURRENT_A",
    ):
        setattr(const_module, name, name.lower())

    sys.modules["homeassistant"] = ha_root
    sys.modules["homeassistant.components"] = ha_components
    sys.modules["homeassistant.components.number"] = ha_number
    sys.modules["homeassistant.components.select"] = ha_select
    sys.modules["homeassistant.config_entries"] = ha_config_entries
    sys.modules["homeassistant.const"] = ha_const
    sys.modules["homeassistant.core"] = ha_core
    sys.modules["homeassistant.helpers"] = ha_helpers
    sys.modules["homeassistant.helpers.entity"] = ha_entity
    sys.modules["homeassistant.helpers.entity_platform"] = ha_entity_platform
    sys.modules["homeassistant.helpers.entity_registry"] = ha_entity_registry
    sys.modules["power_sync"] = ps_module
    sys.modules["power_sync.const"] = const_module
    sys.modules.pop(f"power_sync.{module_name}", None)

    module = importlib.import_module(f"power_sync.{module_name}")

    def restore() -> None:
        for name, module_obj in saved.items():
            if module_obj is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module_obj

    return module, restore


def _entry():
    return SimpleNamespace(
        entry_id="entry-1",
        data={"powerwall_local_paired": True},
        options={},
    )


def _hass(local_data=None, *, local_age_seconds=0, cloud_site_info=None):
    local_coord = None
    if local_data is not None:
        local_coord = SimpleNamespace(
            data=local_data,
            last_success_ts=time.time() - local_age_seconds,
        )
    return SimpleNamespace(
        data={
            "power_sync": {
                "entry-1": {
                    "powerwall_local": {"coordinator": local_coord},
                    "tesla_coordinator": SimpleNamespace(
                        _site_info_cache=cloud_site_info or {}
                    ),
                }
            }
        }
    )


def test_backup_reserve_number_prefers_fresh_local_readback():
    number, restore = _load_platform_module("number")
    try:
        entity = number.BackupReserveNumber(
            _hass(
                SimpleNamespace(backup_reserve_percent=42),
                cloud_site_info={"backup_reserve_percent": 5},
            ),
            _entry(),
        )

        assert entity.native_value == 42.0
    finally:
        restore()


def test_backup_reserve_number_falls_back_when_local_readback_is_stale():
    number, restore = _load_platform_module("number")
    try:
        entity = number.BackupReserveNumber(
            _hass(
                SimpleNamespace(backup_reserve_percent=42),
                local_age_seconds=120,
                cloud_site_info={"backup_reserve_percent": 5},
            ),
            _entry(),
        )

        assert entity.native_value == 5.0
    finally:
        restore()


def test_operation_mode_select_prefers_fresh_local_readback():
    select, restore = _load_platform_module("select")
    try:
        entity = select.TeslaOperationModeSelect(
            _hass(
                SimpleNamespace(operation_mode="backup"),
                cloud_site_info={"default_real_mode": "self_consumption"},
            ),
            _entry(),
        )

        assert entity.current_option == "backup"
    finally:
        restore()


def test_operation_mode_select_falls_back_for_invalid_local_value():
    select, restore = _load_platform_module("select")
    try:
        entity = select.TeslaOperationModeSelect(
            _hass(
                SimpleNamespace(operation_mode="invalid"),
                cloud_site_info={"default_real_mode": "self_consumption"},
            ),
            _entry(),
        )

        assert entity.current_option == "self_consumption"
    finally:
        restore()
