"""Tests for Tesla Powerwall on-grid/off-grid switch behavior."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"

_ha_root = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
_ha_components = sys.modules.setdefault(
    "homeassistant.components",
    types.ModuleType("homeassistant.components"),
)
_ha_switch = types.ModuleType("homeassistant.components.switch")


class _SwitchEntity:
    def __init__(self) -> None:
        self._remove_callbacks = []
        self.write_count = 0

    def async_on_remove(self, callback):
        if not hasattr(self, "_remove_callbacks"):
            self._remove_callbacks = []
        self._remove_callbacks.append(callback)

    def async_write_ha_state(self):
        if not hasattr(self, "write_count"):
            self.write_count = 0
        self.write_count += 1


class _SwitchEntityDescription:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


_ha_switch.SwitchEntity = _SwitchEntity
_ha_switch.SwitchEntityDescription = _SwitchEntityDescription
_ha_components.switch = _ha_switch
_ha_root.components = _ha_components
sys.modules["homeassistant.components.switch"] = _ha_switch

_ha_config_entries = types.ModuleType("homeassistant.config_entries")
_ha_config_entries.ConfigEntry = object
sys.modules["homeassistant.config_entries"] = _ha_config_entries

_ha_const = types.ModuleType("homeassistant.const")


class _EntityCategory:
    CONFIG = "config"


_ha_const.EntityCategory = _EntityCategory
sys.modules["homeassistant.const"] = _ha_const

_ha_core = types.ModuleType("homeassistant.core")
_ha_core.HomeAssistant = object
_ha_core.callback = lambda func: func
sys.modules["homeassistant.core"] = _ha_core

_ha_dispatcher = types.ModuleType("homeassistant.helpers.dispatcher")
_ha_dispatcher.async_dispatcher_connect = lambda *args, **kwargs: lambda: None
sys.modules["homeassistant.helpers.dispatcher"] = _ha_dispatcher

_ha_entity_platform = types.ModuleType("homeassistant.helpers.entity_platform")
_ha_entity_platform.AddEntitiesCallback = object
sys.modules["homeassistant.helpers.entity_platform"] = _ha_entity_platform

_ha_event = types.ModuleType("homeassistant.helpers.event")
_ha_event.async_track_time_interval = lambda *args, **kwargs: lambda: None
sys.modules["homeassistant.helpers.event"] = _ha_event

_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps

_ps_const = types.ModuleType("power_sync.const")
_ps_const.DOMAIN = "power_sync"
_ps_const.CONF_AUTO_SYNC_ENABLED = "auto_sync_enabled"
_ps_const.CONF_AUTO_UPDATE_ENABLED = "auto_update_enabled"
_ps_const.CONF_AUTO_UPDATE_TIME = "auto_update_time"
_ps_const.CONF_BATTERY_SYSTEM = "battery_system"
_ps_const.CONF_FORCE_CHARGE_DURATION = "force_charge_duration"
_ps_const.CONF_FORCE_DISCHARGE_DURATION = "force_discharge_duration"
_ps_const.DEFAULT_AUTO_UPDATE_TIME = "03:00"
_ps_const.CONF_ELECTRICITY_PROVIDER = "electricity_provider"
_ps_const.CONF_MONITORING_MODE = "monitoring_mode"
_ps_const.CONF_OPTIMIZATION_ENABLED = "optimization_enabled"
_ps_const.CONF_OPTIMIZATION_PROVIDER = "optimization_provider"
_ps_const.CONF_OPTIMIZATION_SPREAD_EXPORT_ENABLED = "optimization_spread_export_enabled"
_ps_const.CONF_OPTIMIZATION_SPREAD_IMPORT_ENABLED = "optimization_spread_import_enabled"
_ps_const.CONF_POWERWALL_LOCAL_PAIRED = "powerwall_local_paired"
_ps_const.CONF_TESLA_ENERGY_SITE_ID = "tesla_energy_site_id"
_ps_const.BATTERY_SYSTEM_TESLA = "tesla"
_ps_const.OPT_PROVIDER_POWERSYNC = "powersync"
_ps_const.TARGET_EXPORT_POWER_BATTERY_SYSTEMS = {
    "goodwe", "sigenergy", "sungrow", "foxess",
    "alphaess", "solax", "fronius_reserva", "neovolt",
}
_ps_const.TARGET_CHARGE_POWER_BATTERY_SYSTEMS = {
    "sigenergy", "foxess", "alphaess", "solax", "fronius_reserva", "neovolt",
}
_ps_const.SWITCH_TYPE_AUTO_SYNC = "auto_sync"
_ps_const.SWITCH_TYPE_AUTO_UPDATE = "auto_update"
_ps_const.SWITCH_TYPE_FORCE_DISCHARGE = "force_discharge"
_ps_const.SWITCH_TYPE_FORCE_CHARGE = "force_charge"
_ps_const.SWITCH_TYPE_MONITORING_MODE = "monitoring_mode"
_ps_const.SWITCH_TYPE_AWAY_MODE = "away_mode"
_ps_const.SWITCH_TYPE_PROFIT_MAX_MODE = "profit_max_mode"
_ps_const.SWITCH_TYPE_OPTIMIZATION_SPREAD_EXPORT = "optimization_spread_export"
_ps_const.SWITCH_TYPE_OPTIMIZATION_SPREAD_IMPORT = "optimization_spread_import"
_ps_const.SWITCH_TYPE_OPTIMIZATION_ENABLED = "optimization_enabled"
_ps_const.DEFAULT_DISCHARGE_DURATION = 60
_ps_const.ATTR_LAST_SYNC = "last_sync"
_ps_const.ATTR_SYNC_STATUS = "sync_status"
_ps_const.SENSOR_FAMILY_LP_OPTIMIZER = "lp_optimizer"
_ps_const.SENSOR_FAMILY_BATTERY = "battery"
_ps_const.SENSOR_FAMILY_CONTROLS = "controls"
_ps_const.TESLA_SITE_INFO_CONTROL_MAX_AGE_SECONDS = 30
_ps_const.POWERWALL_LOCAL_POLL_INTERVAL = 2
_ps_const.family_device_info = lambda entry_id, family: {
    "identifiers": {("power_sync", entry_id, family)}
}
sys.modules["power_sync.const"] = _ps_const

sys.modules.pop("power_sync.switch", None)
switch = importlib.import_module("power_sync.switch")


class _State:
    def __init__(self, state: str) -> None:
        self.state = state


class _States:
    def __init__(self) -> None:
        self._states = {}

    def get(self, entity_id: str):
        return self._states.get(entity_id)


class _Services:
    def __init__(self) -> None:
        self.calls = []

    async def async_call(self, domain, service, data, *, blocking=False):
        self.calls.append((domain, service, data, blocking))


class _Hass:
    def __init__(self, grid_status: str | None) -> None:
        self.states = _States()
        self.services = _Services()
        snap = types.SimpleNamespace(grid_status=grid_status)
        coord = types.SimpleNamespace(data=snap)
        self.data = {
            "power_sync": {
                "entry-1": {
                    "powerwall_local": {
                        "coordinator": coord,
                    }
                }
            }
        }


def _entry():
    return types.SimpleNamespace(entry_id="entry-1", data={}, options={})


def _switches(grid_status: str | None = "SystemGridConnected"):
    hass = _Hass(grid_status)
    entry = _entry()
    return (
        switch.OffGridSwitch(hass, entry),
        switch.OnGridSwitch(hass, entry),
        hass,
    )


def test_grid_mode_switches_are_mutually_exclusive_from_actual_state():
    off_grid, on_grid, hass = _switches("SystemGridConnected")

    assert off_grid.is_on is False
    assert on_grid.is_on is True

    coord = hass.data["power_sync"]["entry-1"]["powerwall_local"]["coordinator"]
    coord.data.grid_status = "SystemIslandedActive"

    assert off_grid.is_on is True
    assert on_grid.is_on is False


def test_off_grid_command_shares_pending_state_across_both_switches():
    off_grid, on_grid, hass = _switches("SystemGridConnected")

    asyncio.run(off_grid.async_turn_on())

    assert hass.services.calls == [
        ("power_sync", "powerwall_go_off_grid", {}, True),
    ]
    assert off_grid.is_on is True
    assert on_grid.is_on is False


def test_on_grid_command_reconnects_and_shares_pending_state():
    off_grid, on_grid, hass = _switches("SystemIslandedActive")

    asyncio.run(on_grid.async_turn_on())

    assert hass.services.calls == [
        ("power_sync", "powerwall_reconnect_grid", {}, True),
    ]
    assert on_grid.is_on is True
    assert off_grid.is_on is False


def test_force_switches_are_added_for_non_tesla_batteries():
    hass = _Hass("SystemGridConnected")
    entry = types.SimpleNamespace(
        entry_id="entry-1",
        data={"battery_system": "sigenergy"},
        options={},
    )
    added = []

    asyncio.run(switch.async_setup_entry(hass, entry, added.extend))

    assert any(isinstance(entity, switch.ForceDischargeSwitch) for entity in added)
    assert any(isinstance(entity, switch.ForceChargeSwitch) for entity in added)
    assert not any(isinstance(entity, switch.GridChargingSwitch) for entity in added)


def test_monitoring_switch_reads_updated_config_entry_options():
    hass = _Hass("SystemGridConnected")
    entry = types.SimpleNamespace(
        entry_id="entry-1",
        data={"monitoring_mode": True},
        options={"monitoring_mode": True},
    )
    monitoring_switch = switch.MonitoringModeSwitch(
        hass,
        entry,
        _SwitchEntityDescription(
            key="monitoring_mode",
            name="Monitoring Mode",
            icon="mdi:eye-outline",
        ),
    )

    assert monitoring_switch.is_on is True

    entry.options = {"monitoring_mode": False}
    monitoring_switch._handle_monitoring_mode_update(False)

    assert monitoring_switch.is_on is False
    assert monitoring_switch.write_count == 1


def test_force_discharge_switch_uses_selected_duration_and_force_power():
    hass = _Hass("SystemGridConnected")
    hass.states._states["number.power_sync_force_power_kw"] = _State("5.5")
    entry = types.SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={"force_discharge_duration": "90"},
    )
    force_switch = switch.ForceDischargeSwitch(
        hass,
        entry,
        _SwitchEntityDescription(
            key="force_discharge",
            name="Force Discharge",
            icon="mdi:battery-arrow-up",
        ),
    )

    asyncio.run(force_switch.async_turn_on())

    assert hass.services.calls == [
        ("power_sync", "force_discharge", {"duration": 90, "power_w": 5500}, True),
    ]


def test_force_charge_switch_uses_selected_duration_and_force_power():
    hass = _Hass("SystemGridConnected")
    hass.states._states["number.power_sync_force_power_kw"] = _State("4")
    entry = types.SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={"force_charge_duration": "120"},
    )
    force_switch = switch.ForceChargeSwitch(
        hass,
        entry,
        _SwitchEntityDescription(
            key="force_charge",
            name="Force Charge",
            icon="mdi:battery-arrow-down",
        ),
    )

    asyncio.run(force_switch.async_turn_on())

    assert hass.services.calls == [
        ("power_sync", "force_charge", {"duration": 120, "power_w": 4000}, True),
    ]


def test_force_switch_state_tracks_service_dispatch_payload():
    hass = _Hass("SystemGridConnected")
    entry = _entry()
    force_switch = switch.ForceDischargeSwitch(
        hass,
        entry,
        _SwitchEntityDescription(
            key="force_discharge",
            name="Force Discharge",
            icon="mdi:battery-arrow-up",
        ),
    )

    force_switch._handle_force_discharge_update({
        "active": True,
        "duration": 45,
        "expires_at": "2026-05-08T12:45:00+00:00",
    })

    assert force_switch.is_on is True
    assert force_switch.extra_state_attributes["duration_minutes"] == 45
    assert force_switch.extra_state_attributes["expires_at"] == "2026-05-08T12:45:00+00:00"

    force_switch._handle_force_discharge_update({"active": False})

    assert force_switch.is_on is False
    assert "expires_at" not in force_switch.extra_state_attributes
