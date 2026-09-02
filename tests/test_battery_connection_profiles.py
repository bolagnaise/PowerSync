"""Contract tests for battery connection profiles and sensor discovery."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys

import pytest


ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "power_sync"


@pytest.fixture(autouse=True)
def _isolate_import_stubs():
    """Keep this file's lightweight HA/package stubs out of the full suite."""
    prefixes = ("battery_profile_test", "homeassistant")
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name.startswith(prefixes)
    }
    yield
    for name in list(sys.modules):
        if name.startswith(prefixes):
            sys.modules.pop(name, None)
    sys.modules.update(saved)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_profile_modules():
    package = ModuleType("battery_profile_test")
    package.__path__ = [str(COMPONENT)]
    backend = ModuleType("battery_profile_test.battery_backend")
    backend.__path__ = [str(COMPONENT / "battery_backend")]
    sys.modules[package.__name__] = package
    sys.modules[backend.__name__] = backend
    const = _load_module("battery_profile_test.const", COMPONENT / "const.py")
    profiles = _load_module(
        "battery_profile_test.battery_backend.profiles",
        COMPONENT / "battery_backend" / "profiles.py",
    )
    return const, profiles


def _load_discovery_module():
    const, _profiles = _load_profile_modules()
    ha = ModuleType("homeassistant")
    core = ModuleType("homeassistant.core")
    helpers = ModuleType("homeassistant.helpers")
    entity_registry = ModuleType("homeassistant.helpers.entity_registry")
    core.HomeAssistant = object
    entity_registry.async_get = lambda hass: hass.entity_registry
    entity_registry.async_entries_for_config_entry = (
        lambda registry, entry_id: registry.entries_for(entry_id)
    )
    helpers.entity_registry = entity_registry
    ha.core = core
    ha.helpers = helpers
    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.core"] = core
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.entity_registry"] = entity_registry
    discovery = _load_module(
        "battery_profile_test.battery_backend.discovery",
        COMPONENT / "battery_backend" / "discovery.py",
    )
    return const, discovery


def test_all_supported_battery_systems_have_registered_profiles():
    const, profiles = _load_profile_modules()
    systems = set(const.BATTERY_SYSTEMS)

    registered = {profile.battery_system for profile in profiles.PROFILE_REGISTRY.values()}

    assert len(systems) == 14
    assert registered == systems
    assert all(profiles.profiles_for_system(system) for system in systems)
    assert "goodwe_ha_monitoring" in profiles.PROFILE_REGISTRY
    assert all(
        profile.requires_upstream
        for profile in profiles.PROFILE_REGISTRY.values()
        if profile.route_kind == "ha_monitoring"
    )


def test_legacy_entries_resolve_to_their_existing_route():
    const, profiles = _load_profile_modules()

    assert profiles.resolve_connection_profile(
        {
            const.CONF_BATTERY_SYSTEM: const.BATTERY_SYSTEM_FOXESS,
            const.CONF_FOXESS_CONNECTION_TYPE: const.FOXESS_CONNECTION_ENTITY,
        },
        {},
    ).profile_id == "foxess_ha_modbus"
    assert profiles.resolve_connection_profile(
        {
            const.CONF_BATTERY_SYSTEM: const.BATTERY_SYSTEM_SUNGROW,
            const.CONF_SUNGROW_CONNECTION_TYPE: const.SUNGROW_CONNECTION_IHOMEMANAGER,
        },
        {},
    ).profile_id == "sungrow_ihomemanager"
    assert profiles.resolve_connection_profile(
        {const.CONF_BATTERY_SYSTEM: const.BATTERY_SYSTEM_ANKER_SOLIX},
        {},
    ).profile_id == "anker_direct"
    assert profiles.resolve_connection_profile(
        {
            const.CONF_BATTERY_SYSTEM: const.BATTERY_SYSTEM_GOODWE,
            "goodwe_ems_control_mode": "entity",
        },
        {},
    ).profile_id == "goodwe_direct"
    assert profiles.resolve_connection_profile(
        {
            const.CONF_BATTERY_SYSTEM: const.BATTERY_SYSTEM_GOODWE,
            const.CONF_BATTERY_CONNECTION_PROFILE: "goodwe_ha",
            "goodwe_ems_control_mode": "entity",
        },
        {},
    ).profile_id == "goodwe_ha"
    assert profiles.resolve_connection_profile(
        {
            const.CONF_BATTERY_SYSTEM: const.BATTERY_SYSTEM_GOODWE,
            const.CONF_BATTERY_CONNECTION_PROFILE: "goodwe_direct",
            "goodwe_ems_control_mode": "entity",
        },
        {},
    ).profile_id == "goodwe_direct"


def test_cross_brand_profile_is_rejected_by_resolution():
    const, profiles = _load_profile_modules()

    profile = profiles.resolve_connection_profile(
        {
            const.CONF_BATTERY_SYSTEM: const.BATTERY_SYSTEM_SIGENERGY,
            const.CONF_BATTERY_CONNECTION_PROFILE: "sungrow_direct",
        },
        {},
    )

    assert profile.profile_id == "sigenergy_direct"


class _Registry:
    def __init__(self, rows):
        self.entities = {row.entity_id: row for row in rows}

    def async_get(self, entity_id):
        return self.entities.get(entity_id)

    def entries_for(self, entry_id):
        return [
            row
            for row in self.entities.values()
            if entry_id in set(getattr(row, "config_entry_ids", ()) or ())
        ]


class _States:
    def __init__(self, values):
        self._values = values

    def get(self, entity_id):
        value = self._values.get(entity_id)
        if value is None:
            return None
        return SimpleNamespace(state=value, attributes={})


def _row(
    entity_id,
    unique_id,
    *,
    platform="foxess_modbus",
    entry_id="selected",
    disabled=False,
):
    return SimpleNamespace(
        entity_id=entity_id,
        unique_id=unique_id,
        platform=platform,
        config_entry_ids={entry_id} if entry_id else set(),
        config_entry_id=entry_id or None,
        device_id="device-1",
        disabled_by="user" if disabled else None,
    )


def test_discovery_stays_inside_selected_config_entry_and_omits_controls():
    const, discovery = _load_discovery_module()
    rows = [
        _row("sensor.fox_battery_soc", "fox_battery_soc"),
        _row("sensor.fox_battery_power", "fox_battery_power"),
        _row("sensor.fox_grid_power", "fox_grid_power"),
        _row("sensor.fox_pv_power", "fox_pv_power"),
        _row("sensor.fox_load_power", "fox_load_power"),
        _row("sensor.fox_battery_temperature", "fox_battery_temperature"),
        _row("sensor.other_battery_soc", "other_battery_soc", entry_id="other"),
        _row("select.fox_work_mode", "fox_work_mode"),
        _row(
            "sensor.fox_disabled_fault",
            "fox_disabled_fault",
            disabled=True,
        ),
    ]
    hass = SimpleNamespace(
        entity_registry=_Registry(rows),
        states=_States({row.entity_id: "1" for row in rows}),
    )

    catalog = discovery.discover_battery_sensor_catalog(
        hass,
        battery_system=const.BATTERY_SYSTEM_FOXESS,
        profile_id="foxess_ha_modbus",
        allowed_domains=("foxess_modbus",),
        config_entry_id="selected",
        display_mode=const.BATTERY_SENSOR_DISPLAY_ALL,
    )
    canonical, missing = discovery.discover_canonical_entities(
        catalog,
        battery_system=const.BATTERY_SYSTEM_FOXESS,
    )

    assert "sensor.other_battery_soc" not in catalog["entity_ids"]
    assert "select.fox_work_mode" not in catalog["entity_ids"]
    assert "sensor.fox_disabled_fault" not in catalog["entity_ids"]
    assert catalog["disabled_count"] == 1
    assert missing == []
    assert canonical == {
        "battery_level": "sensor.fox_battery_soc",
        "battery_power": "sensor.fox_battery_power",
        "grid_power": "sensor.fox_grid_power",
        "solar_power": "sensor.fox_pv_power",
        "load_power": "sensor.fox_load_power",
    }


def test_goodwe_entity_profile_accepts_standard_active_power_and_ppv_names():
    """#398: profile validation must match the GoodWe entity bridge names."""
    const, discovery = _load_discovery_module()
    rows = [
        _row(
            "sensor.goodwe_battery_soc",
            "goodwe_battery_soc",
            platform="goodwe",
        ),
        _row(
            "sensor.goodwe_battery_power",
            "goodwe_battery_power",
            platform="goodwe",
        ),
        _row(
            "sensor.goodwe_active_power",
            "goodwe_active_power",
            platform="goodwe",
        ),
        _row("sensor.goodwe_ppv", "goodwe_ppv", platform="goodwe"),
        _row(
            "sensor.goodwe_house_consumption",
            "goodwe_house_consumption",
            platform="goodwe",
        ),
    ]
    hass = SimpleNamespace(
        entity_registry=_Registry(rows),
        states=_States({row.entity_id: "1" for row in rows}),
    )

    catalog = discovery.discover_battery_sensor_catalog(
        hass,
        battery_system=const.BATTERY_SYSTEM_GOODWE,
        profile_id="goodwe_ha",
        allowed_domains=("goodwe",),
        config_entry_id="selected",
        display_mode=const.BATTERY_SENSOR_DISPLAY_ALL,
    )
    canonical, missing = discovery.discover_canonical_entities(
        catalog,
        battery_system=const.BATTERY_SYSTEM_GOODWE,
    )

    assert missing == []
    assert canonical == {
        "battery_level": "sensor.goodwe_battery_soc",
        "battery_power": "sensor.goodwe_battery_power",
        "grid_power": "sensor.goodwe_active_power",
        "solar_power": "sensor.goodwe_ppv",
        "load_power": "sensor.goodwe_house_consumption",
    }


def test_sungrow_anchor_limits_yaml_discovery_to_stable_unique_id_namespace():
    const, discovery = _load_discovery_module()
    rows = [
        _row(
            "sensor.sg_battery_soc",
            "sg_battery_soc",
            platform="modbus",
            entry_id="",
        ),
        _row(
            "sensor.sg_export_power",
            "sg_export_power_raw",
            platform="modbus",
            entry_id="",
        ),
        _row(
            "sensor.unrelated_battery_soc",
            "other_battery_soc",
            platform="modbus",
            entry_id="",
        ),
    ]
    hass = SimpleNamespace(
        entity_registry=_Registry(rows),
        states=_States({row.entity_id: "1" for row in rows}),
    )

    catalog = discovery.discover_battery_sensor_catalog(
        hass,
        battery_system=const.BATTERY_SYSTEM_SUNGROW,
        profile_id="sungrow_ha_monitoring",
        allowed_domains=("modbus",),
        anchor_entity_id="sensor.sg_battery_soc",
        display_mode=const.BATTERY_SENSOR_DISPLAY_ALL,
    )
    discovery.discover_canonical_entities(
        catalog,
        battery_system=const.BATTERY_SYSTEM_SUNGROW,
    )

    assert set(catalog["entity_ids"]) == {
        "sensor.sg_battery_soc",
        "sensor.sg_export_power",
    }
    assert catalog["grid_power_multiplier"] == -1.0


def test_runtime_resolves_monitoring_profile_before_direct_construction():
    source = (COMPONENT / "__init__.py").read_text()

    profile_resolution = source.index("battery_connection_profile = resolve_connection_profile")
    direct_sigenergy = source.index("SigenergyEnergyCoordinator(", profile_resolution)
    monitoring_gate = source.index(
        'if battery_connection_profile.route_kind == "ha_monitoring":',
        profile_resolution,
    )

    assert profile_resolution < monitoring_gate < direct_sigenergy
    assert 'elif is_sigenergy:' in source[monitoring_gate:direct_sigenergy]
    assert "no direct battery client will be constructed" in source
    monitoring_dispatch = source[monitoring_gate:direct_sigenergy]
    for assignment in (
        "sigenergy_coordinator = discovered_coordinator",
        "sungrow_coordinator = discovered_coordinator",
        "alphaess_coordinator = discovered_coordinator",
        "goodwe_coordinator = discovered_coordinator",
        "tesla_coordinator = discovered_coordinator",
    ):
        assert assignment in monitoring_dispatch


def test_goodwe_entity_telemetry_does_not_construct_direct_controller():
    source = (COMPONENT / "coordinator.py").read_text()
    class_source = source.split("class GoodWeEnergyCoordinator", 1)[1].split(
        "class SolaxBatteryEnergyCoordinator", 1
    )[0]
    init_source = class_source.split("def __init__", 1)[1].split(
        "def _native_integration_enabled", 1
    )[0]

    assert "if self._using_entity_telemetry:" in init_source
    direct_branch = init_source.split("else:", 1)[1]
    assert "GoodWeBatteryController" in direct_branch
    assert "self._controller = None" in init_source
    assert "if self._controller is None:" in class_source


def test_profile_switch_restores_old_route_before_persisting_new_route():
    source = (COMPONENT / "config_flow.py").read_text()
    method = source.split(
        "async def _save_connection_profile_and_reload",
        1,
    )[1].split("def _electricity_provider", 1)[0]

    restore = method.index("await async_prepare_monitoring_handoff(")
    persist = method.index("self._save_connection_and_reload(data_updates)")
    assert restore < persist
    assert "route_changed and not old_profile.monitoring_only" in method
    assert "return self.async_abort(reason=\"monitoring_cleanup_failed\")" in method


def test_ha_only_solaredge_profile_cannot_construct_curtailment_client():
    source = (COMPONENT / "__init__.py").read_text()
    getter = source.split("def _get_solaredge_curtailment_controller", 1)[1].split(
        "async def _restore_solaredge_curtailment_for_dispatch",
        1,
    )[0]

    guard = getter.index('profile_id == "solaredge_ha_only"')
    direct_import = getter.index("from .inverters.solaredge import SolarEdgeController")
    assert guard < direct_import
