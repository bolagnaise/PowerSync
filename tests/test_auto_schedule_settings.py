"""Tests for auto-schedule settings serialization and clearing."""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"

sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))

_ha_root = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
_ha_core = sys.modules.setdefault("homeassistant.core", types.ModuleType("homeassistant.core"))
_ha_config_entries = sys.modules.setdefault(
    "homeassistant.config_entries", types.ModuleType("homeassistant.config_entries")
)
_ha_exceptions = sys.modules.setdefault(
    "homeassistant.exceptions", types.ModuleType("homeassistant.exceptions")
)
_ha_helpers = sys.modules.setdefault("homeassistant.helpers", types.ModuleType("homeassistant.helpers"))
_ha_storage = sys.modules.setdefault(
    "homeassistant.helpers.storage", types.ModuleType("homeassistant.helpers.storage")
)
_ha_update = sys.modules.setdefault(
    "homeassistant.helpers.update_coordinator",
    types.ModuleType("homeassistant.helpers.update_coordinator"),
)
_ha_event = sys.modules.setdefault(
    "homeassistant.helpers.event", types.ModuleType("homeassistant.helpers.event")
)
_ha_util = sys.modules.setdefault("homeassistant.util", types.ModuleType("homeassistant.util"))
_ha_dt = sys.modules.setdefault("homeassistant.util.dt", types.ModuleType("homeassistant.util.dt"))
_ha_core.HomeAssistant = type("HomeAssistant", (), {})
_ha_config_entries.ConfigEntry = type("ConfigEntry", (), {})
_ha_exceptions.ConfigEntryNotReady = type("ConfigEntryNotReady", (Exception,), {})
_ha_storage.Store = type("Store", (), {"__init__": lambda self, *args, **kwargs: None})
_ha_update.DataUpdateCoordinator = type(
    "DataUpdateCoordinator",
    (),
    {
        "__class_getitem__": classmethod(lambda cls, item: cls),
        "__init__": lambda self, *args, **kwargs: None,
    },
)
_ha_event.async_track_time_change = lambda *args, **kwargs: (lambda: None)
_ha_helpers.storage = _ha_storage
_ha_helpers.update_coordinator = _ha_update
_ha_helpers.event = _ha_event
_ha_dt.now = getattr(_ha_dt, "now", lambda *args, **kwargs: None)
_ha_util.dt = _ha_dt
_ha_root.helpers = _ha_helpers
_ha_root.util = _ha_util

_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps

_optimization = types.ModuleType("power_sync.optimization")
_optimization.__path__ = [str(ROOT / "optimization")]
sys.modules["power_sync.optimization"] = _optimization

_automations = types.ModuleType("power_sync.automations")
_automations.__path__ = [str(ROOT / "automations")]
sys.modules["power_sync.automations"] = _automations

sys.modules.pop("power_sync.const", None)
ev_planner = importlib.import_module("power_sync.automations.ev_charging_planner")


def test_empty_departure_times_does_not_rehydrate_legacy_schedule():
    settings = ev_planner.AutoScheduleSettings.from_dict({
        "departure_time": "07:30",
        "departure_days": [0, 1, 2, 3, 4],
        "departure_times": {},
    })

    assert settings.departure_times == {}
    assert settings.to_dict()["departure_time"] is None
    assert settings.to_dict()["departure_days"] == []


def test_empty_per_day_overrides_clear_legacy_aliases():
    settings = ev_planner.AutoScheduleSettings.from_dict({
        "departure_priorities": {},
        "departure_min_battery_to_start": {},
        "departure_home_battery_min": {"0": 80},
        "departure_limit_grid_import": {},
        "departure_no_grid_import": {"0": True},
        "departure_consume_battery_level": {},
        "departure_stop_at_battery_floor": {},
    })

    assert settings.departure_priorities == {}
    assert settings.departure_min_battery_to_start == {}
    assert settings.departure_limit_grid_import == {}
    assert settings.departure_consume_battery_level == {}
    assert settings.departure_stop_at_battery_floor == {}


def test_generic_status_entity_round_trips_with_auto_schedule_settings():
    settings = ev_planner.AutoScheduleSettings.from_dict({
        "charger_type": "generic",
        "charger_switch_entity": "switch.garage_ev",
        "charger_amps_entity": "number.garage_ev_current",
        "charger_status_entity": "sensor.garage_ev_status",
    })

    assert settings.charger_status_entity == "sensor.garage_ev_status"
    assert settings.to_dict()["charger_status_entity"] == "sensor.garage_ev_status"


def test_preserve_home_battery_round_trips_with_auto_schedule_settings():
    settings = ev_planner.AutoScheduleSettings.from_dict({
        "preserve_home_battery": True,
    })

    assert settings.preserve_home_battery is True
    assert settings.to_dict()["preserve_home_battery"] is True


def test_preserve_home_battery_disables_consume_battery_level_on_load():
    settings = ev_planner.AutoScheduleSettings.from_dict({
        "preserve_home_battery": True,
        "consume_battery_level": 50,
    })

    assert settings.preserve_home_battery is True
    assert settings.consume_battery_level == 0


def test_departure_preserve_home_battery_round_trips_and_overrides_default():
    settings = ev_planner.AutoScheduleSettings.from_dict({
        "preserve_home_battery": True,
        "departure_preserve_home_battery": {"0": False, "1": True},
    })

    assert settings.departure_preserve_home_battery == {0: False, 1: True}
    assert settings.get_effective_preserve_home_battery(0) is False
    assert settings.get_effective_preserve_home_battery(1) is True
    assert settings.get_effective_preserve_home_battery(2) is True
    assert settings.to_dict()["departure_preserve_home_battery"] == {
        "0": False,
        "1": True,
    }


def test_departure_preserve_home_battery_disables_day_consume_on_load():
    settings = ev_planner.AutoScheduleSettings.from_dict({
        "departure_consume_battery_level": {"0": 45, "1": 35},
        "departure_preserve_home_battery": {"0": True, "1": False},
    })

    assert settings.departure_consume_battery_level == {0: 0, 1: 35}
    assert settings.departure_preserve_home_battery == {0: True, 1: False}


def test_auto_schedule_preserve_and_consume_settings_are_mutually_exclusive():
    executor = object.__new__(ev_planner.AutoScheduleExecutor)
    executor._settings = {}
    executor._state = {}

    settings = executor.update_settings(
        "ev-1",
        {
            "consume_battery_level": 40,
            "preserve_home_battery": True,
        },
    )

    assert settings.preserve_home_battery is True
    assert settings.consume_battery_level == 0

    settings = executor.update_settings(
        "ev-1",
        {"consume_battery_level": 30},
    )

    assert settings.consume_battery_level == 30
    assert settings.preserve_home_battery is False


def test_auto_schedule_departure_preserve_and_consume_settings_are_mutually_exclusive():
    executor = object.__new__(ev_planner.AutoScheduleExecutor)
    executor._settings = {}
    executor._state = {}

    settings = executor.update_settings(
        "ev-1",
        {
            "departure_consume_battery_level": {0: 40},
            "departure_preserve_home_battery": {0: True},
        },
    )

    assert settings.departure_preserve_home_battery == {0: True}
    assert settings.departure_consume_battery_level == {0: 0}

    settings = executor.update_settings(
        "ev-1",
        {"departure_consume_battery_level": {0: 30}},
    )

    assert settings.departure_consume_battery_level == {0: 30}
    assert settings.departure_preserve_home_battery == {0: False}


def test_vehicle_charger_config_syncs_generic_status_entity():
    settings = ev_planner.AutoScheduleSettings()

    settings.apply_charger_config({
        "charger_type": "generic",
        "min_amps": 6,
        "max_amps": 24,
        "voltage": 240,
        "phases": 3,
        "charger_switch_entity": "switch.garage_ev",
        "charger_amps_entity": "number.garage_ev_current",
        "charger_status_entity": "sensor.garage_ev_status",
    })

    assert settings.charger_type == "generic"
    assert settings.min_charge_amps == 6
    assert settings.max_charge_amps == 24
    assert settings.voltage == 240
    assert settings.phases == 3
    assert settings.charger_status_entity == "sensor.garage_ev_status"


def test_vehicle_charger_config_syncs_app_charge_amp_aliases():
    settings = ev_planner.AutoScheduleSettings(
        min_charge_amps=5,
        max_charge_amps=32,
    )

    settings.apply_charger_config({
        "min_charge_amps": 6,
        "max_charge_amps": 15,
        "voltage": 240,
        "phases": 1,
    })

    assert settings.min_charge_amps == 6
    assert settings.max_charge_amps == 15
    assert settings.voltage == 240
    assert settings.phases == 1


def test_auto_schedule_sync_reads_app_vehicle_config_store():
    automation_store = types.SimpleNamespace(
        _data={
            "vehicle_charging_configs": [
                {
                    "vehicle_id": "ble_teslablefbd",
                    "charger_type": "tesla",
                    "min_amps": 5,
                    "max_amps": 15,
                    "voltage": 240,
                    "phases": 1,
                }
            ]
        }
    )
    hass = types.SimpleNamespace(
        data={
            ev_planner.DOMAIN: {
                "entry-1": {
                    "automation_store": automation_store,
                }
            }
        }
    )
    entry = types.SimpleNamespace(entry_id="entry-1")
    executor = object.__new__(ev_planner.AutoScheduleExecutor)
    executor.hass = hass
    executor.config_entry = entry
    executor._store = types.SimpleNamespace(_data={})

    settings = ev_planner.AutoScheduleSettings(
        vehicle_id="ble_teslablefbd",
        max_charge_amps=32,
        voltage=230,
    )

    executor._sync_charger_params_from_vehicle_configs(
        "ble_teslablefbd",
        settings,
    )

    assert settings.max_charge_amps == 15
    assert settings.voltage == 240
    assert settings.phases == 1


def test_auto_schedule_sync_reads_app_charge_amp_aliases():
    automation_store = types.SimpleNamespace(
        _data={
            "vehicle_charging_configs": [
                {
                    "vehicle_id": "ble_teslablefbd",
                    "charger_type": "tesla",
                    "min_charge_amps": 6,
                    "max_charge_amps": 15,
                    "voltage": 240,
                    "phases": 1,
                }
            ]
        }
    )
    hass = types.SimpleNamespace(
        data={
            ev_planner.DOMAIN: {
                "entry-1": {
                    "automation_store": automation_store,
                }
            }
        }
    )
    entry = types.SimpleNamespace(entry_id="entry-1")
    executor = object.__new__(ev_planner.AutoScheduleExecutor)
    executor.hass = hass
    executor.config_entry = entry
    executor._store = types.SimpleNamespace(_data={})

    settings = ev_planner.AutoScheduleSettings(
        vehicle_id="ble_teslablefbd",
        min_charge_amps=5,
        max_charge_amps=32,
        voltage=230,
    )

    executor._sync_charger_params_from_vehicle_configs(
        "ble_teslablefbd",
        settings,
    )

    assert settings.min_charge_amps == 6
    assert settings.max_charge_amps == 15
    assert settings.voltage == 240
    assert settings.phases == 1


def test_auto_schedule_sync_matches_ble_prefix_alias():
    automation_store = types.SimpleNamespace(
        _data={
            "vehicle_charging_configs": [
                {
                    "vehicle_id": "teslablefbd",
                    "max_amps": 15,
                    "voltage": 240,
                    "phases": 1,
                }
            ]
        }
    )
    hass = types.SimpleNamespace(
        data={
            ev_planner.DOMAIN: {
                "entry-1": {
                    "automation_store": automation_store,
                }
            }
        }
    )
    entry = types.SimpleNamespace(entry_id="entry-1")
    executor = object.__new__(ev_planner.AutoScheduleExecutor)
    executor.hass = hass
    executor.config_entry = entry
    executor._store = types.SimpleNamespace(_data={})

    settings = ev_planner.AutoScheduleSettings(
        vehicle_id="ble_teslablefbd",
        max_charge_amps=32,
    )

    executor._sync_charger_params_from_vehicle_configs(
        "ble_teslablefbd",
        settings,
    )

    assert settings.max_charge_amps == 15


def test_configured_generic_entities_preserve_vehicle_overrides():
    params = {
        "charger_switch_entity": "switch.vehicle_ev",
        "charger_amps_entity": "number.vehicle_ev_current",
        "charger_status_entity": "sensor.vehicle_ev_status",
    }

    result = ev_planner._with_configured_charger_entities(
        None,
        params,
        {
            "generic_charger_switch_entity": "switch.global_ev",
            "generic_charger_amps_entity": "number.global_ev_current",
            "generic_charger_status_entity": "sensor.global_ev_status",
        },
        "generic",
    )

    assert result["charger_switch_entity"] == "switch.vehicle_ev"
    assert result["charger_amps_entity"] == "number.vehicle_ev_current"
    assert result["charger_status_entity"] == "sensor.vehicle_ev_status"


def test_power_to_amps_uses_vehicle_charger_phase_settings():
    executor = object.__new__(ev_planner.AutoScheduleExecutor)

    single_phase = ev_planner.AutoScheduleSettings(
        voltage=230,
        phases=1,
        max_charge_amps=32,
    )
    three_phase = ev_planner.AutoScheduleSettings(
        voltage=230,
        phases=3,
        max_charge_amps=32,
    )

    assert executor._power_to_amps_for_settings(6900, single_phase) == 30
    assert executor._power_to_amps_for_settings(6900, three_phase) == 10
