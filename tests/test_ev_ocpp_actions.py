"""Tests for OCPP EV action fallbacks and ownership guards."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"


def _install_ha_stubs() -> None:
    ha_root = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
    ha_config_entries = sys.modules.setdefault(
        "homeassistant.config_entries", types.ModuleType("homeassistant.config_entries")
    )
    ha_core = sys.modules.setdefault("homeassistant.core", types.ModuleType("homeassistant.core"))
    ha_exceptions = sys.modules.setdefault(
        "homeassistant.exceptions", types.ModuleType("homeassistant.exceptions")
    )
    ha_helpers = sys.modules.setdefault(
        "homeassistant.helpers", types.ModuleType("homeassistant.helpers")
    )
    ha_storage = sys.modules.setdefault(
        "homeassistant.helpers.storage", types.ModuleType("homeassistant.helpers.storage")
    )
    ha_update = sys.modules.setdefault(
        "homeassistant.helpers.update_coordinator",
        types.ModuleType("homeassistant.helpers.update_coordinator"),
    )
    ha_er = sys.modules.setdefault(
        "homeassistant.helpers.entity_registry",
        types.ModuleType("homeassistant.helpers.entity_registry"),
    )
    ha_dr = sys.modules.setdefault(
        "homeassistant.helpers.device_registry",
        types.ModuleType("homeassistant.helpers.device_registry"),
    )
    ha_event = sys.modules.setdefault(
        "homeassistant.helpers.event", types.ModuleType("homeassistant.helpers.event")
    )
    ha_util = sys.modules.setdefault("homeassistant.util", types.ModuleType("homeassistant.util"))
    ha_dt = sys.modules.setdefault("homeassistant.util.dt", types.ModuleType("homeassistant.util.dt"))

    ha_core.HomeAssistant = type("HomeAssistant", (), {})
    ha_config_entries.ConfigEntry = type("ConfigEntry", (), {})
    ha_exceptions.ConfigEntryNotReady = type("ConfigEntryNotReady", (Exception,), {})
    ha_er.async_get = lambda hass: hass.entity_registry
    ha_dr.async_get = lambda hass: getattr(
        hass,
        "device_registry",
        SimpleNamespace(devices={}),
    )
    ha_storage.Store = type("Store", (), {"__init__": lambda self, *args, **kwargs: None})
    ha_update.DataUpdateCoordinator = type(
        "DataUpdateCoordinator",
        (),
        {
            "__class_getitem__": classmethod(lambda cls, item: cls),
            "__init__": lambda self, *args, **kwargs: None,
        },
    )
    ha_event.async_track_time_interval = lambda *args, **kwargs: (lambda: None)
    ha_event.async_track_time_change = lambda *args, **kwargs: (lambda: None)
    ha_event.async_track_point_in_time = lambda *args, **kwargs: (lambda: None)
    ha_dt.now = getattr(ha_dt, "now", lambda *args, **kwargs: None)
    ha_dt.utcnow = getattr(ha_dt, "utcnow", lambda *args, **kwargs: None)

    ha_helpers.entity_registry = ha_er
    ha_helpers.device_registry = ha_dr
    ha_helpers.storage = ha_storage
    ha_helpers.update_coordinator = ha_update
    ha_helpers.event = ha_event
    ha_util.dt = ha_dt
    ha_root.helpers = ha_helpers
    ha_root.util = ha_util


_install_ha_stubs()

_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps

_automations = types.ModuleType("power_sync.automations")
_automations.__path__ = [str(ROOT / "automations")]
sys.modules["power_sync.automations"] = _automations

if not hasattr(sys.modules.get("power_sync.const"), "CONF_EV_PROVIDER"):
    sys.modules.pop("power_sync.const", None)
sys.modules.pop("power_sync.automations.actions", None)
actions = importlib.import_module("power_sync.automations.actions")


@pytest.fixture(autouse=True)
def _reset_ev_action_module_state():
    """Keep mutable EV action state isolated from test execution order."""
    mutable_state = (
        actions._dynamic_ev_state,
        actions._dynamic_ev_update_locks,
        actions._phase_load_management_locks,
        actions._phase_load_management_targets,
        actions._ev_wake_lock,
        actions._ev_scheduled_stop,
    )
    for state in mutable_state:
        state.clear()
    yield
    for state in mutable_state:
        state.clear()


def _phase_managed_hass(*, currents=(20, 18, 17), age_seconds=1):
    now = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    states = [
        _State(
            f"sensor.grid_l{index}_current",
            str(current),
            {"unit_of_measurement": "A"},
            last_updated=now - timedelta(seconds=age_seconds),
        )
        for index, current in enumerate(currents, 1)
    ]
    hass = _Hass(states)
    hass.data["power_sync"]["entry-1"]["automation_store"] = SimpleNamespace(
        _data={
            "home_power_settings": {
                "phase_type": "three",
                "max_grid_import_amps": 32,
                "phase_load_management_enabled": True,
                "phase_current_entity_l1": "sensor.grid_l1_current",
                "phase_current_entity_l2": "sensor.grid_l2_current",
                "phase_current_entity_l3": "sensor.grid_l3_current",
                "phase_current_safety_margin_amps": 2,
            }
        }
    )
    return hass, now


def test_phase_management_clamps_owned_initial_target_to_worst_phase(monkeypatch):
    hass, now = _phase_managed_hass(currents=(20, 18, 17))
    monkeypatch.setattr(actions.dt_util, "utcnow", lambda: now)
    params = {
        "owner_mode": "scheduled",
        "charger_type": "generic",
        "phases": 3,
        "min_charge_amps": 6,
        "max_charge_amps": 32,
    }

    target = asyncio.run(actions._phase_load_managed_target_amps(
        hass, _Entry(), "charger", 16, params
    ))

    assert target == 10
    status = hass.data["power_sync"]["entry-1"]["phase_load_management_status"]
    assert status["limiting_phase"] == "l1"
    assert status["allocated_amps"] == 10


def test_phase_management_stale_data_stops_active_owned_charging(monkeypatch):
    hass, now = _phase_managed_hass(age_seconds=120)
    monkeypatch.setattr(actions.dt_util, "utcnow", lambda: now)
    params = {
        "owner_mode": "scheduled",
        "charger_type": "generic",
        "phases": 3,
        "min_charge_amps": 6,
        "max_charge_amps": 32,
    }
    actions._dynamic_ev_state["entry-1"] = {
        "charger": {
            "active": True,
            "current_amps": 16,
            "target_amps": 16,
            "params": params,
        }
    }
    commanded: list[int] = []

    async def record_command(hass, entry, vehicle_id, amps, command_params):
        commanded.append(amps)
        return True

    monkeypatch.setattr(actions, "_set_vehicle_amps_unchecked", record_command)

    assert asyncio.run(
        actions._set_vehicle_amps(hass, _Entry(), "charger", 16, params)
    ) is True
    assert commanded == [0]
    status = hass.data["power_sync"]["entry-1"]["phase_load_management_status"]
    assert status["available"] is False
    assert "stale" in status["telemetry_reason"]


def test_phase_management_never_clamps_manual_or_external_command(monkeypatch):
    hass, now = _phase_managed_hass(currents=(31, 31, 31))
    monkeypatch.setattr(actions.dt_util, "utcnow", lambda: now)
    commanded: list[int] = []

    async def record_command(hass, entry, vehicle_id, amps, command_params):
        commanded.append(amps)
        return True

    monkeypatch.setattr(actions, "_set_vehicle_amps_unchecked", record_command)

    assert asyncio.run(actions._set_vehicle_amps(
        hass,
        _Entry(),
        "charger",
        16,
        {"owner_mode": "manual", "charger_type": "generic"},
    )) is True
    assert asyncio.run(actions._set_vehicle_amps(
        hass,
        _Entry(),
        "external",
        20,
        {"charger_type": "generic"},
    )) is True
    assert commanded == [16, 20]


def test_phase_management_remembers_direct_owned_targets_without_meter_lag(monkeypatch):
    hass, now = _phase_managed_hass(currents=(14, 14, 14))
    monkeypatch.setattr(actions.dt_util, "utcnow", lambda: now)
    commanded: list[tuple[str, int]] = []

    async def record_command(hass, entry, vehicle_id, amps, command_params):
        commanded.append((vehicle_id, amps))
        return True

    monkeypatch.setattr(actions, "_set_vehicle_amps_unchecked", record_command)
    params = {
        "owner_mode": "smart_schedule",
        "charger_type": "generic",
        "phases": 3,
        "min_charge_amps": 6,
        "max_charge_amps": 32,
    }

    assert asyncio.run(
        actions._set_vehicle_amps(hass, _Entry(), "car-a", 10, dict(params))
    ) is True
    assert asyncio.run(
        actions._set_vehicle_amps(hass, _Entry(), "car-b", 10, dict(params))
    ) is True

    assert commanded == [("car-a", 10), ("car-b", 6)]
    assert sum(
        target["amps"]
        for target in actions._phase_load_management_targets["entry-1"].values()
    ) == 16


def test_phase_management_settings_change_clears_remembered_targets():
    hass, _now = _phase_managed_hass()
    actions._phase_load_management_targets["entry-1"] = {
        "car-a": {"amps": 10, "params": {"owner_mode": "scheduled"}}
    }

    actions.reset_phase_load_management_runtime(
        hass,
        "entry-1",
        enabled=False,
    )

    assert "entry-1" not in actions._phase_load_management_targets
    assert hass.data["power_sync"]["entry-1"]["phase_load_management_status"] == {
        "enabled": False,
        "available": None,
        "reason": "disabled",
    }


class _State:
    def __init__(
        self,
        entity_id: str,
        state: str,
        attributes: dict | None = None,
        *,
        last_changed: datetime | None = None,
        last_updated: datetime | None = None,
    ) -> None:
        self.entity_id = entity_id
        self.state = state
        self.attributes = attributes or {}
        self.last_changed = last_changed
        self.last_updated = last_updated or last_changed


class _States:
    def __init__(self, states: list[_State]) -> None:
        self._states = {state.entity_id: state for state in states}

    def get(self, entity_id: str):
        return self._states.get(entity_id)

    def async_entity_ids(self, domain: str | None = None):
        if domain is None:
            return list(self._states)
        return [entity_id for entity_id in self._states if entity_id.startswith(f"{domain}.")]

    def async_all(self, domain: str | None = None):
        if domain is None:
            return list(self._states.values())
        return [
            state for entity_id, state in self._states.items()
            if entity_id.startswith(f"{domain}.")
        ]


class _Services:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    async def async_call(self, domain: str, service: str, data: dict, blocking: bool = True):
        self.calls.append((domain, service, data))


class _Hass:
    def __init__(
        self,
        states: list[_State],
        registry_entities: dict[str, object] | None = None,
        registry_devices: dict[str, object] | None = None,
    ) -> None:
        self.data = {"power_sync": {"entry-1": {}}}
        self.states = _States(states)
        self.services = _Services()
        self.entity_registry = SimpleNamespace(entities=registry_entities or {})
        self.device_registry = SimpleNamespace(devices=registry_devices or {})


class _Entry:
    entry_id = "entry-1"
    data = {}
    options = {}


def _tesla_capability_hass(
    *,
    first_max: int = 32,
    second_max: int = 10,
    first_cable: str = "on",
    second_cable: str = "on",
):
    vin_a = "5YJTEST00000000A1"
    vin_b = "5YJTEST00000000B2"
    states = [
        _State("binary_sensor.car_a_charge_cable", first_cable),
        _State(
            "number.car_a_charge_current",
            str(first_max),
            {"min": 0, "max": first_max},
        ),
        _State("sensor.car_a_charger_voltage", "240", {"unit_of_measurement": "V"}),
        _State("sensor.car_a_charger_phases", "3"),
        _State("binary_sensor.car_b_charge_cable", second_cable),
        _State(
            "number.car_b_charge_current",
            str(second_max),
            {"min": 0, "max": second_max},
        ),
        _State("sensor.car_b_charger_voltage", "230", {"unit_of_measurement": "V"}),
        _State("sensor.car_b_charger_phases", "1"),
    ]
    registry_entities = {
        state.entity_id: SimpleNamespace(
            entity_id=state.entity_id,
            device_id="car-a" if "car_a" in state.entity_id else "car-b",
            platform="tesla_fleet",
        )
        for state in states
    }
    registry_devices = {
        "car-a": SimpleNamespace(
            id="car-a",
            identifiers={("tesla_fleet", vin_a)},
        ),
        "car-b": SimpleNamespace(
            id="car-b",
            identifiers={("tesla_fleet", vin_b)},
        ),
    }
    return _Hass(states, registry_entities, registry_devices), vin_a, vin_b


def test_tesla_active_charger_capability_is_vin_scoped_and_follows_swap():
    hass, vin_a, vin_b = _tesla_capability_hass()

    first = asyncio.run(
        actions._resolve_tesla_active_charger_capability(
            hass,
            _Entry(),
            vin_a,
        )
    )
    second = asyncio.run(
        actions._resolve_tesla_active_charger_capability(
            hass,
            _Entry(),
            vin_b,
        )
    )

    assert first == {
        "association_known": True,
        "capability_known": True,
        "max_charge_amps": 32,
        "max_charge_amps_source": "active_charger",
        "voltage": 240,
        "phases": 3,
    }
    assert second["max_charge_amps"] == 10
    assert second["voltage"] == 230
    assert second["phases"] == 1

    hass.states.get("number.car_a_charge_current").attributes["max"] = 10
    hass.states.get("number.car_b_charge_current").attributes["max"] = 32
    swapped_first = asyncio.run(
        actions._resolve_tesla_active_charger_capability(
            hass,
            _Entry(),
            vin_a,
        )
    )
    swapped_second = asyncio.run(
        actions._resolve_tesla_active_charger_capability(
            hass,
            _Entry(),
            vin_b,
        )
    )

    assert swapped_first["max_charge_amps"] == 10
    assert swapped_second["max_charge_amps"] == 32


def test_tesla_active_charger_capability_applies_lower_site_limit():
    hass, vin_a, _vin_b = _tesla_capability_hass(first_max=32)
    hass.data["power_sync"]["entry-1"]["automation_store"] = SimpleNamespace(
        _data={
            "home_power_settings": {
                "max_charge_speed_enabled": True,
                "max_amps_per_phase": 20,
            }
        }
    )

    capability = asyncio.run(
        actions._resolve_tesla_active_charger_capability(
            hass,
            _Entry(),
            vin_a,
        )
    )

    assert capability["max_charge_amps"] == 20
    assert capability["max_charge_amps_source"] == "active_charger_and_site_limit"


def test_tesla_active_charger_capability_uses_exact_wall_connector_over_stale_ble(
    monkeypatch,
):
    """An exact Wall Connector VIN fences out a prior BLE pilot limit."""
    hass, vin_a, _vin_b = _tesla_capability_hass(first_max=32)
    hass.states._states.update({
        "binary_sensor.teslable_charge_flap": _State(
            "binary_sensor.teslable_charge_flap",
            "on",
        ),
        "number.teslable_charging_amps": _State(
            "number.teslable_charging_amps",
            "5",
            {"min": 0, "max": 15},
        ),
    })
    monkeypatch.setattr(
        actions,
        "_resolve_ble_prefix_for_vehicle",
        lambda _hass, _entry, _vin: "teslable",
    )

    stale_ble_only = asyncio.run(
        actions._resolve_tesla_active_charger_capability(
            hass,
            _Entry(),
            vin_a,
            configured_max_amps=32,
        )
    )
    assert stale_ble_only["max_charge_amps"] == 15
    assert stale_ble_only["max_charge_amps_source"] == "active_charger"

    hass.states._states.update({
        "binary_sensor.tesla_wall_connector_vehicle_connected": _State(
            "binary_sensor.tesla_wall_connector_vehicle_connected",
            "on",
        ),
        "sensor.wall_connector_vehicle_2": _State(
            "sensor.wall_connector_vehicle_2",
            vin_a,
        ),
        "sensor.wall_connector_teslemetry_vehicle": _State(
            "sensor.wall_connector_teslemetry_vehicle",
            vin_a,
        ),
    })
    hass.entity_registry.entities.update({
        "binary_sensor.tesla_wall_connector_vehicle_connected": SimpleNamespace(
            entity_id="binary_sensor.tesla_wall_connector_vehicle_connected",
            device_id="wall-connector-local",
            platform="tesla_wall_connector",
        ),
        "sensor.wall_connector_vehicle_2": SimpleNamespace(
            entity_id="sensor.wall_connector_vehicle_2",
            device_id="wall-connector-fleet",
            platform="tesla_fleet",
        ),
        "sensor.wall_connector_teslemetry_vehicle": SimpleNamespace(
            entity_id="sensor.wall_connector_teslemetry_vehicle",
            device_id="wall-connector-teslemetry",
            platform="teslemetry",
        ),
    })
    hass.device_registry.devices.update({
        "wall-connector-local": SimpleNamespace(
            id="wall-connector-local",
            identifiers={("tesla_wall_connector", "WC-SERIAL-A")},
            serial_number="WC-SERIAL-A",
        ),
        "wall-connector-fleet": SimpleNamespace(
            id="wall-connector-fleet",
            identifiers={("tesla_fleet", "wall-connector-a")},
            serial_number="WC-SERIAL-A",
        ),
        "wall-connector-teslemetry": SimpleNamespace(
            id="wall-connector-teslemetry",
            identifiers={("teslemetry", "wall-connector-a")},
            serial_number="WC-SERIAL-A",
        ),
    })

    hass.states._states[
        "binary_sensor.tesla_wall_connector_vehicle_connected"
    ].state = "off"
    disconnected_wall_connector = asyncio.run(
        actions._resolve_tesla_active_charger_capability(
            hass,
            _Entry(),
            vin_a,
            configured_max_amps=32,
        )
    )
    assert disconnected_wall_connector["max_charge_amps"] == 15
    assert "allow_stale_entity_max_override" not in disconnected_wall_connector

    hass.states._states[
        "binary_sensor.tesla_wall_connector_vehicle_connected"
    ].state = "on"
    exact_wall_connector = asyncio.run(
        actions._resolve_tesla_active_charger_capability(
            hass,
            _Entry(),
            vin_a,
            configured_max_amps=32,
        )
    )

    assert exact_wall_connector == {
        "association_known": True,
        "capability_known": True,
        "max_charge_amps": 32,
        "max_charge_amps_source": "active_wall_connector_vehicle",
        "voltage": 240,
        "phases": 3,
        "allow_stale_entity_max_override": True,
        "prefer_vin_scoped_current_control": True,
    }

    configured_limit = asyncio.run(
        actions._resolve_tesla_active_charger_capability(
            hass,
            _Entry(),
            vin_a,
            configured_max_amps=24,
        )
    )
    assert configured_limit["max_charge_amps"] == 24
    assert configured_limit["max_charge_amps_source"] == (
        "active_wall_connector_vehicle_and_configured_limit"
    )
    assert configured_limit["allow_stale_entity_max_override"] is True


def test_tesla_active_charger_capability_keeps_ble_cap_for_conflicting_connector_vins(
    monkeypatch,
):
    """Conflicting Wall Connector identities cannot lift the BLE cap."""
    hass, vin_a, vin_b = _tesla_capability_hass(first_max=32)
    hass.states._states.update({
        "binary_sensor.teslable_charge_flap": _State(
            "binary_sensor.teslable_charge_flap",
            "on",
        ),
        "number.teslable_charging_amps": _State(
            "number.teslable_charging_amps",
            "5",
            {"min": 0, "max": 15},
        ),
        "binary_sensor.tesla_wall_connector_vehicle_connected": _State(
            "binary_sensor.tesla_wall_connector_vehicle_connected",
            "on",
        ),
        "sensor.wall_connector_vehicle_2": _State(
            "sensor.wall_connector_vehicle_2",
            vin_a,
        ),
        "sensor.wall_connector_teslemetry_vehicle": _State(
            "sensor.wall_connector_teslemetry_vehicle",
            vin_b,
        ),
    })
    hass.entity_registry.entities.update({
        "binary_sensor.tesla_wall_connector_vehicle_connected": SimpleNamespace(
            entity_id="binary_sensor.tesla_wall_connector_vehicle_connected",
            device_id="wall-connector-local",
            platform="tesla_wall_connector",
        ),
        "sensor.wall_connector_vehicle_2": SimpleNamespace(
            entity_id="sensor.wall_connector_vehicle_2",
            device_id="wall-connector-fleet",
            platform="tesla_fleet",
        ),
        "sensor.wall_connector_teslemetry_vehicle": SimpleNamespace(
            entity_id="sensor.wall_connector_teslemetry_vehicle",
            device_id="wall-connector-teslemetry",
            platform="teslemetry",
        ),
    })
    hass.device_registry.devices.update({
        "wall-connector-local": SimpleNamespace(
            id="wall-connector-local",
            identifiers={("tesla_wall_connector", "WC-SERIAL-A")},
            serial_number="WC-SERIAL-A",
        ),
        "wall-connector-fleet": SimpleNamespace(
            id="wall-connector-fleet",
            identifiers={("tesla_fleet", "wall-connector-a")},
            serial_number="WC-SERIAL-A",
        ),
        "wall-connector-teslemetry": SimpleNamespace(
            id="wall-connector-teslemetry",
            identifiers={("teslemetry", "wall-connector-a")},
            serial_number="WC-SERIAL-A",
        ),
    })
    monkeypatch.setattr(
        actions,
        "_resolve_ble_prefix_for_vehicle",
        lambda _hass, _entry, _vin: "teslable",
    )

    capability = asyncio.run(
        actions._resolve_tesla_active_charger_capability(
            hass,
            _Entry(),
            vin_a,
            configured_max_amps=32,
        )
    )

    assert capability["max_charge_amps"] == 15
    assert capability["max_charge_amps_source"] == "active_charger"
    assert "allow_stale_entity_max_override" not in capability


def test_tesla_active_charger_capability_supports_multiple_wall_connectors(
    monkeypatch,
):
    """Each connected Wall Connector is associated by its physical serial."""
    hass, vin_a, vin_b = _tesla_capability_hass(first_max=32, second_max=32)
    hass.states._states.update({
        "binary_sensor.garage_wall_connector_vehicle_connected": _State(
            "binary_sensor.garage_wall_connector_vehicle_connected",
            "on",
        ),
        "binary_sensor.driveway_wall_connector_vehicle_connected": _State(
            "binary_sensor.driveway_wall_connector_vehicle_connected",
            "on",
        ),
        "sensor.garage_wall_connector_vehicle": _State(
            "sensor.garage_wall_connector_vehicle",
            vin_a,
        ),
        "sensor.driveway_wall_connector_vehicle": _State(
            "sensor.driveway_wall_connector_vehicle",
            vin_b,
        ),
        "binary_sensor.teslable_charge_flap": _State(
            "binary_sensor.teslable_charge_flap",
            "on",
        ),
        "number.teslable_charging_amps": _State(
            "number.teslable_charging_amps",
            "5",
            {"min": 0, "max": 15},
        ),
    })
    hass.entity_registry.entities.update({
        "binary_sensor.garage_wall_connector_vehicle_connected": SimpleNamespace(
            entity_id="binary_sensor.garage_wall_connector_vehicle_connected",
            device_id="garage-wall-connector-local",
            platform="tesla_wall_connector",
        ),
        "binary_sensor.driveway_wall_connector_vehicle_connected": SimpleNamespace(
            entity_id="binary_sensor.driveway_wall_connector_vehicle_connected",
            device_id="driveway-wall-connector-local",
            platform="tesla_wall_connector",
        ),
        "sensor.garage_wall_connector_vehicle": SimpleNamespace(
            entity_id="sensor.garage_wall_connector_vehicle",
            device_id="garage-wall-connector-fleet",
            platform="tesla_fleet",
        ),
        "sensor.driveway_wall_connector_vehicle": SimpleNamespace(
            entity_id="sensor.driveway_wall_connector_vehicle",
            device_id="driveway-wall-connector-fleet",
            platform="tesla_fleet",
        ),
    })
    hass.device_registry.devices.update({
        "garage-wall-connector-local": SimpleNamespace(
            id="garage-wall-connector-local",
            identifiers={("tesla_wall_connector", "WC-SERIAL-A")},
            serial_number="WC-SERIAL-A",
        ),
        "driveway-wall-connector-local": SimpleNamespace(
            id="driveway-wall-connector-local",
            identifiers={("tesla_wall_connector", "WC-SERIAL-B")},
            serial_number="WC-SERIAL-B",
        ),
        "garage-wall-connector-fleet": SimpleNamespace(
            id="garage-wall-connector-fleet",
            identifiers={("tesla_fleet", "wall-connector-a")},
            serial_number="WC-SERIAL-A",
        ),
        "driveway-wall-connector-fleet": SimpleNamespace(
            id="driveway-wall-connector-fleet",
            identifiers={("tesla_fleet", "wall-connector-b")},
            serial_number="WC-SERIAL-B",
        ),
    })
    monkeypatch.setattr(
        actions,
        "_resolve_ble_prefix_for_vehicle",
        lambda _hass, _entry, _vin: "teslable",
    )

    first = asyncio.run(
        actions._resolve_tesla_active_charger_capability(
            hass,
            _Entry(),
            vin_a,
            configured_max_amps=32,
        )
    )
    second = asyncio.run(
        actions._resolve_tesla_active_charger_capability(
            hass,
            _Entry(),
            vin_b,
            configured_max_amps=32,
        )
    )

    assert first["max_charge_amps"] == 32
    assert second["max_charge_amps"] == 32
    assert first["max_charge_amps_source"] == "active_wall_connector_vehicle"
    assert second["max_charge_amps_source"] == "active_wall_connector_vehicle"
    assert first["prefer_vin_scoped_current_control"] is True
    assert second["prefer_vin_scoped_current_control"] is True


def test_tesla_active_charger_capability_fails_closed_when_unplugged_or_unavailable():
    hass, vin_a, _vin_b = _tesla_capability_hass(first_cable="off")
    unplugged = asyncio.run(
        actions._resolve_tesla_active_charger_capability(
            hass,
            _Entry(),
            vin_a,
        )
    )
    assert unplugged["association_known"] is False
    assert unplugged["max_charge_amps"] == 5
    assert unplugged["max_charge_amps_source"] == "safe_unplugged"

    hass.states.get("binary_sensor.car_a_charge_cable").state = "on"
    hass.states.get("number.car_a_charge_current").state = "unavailable"
    unavailable = asyncio.run(
        actions._resolve_tesla_active_charger_capability(
            hass,
            _Entry(),
            vin_a,
        )
    )
    assert unavailable["association_known"] is True
    assert unavailable["capability_known"] is False
    assert unavailable["max_charge_amps"] == 5
    assert unavailable["max_charge_amps_source"] == (
        "safe_unavailable_charger_capability"
    )


def test_tesla_active_charger_capability_rejects_conflicting_sources_and_unknown_vin():
    hass, vin_a, _vin_b = _tesla_capability_hass()
    hass.device_registry.devices["car-a-stale"] = SimpleNamespace(
        id="car-a-stale",
        identifiers={("teslemetry", vin_a)},
    )
    hass.entity_registry.entities["binary_sensor.car_a_stale_charge_cable"] = (
        SimpleNamespace(
            entity_id="binary_sensor.car_a_stale_charge_cable",
            device_id="car-a-stale",
        )
    )
    hass.entity_registry.entities["number.car_a_stale_charge_current"] = (
        SimpleNamespace(
            entity_id="number.car_a_stale_charge_current",
            device_id="car-a-stale",
        )
    )
    hass.states._states["binary_sensor.car_a_stale_charge_cable"] = _State(
        "binary_sensor.car_a_stale_charge_cable",
        "off",
    )
    hass.states._states["number.car_a_stale_charge_current"] = _State(
        "number.car_a_stale_charge_current",
        "10",
        {"min": 0, "max": 10},
    )

    ambiguous = asyncio.run(
        actions._resolve_tesla_active_charger_capability(
            hass,
            _Entry(),
            vin_a,
        )
    )
    unknown = asyncio.run(
        actions._resolve_tesla_active_charger_capability(
            hass,
            _Entry(),
            "5YJTEST00000000C3",
        )
    )

    assert ambiguous["max_charge_amps"] == 5
    assert ambiguous["max_charge_amps_source"] == "safe_ambiguous_charger"
    assert unknown["association_known"] is False
    assert unknown["max_charge_amps"] == 5


def test_tesla_active_charger_capability_ignores_clearly_stale_plug_conflict():
    now = datetime.now(timezone.utc)
    hass, vin_a, _vin_b = _tesla_capability_hass(first_max=10)
    hass.states.get("binary_sensor.car_a_charge_cable").last_changed = now
    hass.states.get("binary_sensor.car_a_charge_cable").last_updated = now
    hass.device_registry.devices["car-a-stale"] = SimpleNamespace(
        id="car-a-stale",
        identifiers={("tesla_fleet", vin_a)},
    )
    for entity_id in (
        "binary_sensor.car_a_stale_charge_cable",
        "number.car_a_stale_charge_current",
    ):
        hass.entity_registry.entities[entity_id] = SimpleNamespace(
            entity_id=entity_id,
            device_id="car-a-stale",
        )
    hass.states._states["binary_sensor.car_a_stale_charge_cable"] = _State(
        "binary_sensor.car_a_stale_charge_cable",
        "off",
        last_updated=now - timedelta(minutes=10),
    )
    hass.states._states["number.car_a_stale_charge_current"] = _State(
        "number.car_a_stale_charge_current",
        "32",
        {"min": 0, "max": 32},
    )

    capability = asyncio.run(
        actions._resolve_tesla_active_charger_capability(
            hass,
            _Entry(),
            vin_a,
        )
    )

    assert capability["association_known"] is True
    assert capability["capability_known"] is True
    assert capability["max_charge_amps"] == 10
    assert capability["max_charge_amps_source"] == "active_charger"


def test_tesla_active_charger_capability_keeps_recent_conflict_fail_closed():
    now = datetime.now(timezone.utc)
    hass, vin_a, _vin_b = _tesla_capability_hass(first_max=10)
    hass.states.get("binary_sensor.car_a_charge_cable").last_changed = now
    hass.states.get("binary_sensor.car_a_charge_cable").last_updated = now
    hass.device_registry.devices["car-a-conflict"] = SimpleNamespace(
        id="car-a-conflict",
        identifiers={("teslemetry", vin_a)},
    )
    hass.entity_registry.entities[
        "binary_sensor.car_a_conflict_charge_cable"
    ] = SimpleNamespace(
        entity_id="binary_sensor.car_a_conflict_charge_cable",
        device_id="car-a-conflict",
    )
    hass.states._states["binary_sensor.car_a_conflict_charge_cable"] = _State(
        "binary_sensor.car_a_conflict_charge_cable",
        "off",
        last_updated=now - timedelta(seconds=30),
    )

    capability = asyncio.run(
        actions._resolve_tesla_active_charger_capability(
            hass,
            _Entry(),
            vin_a,
        )
    )

    assert capability["association_known"] is False
    assert capability["max_charge_amps"] == 5
    assert capability["max_charge_amps_source"] == "safe_ambiguous_charger"


def test_tesla_active_charger_capability_prefers_fresh_unplugged_state():
    now = datetime.now(timezone.utc)
    hass, vin_a, _vin_b = _tesla_capability_hass(
        first_max=10,
        first_cable="off",
    )
    hass.states.get("binary_sensor.car_a_charge_cable").last_changed = now
    hass.states.get("binary_sensor.car_a_charge_cable").last_updated = now
    hass.device_registry.devices["car-a-stale"] = SimpleNamespace(
        id="car-a-stale",
        identifiers={("teslemetry", vin_a)},
    )
    hass.entity_registry.entities[
        "binary_sensor.car_a_stale_charge_cable"
    ] = SimpleNamespace(
        entity_id="binary_sensor.car_a_stale_charge_cable",
        device_id="car-a-stale",
    )
    hass.states._states["binary_sensor.car_a_stale_charge_cable"] = _State(
        "binary_sensor.car_a_stale_charge_cable",
        "on",
        last_updated=now - timedelta(minutes=10),
    )

    capability = asyncio.run(
        actions._resolve_tesla_active_charger_capability(
            hass,
            _Entry(),
            vin_a,
        )
    )

    assert capability["association_known"] is False
    assert capability["max_charge_amps"] == 5
    assert capability["max_charge_amps_source"] == "safe_unplugged"


def _tesla_confirmation_hass():
    vin_a = "5YJTEST00000000A1"
    vin_b = "5YJTEST00000000B2"
    states = [
        _State("sensor.car_a_charging", "stopped"),
        _State("sensor.car_a_charger_actual_current", "0", {"unit_of_measurement": "A"}),
        _State("switch.car_a_charge", "off"),
        _State("sensor.car_b_charging", "charging"),
        _State("sensor.car_b_charger_actual_current", "15", {"unit_of_measurement": "A"}),
    ]
    registry_entities = {
        state.entity_id: SimpleNamespace(
            entity_id=state.entity_id,
            device_id="car-a" if "car_a" in state.entity_id else "car-b",
        )
        for state in states
    }
    registry_devices = {
        "car-a": SimpleNamespace(
            id="car-a",
            identifiers={("tesla_fleet", vin_a)},
        ),
        "car-b": SimpleNamespace(
            id="car-b",
            identifiers={("tesla_fleet", vin_b)},
        ),
    }
    return _Hass(states, registry_entities, registry_devices), vin_a, vin_b


def test_tesla_physical_start_requires_fresh_state_and_measured_draw():
    hass, vin_a, _vin_b = _tesla_confirmation_hass()
    command_started_at = datetime.now(timezone.utc)
    baseline = actions._tesla_physical_charging_snapshot(
        hass,
        _Entry(),
        vin_a,
        {},
    )

    # A start-control switch changing to on is not measured charging evidence.
    hass.states.get("switch.car_a_charge").state = "on"
    hass.states.get("sensor.car_a_charging").state = "charging"
    no_draw = asyncio.run(
        actions._wait_for_tesla_physical_start(
            hass,
            _Entry(),
            vin_a,
            {},
            baseline,
            command_started_at,
            timeout_seconds=0,
        )
    )
    assert no_draw[0] is False

    # Fresh VIN-scoped actual current plus the charging-state transition proves
    # the vehicle physically started; the 15A value is observation, not a cap
    # override or proof that PowerSync set the limit.
    actual_current = hass.states.get("sensor.car_a_charger_actual_current")
    actual_current.state = "15"
    actual_current.last_updated = command_started_at + timedelta(seconds=1)
    confirmed = asyncio.run(
        actions._wait_for_tesla_physical_start(
            hass,
            _Entry(),
            vin_a,
            {},
            baseline,
            command_started_at,
            timeout_seconds=0,
        )
    )
    assert confirmed[0] is True
    assert "15.0A" in confirmed[1]


def test_tesla_physical_start_allows_delayed_cloud_draw(monkeypatch):
    """A real start must not be cancelled at the former 90-second boundary."""
    hass, vin_a, _vin_b = _tesla_confirmation_hass()
    command_started_at = datetime.now(timezone.utc)
    baseline = actions._tesla_physical_charging_snapshot(
        hass,
        _Entry(),
        vin_a,
        {},
    )
    elapsed = [0.0]

    class _Clock:
        @staticmethod
        def time():
            return elapsed[0]

    async def advance(seconds):
        elapsed[0] += seconds
        if elapsed[0] >= 100:
            charging = hass.states.get("sensor.car_a_charging")
            charging.state = "charging"
            charging.last_updated = command_started_at + timedelta(
                seconds=elapsed[0]
            )
        if elapsed[0] >= 120:
            current = hass.states.get("sensor.car_a_charger_actual_current")
            current.state = "10"
            current.last_updated = command_started_at + timedelta(
                seconds=elapsed[0]
            )

    monkeypatch.setattr(actions.asyncio, "get_running_loop", lambda: _Clock())
    monkeypatch.setattr(actions.asyncio, "sleep", advance)

    confirmed = asyncio.run(
        actions._wait_for_tesla_physical_start(
            hass,
            _Entry(),
            vin_a,
            {},
            baseline,
            command_started_at,
        )
    )

    assert actions._TESLA_START_CONFIRMATION_TIMEOUT_SECONDS == 150
    assert confirmed[0] is True
    assert "10.0A" in confirmed[1]
    assert elapsed[0] == 120


def test_tesla_physical_start_rejects_stale_and_other_vin_telemetry():
    hass, vin_a, vin_b = _tesla_confirmation_hass()
    command_started_at = datetime.now(timezone.utc)

    # VIN B is actively drawing 15A, but cannot confirm VIN A's start.
    baseline_a = actions._tesla_physical_charging_snapshot(
        hass,
        _Entry(),
        vin_a,
        {},
    )
    other_vin = asyncio.run(
        actions._wait_for_tesla_physical_start(
            hass,
            _Entry(),
            vin_a,
            {"tesla_charging_state_entity": "sensor.car_b_charging"},
            baseline_a,
            command_started_at,
            timeout_seconds=0,
        )
    )
    assert other_vin[0] is False

    # An unchanged pre-command charging/current record is stale evidence until
    # one of its VIN-scoped measurements refreshes after this request.
    baseline_b = actions._tesla_physical_charging_snapshot(
        hass,
        _Entry(),
        vin_b,
        {},
    )
    stale = asyncio.run(
        actions._wait_for_tesla_physical_start(
            hass,
            _Entry(),
            vin_b,
            {},
            baseline_b,
            command_started_at,
            timeout_seconds=0,
        )
    )
    assert stale[0] is False


def test_tesla_physical_start_rejects_fresh_state_with_stale_draw():
    hass, vin_a, _vin_b = _tesla_confirmation_hass()
    now = datetime.now(timezone.utc)
    stale_current = hass.states.get("sensor.car_a_charger_actual_current")
    stale_current.state = "32"
    stale_current.last_updated = now - timedelta(minutes=2)
    baseline = actions._tesla_physical_charging_snapshot(
        hass,
        _Entry(),
        vin_a,
        {},
    )

    charging = hass.states.get("sensor.car_a_charging")
    charging.state = "charging"
    charging.last_updated = now + timedelta(seconds=1)

    confirmed = asyncio.run(
        actions._wait_for_tesla_physical_start(
            hass,
            _Entry(),
            vin_a,
            {},
            baseline,
            now,
            timeout_seconds=0,
        )
    )

    assert confirmed[0] is False


def test_tesla_physical_snapshot_prefers_fresh_stopped_provider():
    """A stale cloud charging sample must not suppress a required restart."""
    hass, vin_a, _vin_b = _tesla_confirmation_hass()
    now = datetime.now(timezone.utc)

    stale_charging = hass.states.get("sensor.car_a_charging")
    stale_charging.state = "charging"
    stale_charging.last_updated = now - timedelta(seconds=30)
    stale_current = hass.states.get("sensor.car_a_charger_actual_current")
    stale_current.state = "32"
    stale_current.last_updated = now - timedelta(seconds=30)

    hass.device_registry.devices["car-a-fresh"] = SimpleNamespace(
        id="car-a-fresh",
        identifiers={("teslemetry", vin_a)},
    )
    for entity_id, value in (
        ("sensor.car_a_fresh_charging", "stopped"),
        ("sensor.car_a_fresh_charger_actual_current", "0"),
    ):
        hass.entity_registry.entities[entity_id] = SimpleNamespace(
            entity_id=entity_id,
            device_id="car-a-fresh",
        )
        hass.states._states[entity_id] = _State(
            entity_id,
            value,
            last_updated=now - timedelta(seconds=1),
        )

    snapshot = actions._tesla_physical_charging_snapshot(
        hass,
        _Entry(),
        vin_a,
        {},
    )

    assert snapshot["charging"] is False
    assert "sensor.car_a_charger_actual_current=32.0A" in snapshot["measurements"]


def test_tesla_physical_snapshot_fails_closed_on_recent_provider_conflict():
    hass, vin_a, _vin_b = _tesla_confirmation_hass()
    now = datetime.now(timezone.utc)
    older_stopped = hass.states.get("sensor.car_a_charging")
    older_stopped.last_updated = now - timedelta(seconds=30)

    hass.device_registry.devices["car-a-fresh"] = SimpleNamespace(
        id="car-a-fresh",
        identifiers={("teslemetry", vin_a)},
    )
    fresh_charging_id = "sensor.car_a_fresh_charging"
    hass.entity_registry.entities[fresh_charging_id] = SimpleNamespace(
        entity_id=fresh_charging_id,
        device_id="car-a-fresh",
    )
    hass.states._states[fresh_charging_id] = _State(
        fresh_charging_id,
        "charging",
        last_updated=now,
    )

    snapshot = actions._tesla_physical_charging_snapshot(
        hass,
        _Entry(),
        vin_a,
        {},
    )

    assert snapshot["charging"] is False


def test_unconfirmed_tesla_dynamic_start_creates_no_runtime_or_ownership(
    monkeypatch,
):
    vin = "5YJTEST00000000A1"
    timer_calls: list[tuple] = []
    stop_calls: list[dict] = []

    async def active_capability(*args, **kwargs):
        return {
            "association_known": True,
            "capability_known": True,
            "max_charge_amps": 15,
            "max_charge_amps_source": "active_charger",
            "voltage": 230,
            "phases": 1,
        }

    async def none_result(*args, **kwargs):
        return None

    async def true_result(*args, **kwargs):
        return True

    async def unconfirmed_result(*args, **kwargs):
        return False, "no fresh VIN-scoped charging state and measured draw"

    async def record_stop(_hass, _entry, params, *_args, **_kwargs):
        stop_calls.append(params)
        return True

    monkeypatch.setattr(
        actions,
        "_resolve_tesla_active_charger_capability",
        active_capability,
    )
    monkeypatch.setattr(
        actions,
        "_resolve_tesla_charge_current_entity",
        none_result,
    )
    monkeypatch.setattr(
        actions,
        "_tesla_vehicle_away_location",
        none_result,
    )
    monkeypatch.setattr(
        actions,
        "_action_start_ev_charging",
        true_result,
    )
    monkeypatch.setattr(
        actions,
        "_set_vehicle_amps",
        true_result,
    )
    monkeypatch.setattr(
        actions,
        "_wait_for_tesla_physical_start",
        unconfirmed_result,
    )
    monkeypatch.setattr(
        actions,
        "_action_stop_ev_charging",
        record_stop,
    )
    monkeypatch.setattr(
        actions,
        "async_track_time_interval",
        lambda *args, **kwargs: timer_calls.append((args, kwargs)),
    )

    hass = _Hass([])
    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_id": vin,
                "vehicle_vin": vin,
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
                "min_charge_amps": 5,
                "max_charge_amps": 15,
                "fixed_charge_amps": 15,
                "require_physical_start_confirmation": True,
            },
        )
    )

    assert result is False
    assert actions._dynamic_ev_state == {}
    assert timer_calls == []
    assert len(stop_calls) == 1
    assert stop_calls[0]["vehicle_id"] == vin
    assert stop_calls[0]["vehicle_vin"] == vin
    assert stop_calls[0]["_force_tesla_stop_request"] is True
    assert hass.data["power_sync"]["entry-1"].get("ev_ownership") in (None, {})
    command = hass.data["power_sync"]["entry-1"]["ev_last_command"][vin]
    assert command["success"] is False
    assert "physical charging was not confirmed" in command["reason"]
    assert "compensating stop request accepted" in command["reason"]


def test_already_charging_tesla_is_recovered_without_restart_or_stop(
    monkeypatch,
):
    vin = "5YJTEST00000000A1"
    timer_calls: list[tuple] = []
    start_calls: list[tuple] = []
    stop_calls: list[tuple] = []

    async def active_capability(*args, **kwargs):
        return {
            "association_known": True,
            "capability_known": True,
            "max_charge_amps": 15,
            "max_charge_amps_source": "active_charger",
            "voltage": 230,
            "phases": 1,
        }

    async def none_result(*args, **kwargs):
        return None

    async def true_result(*args, **kwargs):
        return True

    async def unexpected_start(*args, **kwargs):
        start_calls.append((args, kwargs))
        return True

    async def unexpected_stop(*args, **kwargs):
        stop_calls.append((args, kwargs))
        return True

    async def unexpected_wait(*args, **kwargs):
        raise AssertionError("already-active telemetry must not wait for a transition")

    monkeypatch.setattr(
        actions,
        "_resolve_tesla_active_charger_capability",
        active_capability,
    )
    monkeypatch.setattr(
        actions,
        "_resolve_tesla_charge_current_entity",
        none_result,
    )
    monkeypatch.setattr(actions, "_tesla_vehicle_away_location", none_result)
    monkeypatch.setattr(actions, "_action_start_ev_charging", unexpected_start)
    monkeypatch.setattr(actions, "_action_stop_ev_charging", unexpected_stop)
    monkeypatch.setattr(actions, "_set_vehicle_amps", true_result)
    monkeypatch.setattr(
        actions,
        "_tesla_physical_charging_snapshot",
        lambda *args, **kwargs: {
            "charging": True,
            "measurements": frozenset({"sensor.car_a_charger_current=15.0A"}),
            "fresh_measurements": frozenset(),
        },
    )
    monkeypatch.setattr(
        actions,
        "_wait_for_tesla_physical_start",
        unexpected_wait,
    )
    monkeypatch.setattr(
        actions,
        "async_track_time_interval",
        lambda *args, **kwargs: timer_calls.append((args, kwargs)),
    )

    hass = _Hass([])
    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_id": vin,
                "vehicle_vin": vin,
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
                "min_charge_amps": 5,
                "max_charge_amps": 15,
                "fixed_charge_amps": 15,
                "require_physical_start_confirmation": True,
            },
        )
    )

    assert result is True
    assert start_calls == []
    assert stop_calls == []
    assert len(timer_calls) == 1
    assert actions._dynamic_ev_state["entry-1"][vin]["active"] is True
    ownership = hass.data["power_sync"]["entry-1"]["ev_ownership"]
    assert ownership[vin]["owner_mode"] == "smart_schedule"


def test_dynamic_tesla_capability_tracks_selected_current_and_state_in_both_mode(
    monkeypatch,
):
    vehicle_id = "LRW3F7FS1NC484342"
    state = _solar_surplus_state(current_amps=0)
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {vehicle_id: state}

    async def active_capability(*args, **kwargs):
        return {
            "max_charge_amps": 32,
            "max_charge_amps_source": "active_charger",
            "voltage": 240,
            "phases": 1,
            "association_known": True,
            "capability_known": True,
        }

    async def tesla_entity(_hass, pattern, _vin, **kwargs):
        if pattern.startswith("number"):
            return "number.n3bula_charge_current"
        if pattern.startswith("sensor"):
            return "sensor.n3bula_charging"
        return None

    monkeypatch.setattr(
        actions,
        "_resolve_tesla_active_charger_capability",
        active_capability,
    )
    monkeypatch.setattr(actions, "_get_tesla_ev_entity", tesla_entity)
    monkeypatch.setattr(
        actions,
        "_get_ev_config",
        lambda *_args: {"ev_provider": actions.EV_PROVIDER_BOTH},
    )

    result = asyncio.run(
        actions._refresh_dynamic_tesla_charger_capability(
            _Hass([]),
            _Entry(),
            "entry-1",
            vehicle_id,
            state,
        )
    )

    assert result is True
    assert state["params"]["tesla_charge_current_entity"] == (
        "number.n3bula_charge_current"
    )
    assert state["params"]["tesla_charging_state_entity"] == (
        "sensor.n3bula_charging"
    )


def _tesla_entry():
    return SimpleNamespace(
        entry_id="entry-1",
        data={"battery_system": "tesla"},
        options={},
    )


def test_tesla_preserve_charge_holds_current_soc_with_backup_reserve():
    hass = _Hass([])
    hass.data["power_sync"]["entry-1"]["tesla_coordinator"] = SimpleNamespace(
        data={"battery_level": 63.4}
    )

    result = asyncio.run(actions._action_preserve_charge(hass, _tesla_entry()))

    assert result is True
    assert hass.services.calls == [
        (
            "power_sync",
            "set_backup_reserve",
            {"percent": 63, "source": "automation_preserve_charge"},
        )
    ]


def test_tesla_preserve_charge_caps_unsupported_mid_80s_soc_to_80_percent():
    hass = _Hass([])
    hass.data["power_sync"]["entry-1"]["tesla_coordinator"] = SimpleNamespace(
        data={"battery_level": 91}
    )

    result = asyncio.run(actions._action_preserve_charge(hass, _tesla_entry()))

    assert result is True
    assert hass.services.calls == [
        (
            "power_sync",
            "set_backup_reserve",
            {"percent": 80, "source": "automation_preserve_charge"},
        )
    ]


def test_tesla_preserve_charge_uses_100_percent_when_already_full():
    hass = _Hass([])
    hass.data["power_sync"]["entry-1"]["tesla_coordinator"] = SimpleNamespace(
        data={"battery_level": 99}
    )

    result = asyncio.run(actions._action_preserve_charge(hass, _tesla_entry()))

    assert result is True
    assert hass.services.calls == [
        (
            "power_sync",
            "set_backup_reserve",
            {"percent": 100, "source": "automation_preserve_charge"},
        )
    ]


def test_tesla_preserve_charge_fails_without_home_battery_soc():
    hass = _Hass([])

    result = asyncio.run(actions._action_preserve_charge(hass, _tesla_entry()))

    assert result is False
    assert hass.services.calls == []


def test_tesla_grid_export_supports_legacy_entry_without_battery_system():
    """Pre-multi-brand Tesla entries implicitly use the Tesla battery system."""
    hass = _Hass([])
    legacy_entry = SimpleNamespace(entry_id="entry-1", data={}, options={})

    result = asyncio.run(
        actions._action_set_grid_export(
            hass,
            legacy_entry,
            {"rule": "pv_only"},
        )
    )

    assert result is True
    assert hass.services.calls == [
        (
            "power_sync",
            "set_grid_export",
            {"rule": "pv_only", "source": "automation"},
        )
    ]


def test_tesla_grid_export_respects_explicit_non_tesla_option():
    hass = _Hass([])
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={"battery_system": "sungrow"},
    )

    result = asyncio.run(
        actions._action_set_grid_export(
            hass,
            entry,
            {"rule": "pv_only"},
        )
    )

    assert result is None
    assert hass.services.calls == []


def test_tesla_stop_accepts_numbered_teslemetry_charge_switch(monkeypatch):
    vin = "LRWYHCEKXTC687964"
    device = SimpleNamespace(
        id="device-yf88",
        name="",
        identifiers={("teslemetry", vin)},
    )
    hass = _Hass(
        [_State("switch.charge_2", "on")],
        registry_entities={
            "switch.charge_2": SimpleNamespace(
                entity_id="switch.charge_2",
                device_id="device-yf88",
            ),
            "binary_sensor.charge_cable": SimpleNamespace(
                entity_id="binary_sensor.charge_cable",
                device_id="device-yf88",
            ),
        },
    )

    monkeypatch.setattr(
        actions.dr,
        "async_get",
        lambda hass: SimpleNamespace(devices={"device-yf88": device}),
    )

    async def wake_success(*args, **kwargs):
        return True

    monkeypatch.setattr(actions, "_wake_tesla_ev", wake_success)

    result = asyncio.run(
        actions._action_stop_ev_charging(
            hass,
            _tesla_entry(),
            {"charger_type": "tesla", "vehicle_vin": vin},
        )
    )

    assert result is True
    assert hass.services.calls == [
        ("switch", "turn_off", {"entity_id": "switch.charge_2"})
    ]


def test_tesla_reconciliation_stop_bypasses_stale_stopped_and_away_guards(
    monkeypatch,
):
    vin_a = "5YJTEST00000000A1"
    vin_b = "5YJTEST00000000B2"
    states = [
        _State("sensor.car_a_charging", "stopped"),
        _State("switch.car_a_charge", "on"),
        _State("sensor.car_b_charging", "charging"),
        _State("switch.car_b_charge", "on"),
    ]
    hass = _Hass(
        states,
        registry_entities={
            state.entity_id: SimpleNamespace(
                entity_id=state.entity_id,
                device_id="car-a" if "car_a" in state.entity_id else "car-b",
            )
            for state in states
        },
        registry_devices={
            "car-a": SimpleNamespace(
                id="car-a",
                name="Car A",
                identifiers={("tesla_fleet", vin_a)},
            ),
            "car-b": SimpleNamespace(
                id="car-b",
                name="Car B",
                identifiers={("tesla_fleet", vin_b)},
            ),
        },
    )
    _install_away_location_module(monkeypatch, "remote_charger")
    wake_calls: list[str] = []
    cancelled_stops: list[str] = []
    actions._ev_scheduled_stop["entry-1"] = {
        "cancel": lambda: cancelled_stops.append(vin_b),
        "vehicle_vin": vin_b,
    }

    async def wake_success(_hass, vehicle_vin):
        wake_calls.append(vehicle_vin)
        return True

    monkeypatch.setattr(actions, "_wake_tesla_ev", wake_success)
    params = {
        "charger_type": "tesla",
        "vehicle_id": vin_a,
        "vehicle_vin": vin_a,
        "owner_mode": "smart_schedule",
        "dynamic_mode": "battery_target",
        "_force_tesla_stop_request": True,
    }

    result = asyncio.run(
        actions._action_stop_ev_charging(hass, _tesla_entry(), params)
    )

    assert result is True
    assert wake_calls == [vin_a]
    assert cancelled_stops == []
    assert actions._ev_scheduled_stop["entry-1"]["vehicle_vin"] == vin_b
    assert hass.services.calls == [
        ("switch", "turn_off", {"entity_id": "switch.car_a_charge"})
    ]


def test_tesla_untracked_stop_bypasses_stale_stopped_state_but_checks_location(
    monkeypatch,
):
    vin = "5YJTEST00000000A1"
    states = [
        _State("sensor.car_a_charging", "stopped"),
        _State("switch.car_a_charge", "on"),
    ]
    hass = _Hass(
        states,
        registry_entities={
            state.entity_id: SimpleNamespace(
                entity_id=state.entity_id,
                device_id="car-a",
            )
            for state in states
        },
        registry_devices={
            "car-a": SimpleNamespace(
                id="car-a",
                name="Car A",
                identifiers={("tesla_fleet", vin)},
            ),
        },
    )
    _install_away_location_module(monkeypatch, None)
    wake_calls: list[str] = []

    async def wake_success(_hass, vehicle_vin):
        wake_calls.append(vehicle_vin)
        return True

    monkeypatch.setattr(actions, "_wake_tesla_ev", wake_success)

    result = asyncio.run(
        actions._action_stop_ev_charging(
            hass,
            _tesla_entry(),
            {
                "charger_type": "tesla",
                "vehicle_id": vin,
                "vehicle_vin": vin,
                "owner_mode": "smart_schedule",
                "stop_untracked": True,
            },
        )
    )

    assert result is True
    assert wake_calls == [vin]
    assert hass.services.calls == [
        ("switch", "turn_off", {"entity_id": "switch.car_a_charge"})
    ]


class _ZaptecClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def set_installation_current(self, installation_id: str, amps: int):
        self.calls.append(("set_installation_current", installation_id, amps))

    async def resume_charging(self, charger_id: str):
        self.calls.append(("resume_charging", charger_id))

    async def stop_charging(self, charger_id: str):
        self.calls.append(("stop_charging", charger_id))


class _SessionManager:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    async def update_session(self, **kwargs):
        self.updates.append(kwargs)


class _OcppCentralSystem:
    def __init__(self, accepted: bool, state_accepted: bool = True) -> None:
        self.accepted = accepted
        self.state_accepted = state_accepted
        self.calls: list[tuple[str, float, int]] = []
        self.state_calls: list[tuple[str, str, bool, int]] = []

    async def set_max_charge_rate_amps(self, charger_id: str, amps: float, connector_id: int = 0):
        self.calls.append((charger_id, amps, connector_id))
        return self.accepted

    async def set_charger_state(
        self,
        charger_id: str,
        service_name: str,
        state: bool = True,
        connector_id: int = 1,
    ):
        self.state_calls.append((charger_id, service_name, state, connector_id))
        return self.state_accepted


def _zaptec_entry(installation_id: str = ""):
    return SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "zaptec_standalone_enabled": True,
            "zaptec_username": "user@example.com",
            "zaptec_charger_id": "charger-1",
            "zaptec_installation_id_cloud": installation_id,
        },
    )


def _zaptec_hass(cached_state: dict):
    client = _ZaptecClient()
    hass = _Hass([])
    hass.data["power_sync"]["entry-1"].update({
        "zaptec_client": client,
        "zaptec_cached_state": cached_state,
    })
    return hass, client


def _install_solar_surplus_runtime_stubs(
    monkeypatch,
    live_status: dict,
    ev_soc: float | None = None,
):
    ev_planner = types.ModuleType("power_sync.automations.ev_charging_planner")

    async def get_ev_location(*args, **kwargs):
        return "home"

    async def get_ev_battery_level(*args, **kwargs):
        return ev_soc

    ev_planner.get_ev_location = get_ev_location
    ev_planner.get_ev_battery_level = get_ev_battery_level
    monkeypatch.setitem(sys.modules, "power_sync.automations.ev_charging_planner", ev_planner)

    ev_session = types.ModuleType("power_sync.automations.ev_charging_session")
    ev_session.get_session_manager = lambda: None
    monkeypatch.setitem(sys.modules, "power_sync.automations.ev_charging_session", ev_session)

    async def fake_live_status(*args, **kwargs):
        return live_status

    set_amps_calls: list[int] = []

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append(amps)
        return True

    async def fake_observed_ev_power_kw(*args, **kwargs):
        return 0.0

    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)
    monkeypatch.setattr(actions, "_get_observed_ev_power_kw", fake_observed_ev_power_kw)
    return set_amps_calls


def _solar_surplus_state(current_amps: int = 8) -> dict:
    return {
        "active": True,
        "current_amps": current_amps,
        "target_amps": current_amps,
        "charging_started": True,
        "entity_max_rechecked": True,
        "params": {
            "dynamic_mode": "solar_surplus",
            "charger_type": "tesla",
            "min_charge_amps": 1,
            "max_charge_amps": 32,
            "voltage": 240,
            "phases": 1,
            "household_buffer_kw": 0.5,
            "surplus_calculation": "grid_based",
            "sustained_surplus_minutes": 3,
            "stop_delay_minutes": 5,
            "min_battery_soc": 20,
            "pause_below_soc": 10,
        },
    }


def test_solar_surplus_parallel_reserve_blocks_sigenergy_battery_charge_surplus():
    surplus_kw = actions._calculate_solar_surplus(
        {
            "battery_soc": 38,
            "grid_power": 30,
            "battery_power": -4530,
            "solar_power": 0,
            "load_power": 0,
        },
        current_ev_power_kw=1.68,
        config={
            "surplus_calculation": "grid_based",
            "household_buffer_kw": 2.0,
            "allow_parallel_charging": True,
            "max_battery_charge_rate_kw": 5.0,
            "min_battery_soc": 20,
        },
    )

    assert surplus_kw == 0


def test_solar_surplus_parallel_reserve_allows_excess_above_battery_rate():
    surplus_kw = actions._calculate_solar_surplus(
        {
            "battery_soc": 38,
            "grid_power": -3000,
            "battery_power": -5000,
            "solar_power": 0,
            "load_power": 0,
        },
        current_ev_power_kw=0,
        config={
            "surplus_calculation": "grid_based",
            "household_buffer_kw": 1.0,
            "allow_parallel_charging": True,
            "max_battery_charge_rate_kw": 5.0,
            "min_battery_soc": 20,
        },
    )

    assert surplus_kw == 2.0


def test_active_solar_surplus_refreshes_threshold_with_hysteresis(monkeypatch):
    hass = _Hass([])
    hass.data["power_sync"]["entry-1"]["automation_store"] = types.SimpleNamespace(
        _data={
            "solar_surplus_config": {
                "enabled": True,
                "home_battery_minimum": 90,
                "allow_parallel_charging": False,
                "household_buffer_kw": 0.5,
            }
        }
    )
    vehicle_id = "generic_ev"
    actions._dynamic_ev_state.clear()
    state = _solar_surplus_state(current_amps=10)
    state["params"].update(
        {
            "charger_type": "generic",
            "min_battery_soc": 80,
            "pause_below_soc": 70,
            "notify_on_error": False,
        }
    )
    actions._dynamic_ev_state["entry-1"] = {vehicle_id: state}

    live_status = {
        "battery_soc": 81,
        "grid_power": -2500,
        "battery_power": -500,
        "solar_power": 5000,
        "load_power": 2000,
    }
    set_amps_calls = _install_solar_surplus_runtime_stubs(monkeypatch, live_status)

    async def not_unplugged(*args, **kwargs):
        return False

    monkeypatch.setattr(actions, "_clear_ble_dynamic_session_if_unplugged", not_unplugged)

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    state = actions._dynamic_ev_state["entry-1"][vehicle_id]
    assert state["params"]["min_battery_soc"] == 90
    assert state["params"]["pause_below_soc"] == 80
    assert state.get("paused") is not True
    assert 0 not in set_amps_calls

    live_status["battery_soc"] = 79
    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    assert state["paused"] is True
    assert set_amps_calls[-1] == 0


def test_solar_surplus_curtailed_full_battery_keeps_active_ev_headroom():
    surplus_kw = actions._calculate_solar_surplus(
        {
            "battery_soc": 100,
            "grid_power": 50,
            "battery_power": -2350,
            "solar_power": 0,
            "load_power": 0,
            "is_curtailed": True,
        },
        current_ev_power_kw=3.31,
        config={
            "surplus_calculation": "grid_based",
            "household_buffer_kw": 1.5,
            "allow_parallel_charging": True,
            "max_battery_charge_rate_kw": 3.0,
            "min_battery_soc": 20,
        },
    )

    assert surplus_kw == 5.61


def test_solar_surplus_curtailed_full_battery_probes_idle_ev_start():
    surplus_kw = actions._calculate_solar_surplus(
        {
            "battery_soc": 100,
            "grid_power": 0,
            "battery_power": 0,
            "solar_power": 1200,
            "load_power": 1200,
            "is_curtailed": True,
        },
        current_ev_power_kw=0,
        config={
            "surplus_calculation": "grid_based",
            "household_buffer_kw": 1.2,
            "allow_parallel_charging": True,
            "max_battery_charge_rate_kw": 3.0,
            "min_battery_soc": 20,
            "min_charge_amps": 5,
            "voltage": 240,
            "phases": 1,
        },
    )

    assert surplus_kw == 1.2


def test_solar_surplus_curtailed_full_battery_idle_probe_requires_solar():
    surplus_kw = actions._calculate_solar_surplus(
        {
            "battery_soc": 100,
            "grid_power": 0,
            "battery_power": 0,
            "solar_power": 0,
            "load_power": 0,
            "is_curtailed": True,
        },
        current_ev_power_kw=0,
        config={
            "surplus_calculation": "grid_based",
            "household_buffer_kw": 1.2,
            "min_charge_amps": 5,
            "voltage": 240,
            "phases": 1,
        },
    )

    assert surplus_kw == 0


def test_solar_surplus_curtailed_full_battery_idle_probe_blocks_grid_import():
    surplus_kw = actions._calculate_solar_surplus(
        {
            "battery_soc": 100,
            "grid_power": 300,
            "battery_power": 0,
            "solar_power": 1200,
            "load_power": 1500,
            "is_curtailed": True,
        },
        current_ev_power_kw=0,
        config={
            "surplus_calculation": "grid_based",
            "household_buffer_kw": 1.2,
            "grid_import_tolerance_kw": 0.1,
            "min_charge_amps": 5,
            "voltage": 240,
            "phases": 1,
        },
    )

    assert surplus_kw == 0


def test_solar_surplus_full_battery_topoff_does_not_reserve_battery_charge_rate():
    surplus_kw = actions._calculate_solar_surplus(
        {
            "battery_soc": 100,
            "grid_power": 50,
            "battery_power": -2350,
            "solar_power": 0,
            "load_power": 0,
        },
        current_ev_power_kw=3.31,
        config={
            "surplus_calculation": "grid_based",
            "household_buffer_kw": 1.5,
            "allow_parallel_charging": True,
            "max_battery_charge_rate_kw": 3.0,
            "min_battery_soc": 20,
        },
    )

    assert surplus_kw == 4.11


def test_observed_wall_connector_power_does_not_probe_vehicle_sensor(monkeypatch):
    async def fail_tesla_entity_lookup(*args, **kwargs):
        raise AssertionError("Tesla vehicle power lookup should not run")

    monkeypatch.setattr(actions, "_get_tesla_ev_entity", fail_tesla_entity_lookup)

    hass = _Hass([
        _State("sensor.tesla_wall_connector_power", "2.2", {"unit_of_measurement": "kW"}),
        _State("sensor.tesla_wall_connector_phase_a_current", "9.1", {"unit_of_measurement": "A"}),
        _State("sensor.tesla_wall_connector_energy", "12.3", {"unit_of_measurement": "kWh"}),
    ])

    power_kw = asyncio.run(
        actions._get_observed_ev_power_kw(
            hass,
            "LRW3F7FS1NC484342",
            {"charger_type": "tesla"},
            allow_wall_connector_fallback=True,
        )
    )

    assert power_kw == 2.2


def test_optional_tesla_power_probe_does_not_warn_when_sensor_missing(caplog):
    caplog.set_level("WARNING")
    hass = _Hass([])

    power_kw = asyncio.run(
        actions._get_observed_ev_power_kw(
            hass,
            "LRW3F7FS1NC484342",
            {"charger_type": "tesla"},
        )
    )

    assert power_kw == 0.0
    assert "No Tesla EV devices found" not in caplog.text
    assert "No entity matching pattern" not in caplog.text


def test_tesla_entity_lookup_prefers_healthy_duplicate_vin_provider():
    """A stale Fleet device must not mask healthy Teslemetry controls."""
    vin = "LRW3F7FS1NC484342"
    stale_device = SimpleNamespace(
        id="fleet-device",
        name="Primary EV Fleet",
        identifiers={("tesla_fleet", vin)},
    )
    healthy_device = SimpleNamespace(
        id="teslemetry-device",
        name="Primary EV Teslemetry",
        identifiers={("teslemetry", vin)},
    )

    def registry_entity(entity_id: str, device_id: str):
        return SimpleNamespace(entity_id=entity_id, device_id=device_id)

    registry_entities = {
        "stale-wake": registry_entity("button.primary_ev_wake", stale_device.id),
        "stale-status": registry_entity(
            "binary_sensor.primary_ev_status", stale_device.id
        ),
        "stale-charge": registry_entity("switch.primary_ev_charge", stale_device.id),
        "stale-limit": registry_entity(
            "number.primary_ev_charge_limit", stale_device.id
        ),
        "stale-battery": registry_entity(
            "sensor.primary_ev_battery_level", stale_device.id
        ),
        "healthy-wake": registry_entity(
            "button.primary_ev_wake_2", healthy_device.id
        ),
        "healthy-status": registry_entity(
            "binary_sensor.primary_ev_status_2", healthy_device.id
        ),
        "healthy-cable": registry_entity(
            "binary_sensor.primary_ev_charge_cable_2", healthy_device.id
        ),
        "healthy-charge": registry_entity(
            "switch.primary_ev_charge_2", healthy_device.id
        ),
        "healthy-current": registry_entity(
            "number.primary_ev_charge_current_2", healthy_device.id
        ),
    }
    hass = _Hass(
        [
            _State("button.primary_ev_wake", "2026-08-03T00:06:05+00:00"),
            # Cached telemetry can remain usable even when command controls are not.
            _State("binary_sensor.primary_ev_status", "off"),
            _State("switch.primary_ev_charge", "unknown"),
            _State("number.primary_ev_charge_limit", "80"),
            _State("sensor.primary_ev_battery_level", "76"),
            _State("button.primary_ev_wake_2", "unknown"),
            _State("binary_sensor.primary_ev_status_2", "off"),
            _State("binary_sensor.primary_ev_charge_cable_2", "on"),
            _State("switch.primary_ev_charge_2", "off"),
            _State("number.primary_ev_charge_current_2", "16"),
        ],
        registry_entities=registry_entities,
        # Keep the stale integration first to reproduce HA registry ordering.
        registry_devices={
            stale_device.id: stale_device,
            healthy_device.id: healthy_device,
        },
    )

    charge_switch = asyncio.run(
        actions._get_tesla_ev_entity(
            hass,
            r"switch\..*(?<!dis)charge(?:_\d+)?$",
            vin,
        )
    )
    wake_button = asyncio.run(
        actions._get_tesla_ev_entity(
            hass,
            r"button\..*wake(_up)?(?:_\d+)?$",
            vin,
        )
    )

    assert charge_switch == "switch.primary_ev_charge_2"
    assert wake_button == "button.primary_ev_wake_2"


def test_tesla_charging_lookup_prefers_active_duplicate_vin_provider():
    """An active Tessie state must beat an idle duplicate VIN provider."""
    vin = "LRW3F7FS1NC484342"
    stale_device = SimpleNamespace(
        id="fleet-device",
        identifiers={("tesla_fleet", vin)},
    )
    healthy_device = SimpleNamespace(
        id="tessie-device",
        identifiers={("tessie", vin)},
    )

    def registry_entity(entity_id: str, device_id: str):
        return SimpleNamespace(entity_id=entity_id, device_id=device_id)

    hass = _Hass(
        [
            _State("sensor.primary_ev_charging_state", "stopped"),
            _State("sensor.n3bula_charging", "charging"),
        ],
        registry_entities={
            "stale-state": registry_entity(
                "sensor.primary_ev_charging_state", stale_device.id
            ),
            "healthy-state": registry_entity(
                "sensor.n3bula_charging", healthy_device.id
            ),
        },
        registry_devices={
            stale_device.id: stale_device,
            healthy_device.id: healthy_device,
        },
    )

    charging_state = asyncio.run(
        actions._get_tesla_ev_entity(
            hass,
            r"sensor\..*(charging_state|charging)(?:_\d+)?$",
            vin,
            warn_on_missing=False,
        )
    )

    assert charging_state == "sensor.n3bula_charging"


def test_observed_wall_connector_power_is_counted_for_solar_surplus_stop(monkeypatch):
    async def not_unplugged(*args, **kwargs):
        return False

    async def fake_live_status(*args, **kwargs):
        return {
            "battery_soc": 55,
            "grid_power": 700,
            "battery_power": -700,
            "solar_power": 9200,
            "load_power": 9100,
        }

    stop_calls = []

    async def fake_set_amps(hass, config_entry, vehicle_id, amps, params):
        stop_calls.append((vehicle_id, amps))
        return True

    monkeypatch.setattr(actions, "_clear_ble_dynamic_session_if_unplugged", not_unplugged)
    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_amps)

    ev_planner = types.ModuleType("power_sync.automations.ev_charging_planner")

    async def home_location(*args, **kwargs):
        return "home"

    ev_planner.get_ev_location = home_location
    monkeypatch.setitem(
        sys.modules,
        "power_sync.automations.ev_charging_planner",
        ev_planner,
    )

    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "LRW3F7FS1NC484342": {
            "active": True,
            "current_amps": 0,
            "target_amps": 0,
            "low_surplus_start": datetime.now() - timedelta(minutes=10),
            "params": {
                "dynamic_mode": "solar_surplus",
                "charger_type": "tesla",
                "min_charge_amps": 5,
                "max_charge_amps": 32,
                "voltage": 240,
                "phases": 1,
                "household_buffer_kw": 2.0,
                "surplus_calculation": "grid_based",
                "allow_parallel_charging": True,
                "max_battery_charge_rate_kw": 5.0,
                "min_battery_soc": 20,
                "stop_delay_minutes": 5,
            },
        }
    }

    hass = _Hass([
        _State("sensor.tesla_wall_connector_power", "5.4", {"unit_of_measurement": "kW"}),
    ])

    asyncio.run(
        actions._dynamic_ev_update(
            hass,
            _Entry(),
            "entry-1",
            "LRW3F7FS1NC484342",
        )
    )

    assert stop_calls == [("LRW3F7FS1NC484342", 0)]
    assert actions._dynamic_ev_state["entry-1"]["LRW3F7FS1NC484342"]["current_amps"] == 0


def test_solar_surplus_active_tesla_uses_positive_measured_power_under_curtailment(
    monkeypatch,
):
    """A stale 30 A command must not mask a 3.11 kW measured Tesla load."""
    vehicle_id = "LRW3F7FS1NC484342"
    set_amps_calls: list[tuple[str, int]] = []

    async def not_unplugged(*args, **kwargs):
        return False

    async def home_location(*args, **kwargs):
        return "home"

    async def fake_live_status(*args, **kwargs):
        return {
            "solar_power": 5200,
            "grid_power": 0,
            "battery_power": 0,
            "load_power": 2090,
            "battery_soc": 100,
            "is_curtailed": True,
        }

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append((vehicle_id, amps))
        return True

    async def active_charger(*args, **kwargs):
        return {
            "association_known": True,
            "capability_known": True,
            "max_charge_amps": 32,
            "max_charge_amps_source": "active_charger",
            "voltage": 240,
            "phases": 1,
        }

    ev_planner = types.ModuleType("power_sync.automations.ev_charging_planner")
    ev_planner.get_ev_location = home_location

    async def no_ev_soc(*args, **kwargs):
        return None

    ev_planner.get_ev_battery_level = no_ev_soc
    monkeypatch.setitem(
        sys.modules,
        "power_sync.automations.ev_charging_planner",
        ev_planner,
    )
    monkeypatch.setattr(actions, "_clear_ble_dynamic_session_if_unplugged", not_unplugged)
    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)
    monkeypatch.setattr(
        actions,
        "_resolve_tesla_active_charger_capability",
        active_charger,
    )

    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        vehicle_id: {
            "active": True,
            "current_amps": 30,
            "target_amps": 30,
            "charging_started": True,
            "entity_max_rechecked": True,
            "params": {
                "dynamic_mode": "solar_surplus",
                "charger_type": "tesla",
                "vehicle_vin": vehicle_id,
                "charger_power_entity": "sensor.tesla_charger_power",
                "min_charge_amps": 1,
                "max_charge_amps": 32,
                "voltage": 240,
                "phases": 1,
                "household_buffer_kw": 0.5,
                "surplus_calculation": "grid_based",
                "sustained_surplus_minutes": 3,
                "stop_delay_minutes": 5,
                "min_battery_soc": 20,
                "pause_below_soc": 10,
            },
        }
    }
    hass = _Hass(
        [
            _State(
                "sensor.tesla_charger_power",
                "3.11",
                {"unit_of_measurement": "kW"},
            ),
            _State(f"sensor.{vehicle_id}_charging_state", "charging"),
        ]
    )

    asyncio.run(actions._dynamic_ev_update(hass, _Entry(), "entry-1", vehicle_id))

    state = actions._dynamic_ev_state["entry-1"][vehicle_id]
    assert state["allocated_surplus_kw"] == 3.11
    assert state["target_amps"] == 13
    assert set_amps_calls == [(vehicle_id, 13)]


def test_effective_ev_power_keeps_command_during_restart_telemetry_grace():
    assert actions._effective_ev_power_kw(
        7.2,
        3.11,
        True,
        charging_state="charging",
        restart_telemetry_pending=True,
    ) == 7.2


def test_effective_ev_power_prefers_positive_observation_after_restart_grace():
    assert actions._effective_ev_power_kw(
        7.2,
        3.11,
        True,
        charging_state="charging",
        restart_telemetry_pending=False,
    ) == 3.11


def test_solar_surplus_direct_parallel_reserve_tops_up_existing_battery_charge():
    surplus_kw = actions._calculate_solar_surplus(
        {
            "battery_soc": 38,
            "grid_power": 0,
            "battery_power": -4000,
            "solar_power": 12000,
            "load_power": 1000,
        },
        current_ev_power_kw=0,
        config={
            "surplus_calculation": "direct",
            "household_buffer_kw": 2.0,
            "allow_parallel_charging": True,
            "max_battery_charge_rate_kw": 5.0,
            "min_battery_soc": 20,
        },
    )

    assert surplus_kw == 4.0


def test_dynamic_ocpp_update_leaves_energy_to_ocpp_session_poll(monkeypatch):
    manager = _SessionManager()
    ev_session = types.ModuleType("power_sync.automations.ev_charging_session")
    ev_session.get_session_manager = lambda: manager
    monkeypatch.setitem(sys.modules, "power_sync.automations.ev_charging_session", ev_session)

    async def not_unplugged(*args, **kwargs):
        return False

    async def fake_live_status(*args, **kwargs):
        return {
            "battery_power": 0,
            "grid_power": 1500,
            "battery_soc": 50,
        }

    monkeypatch.setattr(actions, "_clear_ble_dynamic_session_if_unplugged", not_unplugged)
    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)

    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "ocpp_charger": {
            "active": True,
            "current_amps": 32,
            "target_amps": 32,
            "params": {
                "dynamic_mode": "battery_target",
                "charger_type": "ocpp",
                "target_battery_charge_kw": 10.5,
                "max_grid_import_kw": 12.5,
                "min_charge_amps": 6,
                "max_charge_amps": 32,
                "voltage": 230,
                "phases": 1,
            },
        }
    }

    asyncio.run(actions._dynamic_ev_update(_Hass([]), _Entry(), "entry-1", "ocpp_charger"))

    assert manager.updates == []


def test_dynamic_battery_target_learns_early_powerwall_taper(monkeypatch):
    """Two consistent samples can establish taper below a fixed SOC cutoff."""
    set_amps_calls: list[int] = []

    async def not_unplugged(*args, **kwargs):
        return False

    async def fake_live_status(*args, **kwargs):
        return {
            "battery_power": -10000,
            "grid_power": 11000,
            "battery_soc": 82.0,
        }

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append(amps)
        return True

    monkeypatch.setattr(actions, "_clear_ble_dynamic_session_if_unplugged", not_unplugged)
    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)

    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "VIN123": {
            "active": True,
            "current_amps": 5,
            "target_amps": 5,
            "params": {
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
                "target_battery_charge_kw": 14.7,
                "max_grid_import_kw": 16.0,
                "min_charge_amps": 5,
                "max_charge_amps": 32,
                "voltage": 230,
                "phases": 1,
            },
        }
    }

    asyncio.run(actions._dynamic_ev_update(_Hass([]), _Entry(), "entry-1", "VIN123"))
    asyncio.run(actions._dynamic_ev_update(_Hass([]), _Entry(), "entry-1", "VIN123"))

    assert set_amps_calls == [6, 26]
    state = actions._dynamic_ev_state["entry-1"]["VIN123"]
    assert state["current_amps"] == 26
    assert state["_battery_acceptance_learner"]["learned_kw"] == 10.0


def test_dynamic_battery_target_uses_immediate_near_full_taper(monkeypatch):
    set_amps_calls: list[int] = []

    async def not_unplugged(*args, **kwargs):
        return False

    async def fake_live_status(*args, **kwargs):
        return {
            "battery_power": -10000,
            "grid_power": 12000,
            "battery_soc": 95.2,
        }

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append(amps)
        return True

    monkeypatch.setattr(actions, "_clear_ble_dynamic_session_if_unplugged", not_unplugged)
    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)

    actions._dynamic_ev_state["entry-1"] = {
        "VIN123": {
            "active": True,
            "current_amps": 5,
            "target_amps": 5,
            "params": {
                "dynamic_mode": "battery_target",
                "charger_type": "tesla",
                "target_battery_charge_kw": 14.7,
                "max_grid_import_kw": 16.0,
                "min_charge_amps": 5,
                "max_charge_amps": 32,
                "voltage": 230,
                "phases": 1,
            },
        }
    }

    asyncio.run(actions._dynamic_ev_update(_Hass([]), _Entry(), "entry-1", "VIN123"))

    assert set_amps_calls == [22]


def test_battery_acceptance_learner_rejects_a_single_transient_sample():
    learner: dict = {}

    first_reserve, first_learned = actions._effective_battery_charge_reserve_kw(
        learner,
        battery_power_kw=-9.7,
        battery_soc=88,
        target_battery_charge_kw=14.7,
        grid_headroom_kw=3.0,
    )
    second_reserve, second_learned = actions._effective_battery_charge_reserve_kw(
        learner,
        battery_power_kw=-9.8,
        battery_soc=88.2,
        target_battery_charge_kw=14.7,
        grid_headroom_kw=2.9,
    )

    assert first_reserve == 14.7
    assert first_learned is False
    assert second_reserve == pytest.approx(10.1)
    assert second_learned is True


def test_battery_acceptance_learner_reserves_recovered_demand_immediately():
    learner = {
        "target_kw": 14.7,
        "last_soc": 90.0,
        "learned_kw": 9.7,
    }

    reserve_kw, learned = actions._effective_battery_charge_reserve_kw(
        learner,
        battery_power_kw=-12.4,
        battery_soc=90.2,
        target_battery_charge_kw=14.7,
        grid_headroom_kw=0.0,
    )

    assert reserve_kw == pytest.approx(12.7)
    assert learned is True
    assert learner["learned_kw"] == 12.4


def test_battery_acceptance_learner_forgets_when_soc_falls():
    learner = {
        "target_kw": 14.7,
        "last_soc": 91.0,
        "learned_kw": 9.7,
    }

    reserve_kw, learned = actions._effective_battery_charge_reserve_kw(
        learner,
        battery_power_kw=-9.7,
        battery_soc=88.0,
        target_battery_charge_kw=14.7,
        grid_headroom_kw=0.0,
    )

    assert reserve_kw == 14.7
    assert learned is False
    assert "learned_kw" not in learner


def test_battery_acceptance_learner_needs_headroom_to_start_learning():
    learner: dict = {}

    for _ in range(3):
        reserve_kw, learned = actions._effective_battery_charge_reserve_kw(
            learner,
            battery_power_kw=-9.7,
            battery_soc=85.0,
            target_battery_charge_kw=14.7,
            grid_headroom_kw=0.0,
        )

    assert reserve_kw == 14.7
    assert learned is False
    assert "candidate_samples" not in learner


def test_dynamic_tesla_resumes_after_site_headroom_returns(monkeypatch):
    """A Tesla stopped at 0A must receive a new physical start command."""
    vehicle_id = "ble_tesla_flinn"
    live_status = {
        "battery_power": -15000,
        "grid_power": 11300,
        "solar_power": 9600,
        "ev_power": 0,
        "battery_soc": 68,
    }
    set_amps_calls: list[tuple[str, int]] = []
    start_calls: list[tuple[str | None, int]] = []
    takeover_during_set: set[str] = set()

    async def not_unplugged(*args, **kwargs):
        return False

    async def fake_live_status(*args, **kwargs):
        return live_status

    async def fake_set_vehicle_amps(hass, config_entry, requested_id, amps, params):
        set_amps_calls.append((requested_id, amps))
        if requested_id in takeover_during_set:
            actions._dynamic_ev_state["entry-1"][requested_id] = {
                "active": True,
                "current_amps": amps,
                "params": {
                    "dynamic_mode": "manual",
                    "owner_mode": "manual",
                    "charger_type": "tesla",
                },
            }
        return True

    async def fake_start_charging(hass, config_entry, params, context=None):
        start_calls.append((params["vehicle_vin"], params["amps"]))
        return len(start_calls) > 1

    monkeypatch.setattr(actions, "_clear_ble_dynamic_session_if_unplugged", not_unplugged)
    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)
    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start_charging)

    ev_session = types.ModuleType("power_sync.automations.ev_charging_session")
    ev_session.get_session_manager = lambda: None
    monkeypatch.setitem(
        sys.modules,
        "power_sync.automations.ev_charging_session",
        ev_session,
    )

    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        vehicle_id: {
            "active": True,
            "current_amps": 0,
            "target_amps": 0,
            "params": {
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
                "vehicle_vin": vehicle_id,
                "target_battery_charge_kw": 15,
                "max_grid_import_kw": 12.5,
                "min_charge_amps": 5,
                "max_charge_amps": 32,
                "voltage": 230,
                "phases": 3,
            },
        }
    }

    # The reported 15kW battery charge and 12.5kW site limit leave only
    # 1.2kW for the EV, below the 3-phase 5A minimum, so staying stopped is valid.
    asyncio.run(
        actions._dynamic_ev_update(
            _Hass([]),
            _Entry(),
            "entry-1",
            vehicle_id,
        )
    )
    assert set_amps_calls == []
    assert start_calls == []

    # When solar later creates enough headroom, raising the amp limit alone is
    # insufficient because the previous 0A transition sent a physical stop.
    live_status.update(
        {
            "grid_power": 5000,
            "solar_power": 25000,
            # Site-wide EV power can belong to the other Tesla and must not
            # suppress this exact vehicle's idempotent restart command.
            "ev_power": 6900,
        }
    )
    asyncio.run(
        actions._dynamic_ev_update(
            _Hass([]),
            _Entry(),
            "entry-1",
            vehicle_id,
        )
    )

    assert set_amps_calls == [(vehicle_id, 11)]
    assert start_calls == [(vehicle_id, 11)]
    assert actions._dynamic_ev_state["entry-1"][vehicle_id]["current_amps"] == 0

    # A failed physical start must leave state at 0A so the next update retries.
    asyncio.run(
        actions._dynamic_ev_update(
            _Hass([]),
            _Entry(),
            "entry-1",
            vehicle_id,
        )
    )
    assert set_amps_calls == [(vehicle_id, 11), (vehicle_id, 11)]
    assert start_calls == [(vehicle_id, 11), (vehicle_id, 11)]
    assert actions._dynamic_ev_state["entry-1"][vehicle_id]["current_amps"] == 11
    assert (
        actions._dynamic_ev_state["entry-1"][vehicle_id]["params"]["owner_mode"]
        == "smart_schedule"
    )

    # A default Fleet session must preserve an unspecified VIN instead of
    # passing the internal "_default" loadpoint identifier to entity lookup.
    default_id = actions.DEFAULT_VEHICLE_ID
    live_status.update(
        {
            "grid_power": 5000,
            "solar_power": 15000,
            "ev_power": 0,
        }
    )
    actions._dynamic_ev_state["entry-1"] = {
        default_id: {
            "active": True,
            "current_amps": 0,
            "target_amps": 0,
            "params": {
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
                "vehicle_vin": None,
                "target_battery_charge_kw": 15,
                "max_grid_import_kw": 12.5,
                "min_charge_amps": 5,
                "max_charge_amps": 32,
                "voltage": 230,
                "phases": 3,
            },
        }
    }
    asyncio.run(
        actions._dynamic_ev_update(
            _Hass([]),
            _Entry(),
            "entry-1",
            default_id,
        )
    )

    assert set_amps_calls[-1] == (default_id, 11)
    assert start_calls[-1] == (None, 11)
    assert actions._dynamic_ev_state["entry-1"][default_id]["current_amps"] == 11

    # If another mode takes ownership while the amp write is awaiting, the
    # stale Smart Schedule callback must not physically restart the vehicle.
    takeover_during_set.add(vehicle_id)
    live_status.update(
        {
            "grid_power": 5000,
            "solar_power": 15000,
            "ev_power": 0,
        }
    )
    actions._dynamic_ev_state["entry-1"] = {
        vehicle_id: {
            "active": True,
            "current_amps": 0,
            "target_amps": 0,
            "params": {
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
                "vehicle_vin": vehicle_id,
                "target_battery_charge_kw": 15,
                "max_grid_import_kw": 12.5,
                "min_charge_amps": 5,
                "max_charge_amps": 32,
                "voltage": 230,
                "phases": 3,
            },
        }
    }
    starts_before_takeover = len(start_calls)
    asyncio.run(
        actions._dynamic_ev_update(
            _Hass([]),
            _Entry(),
            "entry-1",
            vehicle_id,
        )
    )

    assert len(start_calls) == starts_before_takeover
    assert (
        actions._dynamic_ev_state["entry-1"][vehicle_id]["params"]["owner_mode"]
        == "manual"
    )


def test_dynamic_multi_tesla_group_learns_early_battery_taper(monkeypatch):
    vehicle_ids = ("ble_tesla_yf88", "ble_tesla_flinn")
    set_amps_calls: list[tuple[str, int]] = []
    start_calls: list[tuple[str | None, int]] = []

    async def not_unplugged(*args, **kwargs):
        return False

    async def fake_live_status(*args, **kwargs):
        return {
            "battery_power": -9600,
            "grid_power": 8600,
            "solar_power": 2600,
            "ev_power": 0,
            "battery_soc": 82.0,
        }

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append((vehicle_id, amps))
        return True

    async def fake_start_charging(hass, config_entry, params, context=None):
        start_calls.append((params.get("vehicle_vin"), params["amps"]))
        return True

    monkeypatch.setattr(actions, "_clear_ble_dynamic_session_if_unplugged", not_unplugged)
    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)
    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start_charging)

    actions._dynamic_ev_state["entry-1"] = {
        vehicle_id: {
            "active": True,
            "current_amps": 0,
            "target_amps": 0,
            "priority": 1,
            "params": {
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
                "vehicle_vin": vehicle_id,
                "target_battery_charge_kw": 15,
                "max_grid_import_kw": 12.5,
                "min_charge_amps": 5,
                "max_charge_amps": 32,
                "voltage": 230,
                "phases": 3,
            },
        }
        for vehicle_id in vehicle_ids
    }

    group_leader = "ble_tesla_flinn"
    for _ in range(2):
        asyncio.run(
            actions._dynamic_ev_update(
                _Hass([]),
                _Entry(),
                "entry-1",
                group_leader,
            )
        )

    assert set_amps_calls == [(group_leader, 5)]
    assert start_calls == [(group_leader, 5)]
    learner = actions._dynamic_ev_state["entry-1"][group_leader][
        "_battery_acceptance_learner"
    ]
    assert learner["learned_kw"] == 9.6


def test_dynamic_multi_tesla_site_headroom_is_not_granted_to_both(monkeypatch):
    """Concurrent Smart Schedules must share one site-import envelope."""
    # Match the reporter's callback order, where yf88 ran immediately before
    # flinn against the same cached site sample.
    vehicle_ids = ("ble_tesla_yf88", "ble_tesla_flinn")
    live_status = {
        "battery_power": -9600,
        "grid_power": 8600,
        "solar_power": 2600,
        "ev_power": 0,
        "battery_soc": 95.2,
    }
    set_amps_calls: list[tuple[str, int]] = []
    start_calls: list[tuple[str | None, int]] = []

    async def not_unplugged(*args, **kwargs):
        return False

    async def fake_live_status(*args, **kwargs):
        return live_status

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append((vehicle_id, amps))
        return True

    async def fake_start_charging(hass, config_entry, params, context=None):
        start_calls.append((params.get("vehicle_vin"), params["amps"]))
        return True

    monkeypatch.setattr(actions, "_clear_ble_dynamic_session_if_unplugged", not_unplugged)
    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)
    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start_charging)

    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        vehicle_id: {
            "active": True,
            "current_amps": 0,
            "target_amps": 0,
            "priority": 1,
            "params": {
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
                "vehicle_vin": vehicle_id,
                "target_battery_charge_kw": 15,
                "max_grid_import_kw": 12.5,
                "min_charge_amps": 5,
                "max_charge_amps": 32,
                "voltage": 230,
                "phases": 3,
            },
        }
        for vehicle_id in vehicle_ids
    }

    # The site has only 3.9 kW of aggregate headroom. Flooring the shared
    # allocation permits one Tesla at 5 A three-phase (3.45 kW), not two.
    for vehicle_id in vehicle_ids:
        asyncio.run(
            actions._dynamic_ev_update(
                _Hass([]),
                _Entry(),
                "entry-1",
                vehicle_id,
            )
        )

    assert set_amps_calls == [("ble_tesla_flinn", 5)]
    assert start_calls == [("ble_tesla_flinn", 5)]
    assert actions._dynamic_ev_state["entry-1"]["ble_tesla_flinn"]["current_amps"] == 5
    assert actions._dynamic_ev_state["entry-1"]["ble_tesla_yf88"]["current_amps"] == 0
    assert actions._dynamic_ev_state["entry-1"]["ble_tesla_flinn"]["target_amps"] == 5
    assert actions._dynamic_ev_state["entry-1"]["ble_tesla_flinn"]["charging_started"] is True
    assert actions._dynamic_ev_state["entry-1"]["ble_tesla_yf88"]["charging_started"] is False

    # If two sessions were already running from the old start path, one
    # over-limit sample must not make both controllers stop in lockstep.
    set_amps_calls.clear()
    start_calls.clear()
    for state in actions._dynamic_ev_state["entry-1"].values():
        state["current_amps"] = 6
    live_status.update({"grid_power": 17100, "ev_power": 8280})

    for vehicle_id in vehicle_ids:
        asyncio.run(
            actions._dynamic_ev_update(
                _Hass([]),
                _Entry(),
                "entry-1",
                vehicle_id,
            )
        )

    assert set_amps_calls == [
        ("ble_tesla_flinn", 5),
        ("ble_tesla_yf88", 0),
    ]
    assert start_calls == []
    assert actions._dynamic_ev_state["entry-1"]["ble_tesla_flinn"]["current_amps"] == 5
    assert actions._dynamic_ev_state["entry-1"]["ble_tesla_yf88"]["current_amps"] == 0
    assert actions._dynamic_ev_state["entry-1"]["ble_tesla_yf88"]["target_amps"] == 0
    assert actions._dynamic_ev_state["entry-1"]["ble_tesla_yf88"]["charging_started"] is False

    # Once the site can sustain both physical minimums, equal-priority cars
    # share the budget instead of one remaining starved for the whole window.
    set_amps_calls.clear()
    live_status.update({"grid_power": 5500, "ev_power": 3450})
    for vehicle_id in vehicle_ids:
        asyncio.run(
            actions._dynamic_ev_update(
                _Hass([]),
                _Entry(),
                "entry-1",
                vehicle_id,
            )
        )

    assert set_amps_calls == [
        ("ble_tesla_flinn", 7),
        ("ble_tesla_yf88", 7),
    ]
    assert start_calls == [("ble_tesla_yf88", 7)]
    assert actions._dynamic_ev_state["entry-1"]["ble_tesla_flinn"]["current_amps"] == 7
    assert actions._dynamic_ev_state["entry-1"]["ble_tesla_yf88"]["current_amps"] == 7


def test_dynamic_multi_tesla_caps_follow_each_vehicle_when_chargers_swap(monkeypatch):
    """Each VIN must retain its own active EVSE cap across a physical swap."""
    vehicle_ids = ("5YJTEST00000000D4", "5YJTEST00000000E5")
    caps = {vehicle_ids[0]: 32, vehicle_ids[1]: 10}
    set_amps_calls: list[tuple[str, int]] = []

    async def not_unplugged(*args, **kwargs):
        return False

    async def fake_live_status(*args, **kwargs):
        return {
            "battery_power": 0,
            "grid_power": 0,
            "solar_power": 0,
            "ev_power": 0,
            "battery_soc": 100,
        }

    async def active_charger(hass, config_entry, vehicle_vin, **kwargs):
        return {
            "association_known": True,
            "capability_known": True,
            "max_charge_amps": caps[vehicle_vin],
            "max_charge_amps_source": "active_charger",
            "voltage": 240,
            "phases": 1,
        }

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append((vehicle_id, amps))
        return True

    async def fake_start_charging(*args, **kwargs):
        return True

    monkeypatch.setattr(actions, "_clear_ble_dynamic_session_if_unplugged", not_unplugged)
    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)
    monkeypatch.setattr(
        actions,
        "_resolve_tesla_active_charger_capability",
        active_charger,
    )
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)
    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start_charging)

    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        vehicle_id: {
            "active": True,
            "current_amps": 0,
            "target_amps": 0,
            "priority": 1,
            "params": {
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
                "vehicle_vin": vehicle_id,
                "target_battery_charge_kw": 0,
                "max_grid_import_kw": 12.5,
                "min_charge_amps": 5,
                "max_charge_amps": 32,
                "voltage": 240,
                "phases": 1,
            },
        }
        for vehicle_id in vehicle_ids
    }

    asyncio.run(
        actions._dynamic_ev_update(
            _Hass([]),
            _Entry(),
            "entry-1",
            vehicle_ids[0],
        )
    )

    assert set_amps_calls == [(vehicle_ids[0], 32), (vehicle_ids[1], 10)]
    assert [
        actions._dynamic_ev_state["entry-1"][vehicle_id]["current_amps"]
        for vehicle_id in vehicle_ids
    ] == [32, 10]

    set_amps_calls.clear()
    caps.update({vehicle_ids[0]: 10, vehicle_ids[1]: 32})
    asyncio.run(
        actions._dynamic_ev_update(
            _Hass([]),
            _Entry(),
            "entry-1",
            vehicle_ids[0],
        )
    )

    assert set_amps_calls == [(vehicle_ids[0], 10), (vehicle_ids[1], 32)]
    assert [
        actions._dynamic_ev_state["entry-1"][vehicle_id]["current_amps"]
        for vehicle_id in vehicle_ids
    ] == [10, 32]


def test_dynamic_multi_tesla_failed_decrease_withholds_other_increase(monkeypatch):
    """A failed release of site capacity must not fund another EV increase."""
    live_status = {
        "battery_power": -9600,
        "grid_power": 12740,
        "solar_power": 2600,
        "ev_power": 4140,
        "battery_soc": 95.2,
    }
    set_amps_calls: list[tuple[str, int]] = []
    start_calls: list[tuple[str | None, int]] = []

    async def not_unplugged(*args, **kwargs):
        return False

    async def fake_live_status(*args, **kwargs):
        return live_status

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append((vehicle_id, amps))
        return vehicle_id != "ble_tesla_flinn"

    async def fake_start_charging(hass, config_entry, params, context=None):
        start_calls.append((params.get("vehicle_vin"), params["amps"]))
        return True

    monkeypatch.setattr(actions, "_clear_ble_dynamic_session_if_unplugged", not_unplugged)
    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)
    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start_charging)

    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "ble_tesla_yf88": {
            "active": True,
            "current_amps": 0,
            "target_amps": 0,
            "priority": 1,
            "params": {
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
                "vehicle_vin": "ble_tesla_yf88",
                "target_battery_charge_kw": 15,
                "max_grid_import_kw": 12.5,
                "min_charge_amps": 5,
                "max_charge_amps": 32,
                "voltage": 230,
                "phases": 3,
            },
        },
        "ble_tesla_flinn": {
            "active": True,
            "current_amps": 6,
            "target_amps": 6,
            "priority": 2,
            "params": {
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
                "vehicle_vin": "ble_tesla_flinn",
                "target_battery_charge_kw": 15,
                "max_grid_import_kw": 12.5,
                "min_charge_amps": 5,
                "max_charge_amps": 32,
                "voltage": 230,
                "phases": 3,
            },
        },
    }

    asyncio.run(
        actions._dynamic_ev_update(
            _Hass([]),
            _Entry(),
            "entry-1",
            "ble_tesla_yf88",
        )
    )

    assert set_amps_calls == [("ble_tesla_flinn", 0)]
    assert start_calls == []
    assert actions._dynamic_ev_state["entry-1"]["ble_tesla_flinn"]["current_amps"] == 6
    assert actions._dynamic_ev_state["entry-1"]["ble_tesla_flinn"]["target_amps"] == 6
    assert actions._dynamic_ev_state["entry-1"]["ble_tesla_yf88"]["current_amps"] == 0
    assert actions._dynamic_ev_state["entry-1"]["ble_tesla_yf88"]["target_amps"] == 0


def test_dynamic_multi_tesla_takeover_during_live_status_cancels_plan(monkeypatch):
    """A group plan must retain the ownership that existed before telemetry."""
    set_amps_calls: list[tuple[str, int]] = []
    start_calls: list[str | None] = []

    async def not_unplugged(*args, **kwargs):
        return False

    async def fake_live_status(*args, **kwargs):
        actions._dynamic_ev_state["entry-1"]["ble_tesla_flinn"]["params"][
            "owner_mode"
        ] = "manual"
        return {
            "battery_power": -9600,
            "grid_power": 8600,
            "solar_power": 2600,
            "ev_power": 0,
            "battery_soc": 95.2,
        }

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append((vehicle_id, amps))
        return True

    async def fake_start_charging(hass, config_entry, params, context=None):
        start_calls.append(params.get("vehicle_vin"))
        return True

    monkeypatch.setattr(actions, "_clear_ble_dynamic_session_if_unplugged", not_unplugged)
    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)
    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start_charging)

    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        vehicle_id: {
            "active": True,
            "current_amps": 0,
            "target_amps": 0,
            "priority": 1,
            "params": {
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
                "vehicle_vin": vehicle_id,
                "target_battery_charge_kw": 15,
                "max_grid_import_kw": 12.5,
                "min_charge_amps": 5,
                "max_charge_amps": 32,
                "voltage": 230,
                "phases": 3,
            },
        }
        for vehicle_id in ("ble_tesla_flinn", "ble_tesla_yf88")
    }

    asyncio.run(
        actions._dynamic_ev_update(
            _Hass([]),
            _Entry(),
            "entry-1",
            "ble_tesla_flinn",
        )
    )

    assert set_amps_calls == []
    assert start_calls == []
    assert actions._dynamic_ev_state["entry-1"]["ble_tesla_flinn"]["target_amps"] == 0
    assert (
        actions._dynamic_ev_state["entry-1"]["ble_tesla_flinn"]["params"][
            "owner_mode"
        ]
        == "manual"
    )


def test_dynamic_scheduled_full_battery_grid_cap_holds_min_amps(monkeypatch):
    set_amps_calls: list[int] = []

    async def not_unplugged(*args, **kwargs):
        return False

    async def fake_live_status(*args, **kwargs):
        return {
            "battery_power": -15000,
            "grid_power": 18400,
            "solar_power": 4000,
            "load_power": 500,
            "ev_power": 2400,
            "battery_soc": 95.1,
        }

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append(amps)
        return True

    monkeypatch.setattr(actions, "_clear_ble_dynamic_session_if_unplugged", not_unplugged)
    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)

    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "VIN123": {
            "active": True,
            "current_amps": 10,
            "target_amps": 10,
            "params": {
                "dynamic_mode": "battery_target",
                "owner_mode": "scheduled",
                "charger_type": "tesla",
                "target_battery_charge_kw": 0,
                "max_grid_import_kw": 12.5,
                "no_grid_import": False,
                "min_charge_amps": 6,
                "max_charge_amps": 32,
                "voltage": 240,
                "phases": 1,
            },
        }
    }

    asyncio.run(actions._dynamic_ev_update(_Hass([]), _Entry(), "entry-1", "VIN123"))

    assert set_amps_calls == [6]
    assert actions._dynamic_ev_state["entry-1"]["VIN123"]["current_amps"] == 6


def test_dynamic_scheduled_grid_shortfall_holds_min_amps(monkeypatch):
    set_amps_calls: list[int] = []

    async def not_unplugged(*args, **kwargs):
        return False

    async def fake_live_status(*args, **kwargs):
        return {
            "battery_power": 0,
            "grid_power": 17400,
            "solar_power": 0,
            "ev_power": 2400,
            "battery_soc": 70,
        }

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append(amps)
        return True

    monkeypatch.setattr(actions, "_clear_ble_dynamic_session_if_unplugged", not_unplugged)
    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)

    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "VIN123": {
            "active": True,
            "current_amps": 10,
            "target_amps": 10,
            "params": {
                "dynamic_mode": "battery_target",
                "owner_mode": "scheduled",
                "charger_type": "tesla",
                "target_battery_charge_kw": 5,
                "max_grid_import_kw": 12.5,
                "no_grid_import": False,
                "min_charge_amps": 6,
                "max_charge_amps": 32,
                "voltage": 240,
                "phases": 1,
            },
        }
    }

    asyncio.run(actions._dynamic_ev_update(_Hass([]), _Entry(), "entry-1", "VIN123"))

    assert set_amps_calls == [6]
    assert actions._dynamic_ev_state["entry-1"]["VIN123"]["current_amps"] == 6


def test_dynamic_full_battery_grid_cap_can_stop_non_scheduled_session(monkeypatch):
    set_amps_calls: list[int] = []

    async def not_unplugged(*args, **kwargs):
        return False

    async def fake_live_status(*args, **kwargs):
        return {
            "battery_power": -15000,
            "grid_power": 18400,
            "solar_power": 4000,
            "load_power": 500,
            "ev_power": 2400,
            "battery_soc": 95.1,
        }

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append(amps)
        return True

    monkeypatch.setattr(actions, "_clear_ble_dynamic_session_if_unplugged", not_unplugged)
    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)

    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "VIN123": {
            "active": True,
            "current_amps": 10,
            "target_amps": 10,
            "params": {
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
                "target_battery_charge_kw": 0,
                "max_grid_import_kw": 12.5,
                "no_grid_import": False,
                "min_charge_amps": 6,
                "max_charge_amps": 32,
                "voltage": 240,
                "phases": 1,
            },
        }
    }

    asyncio.run(actions._dynamic_ev_update(_Hass([]), _Entry(), "entry-1", "VIN123"))

    assert set_amps_calls == [0]
    assert actions._dynamic_ev_state["entry-1"]["VIN123"]["current_amps"] == 0


def test_dynamic_battery_target_uses_solar_and_home_load_to_preserve_grid_charge(monkeypatch):
    set_amps_calls: list[int] = []

    async def not_unplugged(*args, **kwargs):
        return False

    async def fake_live_status(*args, **kwargs):
        return {
            "battery_power": -8200,
            "grid_power": 15900,
            "solar_power": 3000,
            "load_power": 3600,
            "ev_power": 7100,
            "battery_soc": 88.0,
        }

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append(amps)
        return True

    monkeypatch.setattr(actions, "_clear_ble_dynamic_session_if_unplugged", not_unplugged)
    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)

    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "VIN123": {
            "active": True,
            "current_amps": 32,
            "target_amps": 32,
            "params": {
                "dynamic_mode": "battery_target",
                "charger_type": "tesla",
                "target_battery_charge_kw": 14.7,
                "max_grid_import_kw": 16.0,
                "no_grid_import": True,
                "min_charge_amps": 5,
                "max_charge_amps": 32,
                "voltage": 240,
                "phases": 1,
            },
        }
    }

    asyncio.run(actions._dynamic_ev_update(_Hass([]), _Entry(), "entry-1", "VIN123"))

    assert set_amps_calls == [5]
    assert actions._dynamic_ev_state["entry-1"]["VIN123"]["current_amps"] == 5


def test_non_ev_home_load_uses_site_balance_when_load_power_is_already_adjusted():
    live_status = {
        "solar_power": 3000,
        "grid_power": 15900,
        "battery_power": -8200,
        "load_power": 3600,
    }

    assert round(actions._non_ev_home_load_kw(live_status, 7.1), 3) == 3.6


def test_direct_surplus_does_not_subtract_ev_from_normalized_home_load_twice():
    live_status = {
        "solar_power": 10_000,
        "grid_power": -5_000,
        "battery_power": 0,
        "load_power": 2_000,
        "home_load_basis": "excludes_ev",
    }

    assert actions._calculate_solar_surplus(
        live_status,
        current_ev_power_kw=3.0,
        config={"surplus_calculation": "direct", "household_buffer_kw": 0.0},
    ) == 8.0


def test_direct_surplus_fails_closed_when_home_load_is_unavailable():
    live_status = {
        "solar_power": 10_000,
        "grid_power": -5_000,
        "battery_power": 0,
        "load_power": None,
        "home_load_basis": "excludes_ev",
    }

    assert actions._calculate_solar_surplus(
        live_status,
        current_ev_power_kw=3.0,
        config={"surplus_calculation": "direct", "household_buffer_kw": 0.0},
    ) == 0.0


def test_grid_based_surplus_does_not_require_home_load():
    live_status = {
        "solar_power": 10_000,
        "grid_power": -5_000,
        "battery_power": 0,
        "load_power": None,
        "home_load_basis": "excludes_ev",
    }

    assert actions._calculate_solar_surplus(
        live_status,
        current_ev_power_kw=3.0,
        config={"surplus_calculation": "grid_based", "household_buffer_kw": 0.0},
    ) == 8.0


def test_non_ev_home_load_does_not_subtract_normalized_fallback_twice():
    live_status = {
        "load_power": 2_000,
        "home_load_basis": "excludes_ev",
    }

    assert actions._non_ev_home_load_kw(live_status, 3.0) == 2.0


def test_ocpp_amps_falls_back_to_hacs_number_entity():
    entity_id = "number.evse_1_maximum_current"
    hass = _Hass(
        [
            _State(entity_id, "16", {"min": 6, "max": 32}),
        ],
        {
            entity_id: SimpleNamespace(entity_id=entity_id, platform="ocpp"),
        },
    )

    assert asyncio.run(actions._set_ocpp_charging_amps(hass, "evse_1", 40)) is True
    assert hass.services.calls == [
        ("number", "set_value", {"entity_id": entity_id, "value": 32})
    ]


def test_ocpp_amps_uses_hacs_api_when_available():
    central = _OcppCentralSystem(accepted=True)
    hass = _Hass([
        _State("number.evse_1_maximum_current", "16", {"min": 6, "max": 32}),
    ])
    hass.data["ocpp"] = {"ocpp-entry": central}

    assert asyncio.run(actions._set_ocpp_charging_amps(hass, "evse_1", 16)) is True

    assert central.calls == [("evse_1", 16.0, 0)]
    assert hass.services.calls == []


def test_ocpp_amps_uses_hacs_api_connector_id_for_multi_connector_prefix():
    central = _OcppCentralSystem(accepted=True)
    hass = _Hass([
        _State("number.evse_1_connector_2_maximum_current", "16", {"min": 6, "max": 32}),
    ])
    hass.data["ocpp"] = {"ocpp-entry": central}

    assert asyncio.run(
        actions._set_ocpp_charging_amps(hass, "evse_1_connector_2", 16)
    ) is True

    assert central.calls == [("evse_1", 16.0, 2)]
    assert hass.services.calls == []


def test_ocpp_amps_reports_hacs_api_rejection_without_optimistic_number_fallback():
    central = _OcppCentralSystem(accepted=False)
    hass = _Hass([
        _State("number.evse_1_maximum_current", "16", {"min": 6, "max": 32}),
    ])
    hass.data["ocpp"] = {"ocpp-entry": central}

    assert asyncio.run(actions._set_ocpp_charging_amps(hass, "evse_1", 16)) is False

    assert central.calls == [("evse_1", 16.0, 0)]
    assert hass.services.calls == []


def test_ocpp_amps_rejects_hacs_number_entity_capped_below_evse_minimum():
    entity_id = "number.evse_1_maximum_current"
    hass = _Hass(
        [
            _State(entity_id, "5", {"min": 0, "max": 5}),
        ],
        {
            entity_id: SimpleNamespace(entity_id=entity_id, platform="ocpp"),
        },
    )

    assert asyncio.run(actions._set_ocpp_charging_amps(hass, "evse_1", 7)) is False
    assert hass.services.calls == []


def test_ocpp_effective_minimum_amps_is_six():
    assert actions._effective_min_charge_amps({
        "charger_type": "ocpp",
        "min_charge_amps": 5,
    }) == 6


def test_ocpp_managed_start_fails_when_only_switch_control_exists():
    hass = _Hass([_State("switch.evse_1_charge_control", "off")])

    result = asyncio.run(
        actions._set_vehicle_amps(
            hass,
            _Entry(),
            "ocpp_evse_1",
            16,
            {"charger_type": "ocpp", "ocpp_charger_id": "evse_1"},
        )
    )

    assert result is False
    assert hass.services.calls == []


def test_ocpp_direct_start_uses_hacs_api_result():
    central = _OcppCentralSystem(accepted=True, state_accepted=True)
    hass = _Hass([_State("switch.evse_1_charge_control", "off")])
    hass.data["ocpp"] = {"ocpp-entry": central}

    assert asyncio.run(actions._start_ocpp_charging(hass, "evse_1")) is True

    assert central.state_calls == [("evse_1", "service_charge_start", True, 1)]
    assert hass.services.calls == []


def test_ocpp_direct_start_reports_hacs_rejection():
    central = _OcppCentralSystem(accepted=True, state_accepted=False)
    hass = _Hass([_State("switch.evse_1_charge_control", "off")])
    hass.data["ocpp"] = {"ocpp-entry": central}

    assert asyncio.run(actions._start_ocpp_charging(hass, "evse_1")) is False

    assert central.state_calls == [("evse_1", "service_charge_start", True, 1)]
    assert hass.services.calls == []


def test_ocpp_start_skips_duplicate_remote_start_when_switch_already_on():
    hass = _Hass(
        [
            _State("switch.evse_1_charge_control", "on"),
            _State("sensor.evse_1_status_connector", "Charging"),
        ]
    )

    assert asyncio.run(actions._start_ocpp_charging(hass, "evse_1")) is True
    assert hass.services.calls == []


def test_ocpp_start_still_resets_when_switch_on_but_connector_finishing():
    hass = _Hass(
        [
            _State("switch.evse_1_charge_control", "on"),
            _State("sensor.evse_1_status_connector", "Finishing"),
        ]
    )

    assert asyncio.run(actions._start_ocpp_charging(hass, "evse_1")) is True
    assert hass.services.calls == [
        ("switch", "turn_off", {"entity_id": "switch.evse_1_charge_control"}),
        ("switch", "turn_on", {"entity_id": "switch.evse_1_charge_control"}),
    ]


def test_ocpp_current_limit_rejection_is_cached_for_session(monkeypatch):
    calls = []

    async def reject_current_limit(hass, charger_id, amps):
        calls.append((charger_id, amps))
        return False

    monkeypatch.setattr(actions, "_set_ocpp_charging_amps", reject_current_limit)

    hass = _Hass([_State("switch.evse_1_charge_control", "off")])
    params = {"charger_type": "ocpp", "ocpp_charger_id": "evse_1"}

    assert asyncio.run(actions._set_vehicle_amps(hass, _Entry(), "ocpp_evse_1", 7, params)) is False
    assert params["_ocpp_current_limit_unsupported"] is True
    assert asyncio.run(actions._set_vehicle_amps(hass, _Entry(), "ocpp_evse_1", 5, params)) is False

    assert calls == [("evse_1", 7)]
    assert hass.services.calls == []


def test_ocpp_loadpoint_id_does_not_double_prefix():
    assert actions._ev_action_loadpoint_id({
        "charger_type": "ocpp",
        "ocpp_charger_id": "evse_1",
    }) == "ocpp_evse_1"

    assert actions._ev_action_loadpoint_id({
        "charger_type": "ocpp",
        "ocpp_charger_id": "ocpp_evse_1",
    }) == "ocpp_evse_1"


def test_generic_start_blocks_when_status_available_and_no_connector_present():
    hass = _Hass([
        _State("switch.garage_ev", "off"),
        _State("sensor.garage_ev_status", "Available"),
    ])

    result = asyncio.run(
        actions._action_start_ev_charging(
            hass,
            _Entry(),
            {
                "charger_type": "generic",
                "charger_switch_entity": "switch.garage_ev",
                "charger_status_entity": "sensor.garage_ev_status",
            },
        )
    )

    assert result is False
    assert hass.services.calls == []


def test_generic_start_allows_available_status_when_connector_has_car():
    hass = _Hass([
        _State("switch.garage_ev", "off"),
        _State("sensor.garage_ev_status", "Available"),
        _State("sensor.garage_ev_status_connector", "Preparing"),
    ])

    result = asyncio.run(
        actions._action_start_ev_charging(
            hass,
            _Entry(),
            {
                "charger_type": "generic",
                "charger_switch_entity": " switch.garage_ev ",
                "charger_status_entity": "sensor.garage_ev_status",
            },
        )
    )

    assert result is True
    assert hass.services.calls == [
        ("switch", "turn_on", {"entity_id": "switch.garage_ev"})
    ]


def test_generic_start_runs_pre_charge_wake_before_switch_on():
    hass = _Hass([
        _State("switch.garage_ev", "off"),
        _State("sensor.garage_ev_status", "Preparing"),
        _State("switch.byd_aircon", "off"),
    ])

    result = asyncio.run(
        actions._action_start_ev_charging(
            hass,
            _Entry(),
            {
                "charger_type": "generic",
                "charger_switch_entity": "switch.garage_ev",
                "charger_status_entity": "sensor.garage_ev_status",
                "pre_charge_wake_entity": "switch.byd_aircon",
                "pre_charge_wake_duration_seconds": 0,
            },
        )
    )

    assert result is True
    assert hass.services.calls == [
        ("switch", "turn_on", {"entity_id": "switch.byd_aircon"}),
        ("switch", "turn_off", {"entity_id": "switch.byd_aircon"}),
        ("switch", "turn_on", {"entity_id": "switch.garage_ev"}),
    ]


def test_generic_direct_start_skips_switch_that_is_already_on():
    hass = _Hass([_State("switch.charger_charge_control", "on")])

    result = asyncio.run(
        actions._action_start_ev_charging(
            hass,
            _Entry(),
            {
                "charger_type": "generic",
                "charger_switch_entity": "switch.charger_charge_control",
            },
        )
    )

    assert result is True
    assert hass.services.calls == []


def test_generic_ocpp_wrapper_resets_finishing_switch_before_start(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(actions.asyncio, "sleep", fake_sleep)
    hass = _Hass([
        _State("switch.charger_charge_control", "on"),
        _State("sensor.charger_status_connector", "Finishing"),
    ])

    result = asyncio.run(
        actions._action_start_ev_charging(
            hass,
            _Entry(),
            {
                "charger_type": "generic",
                "charger_switch_entity": "switch.charger_charge_control",
            },
        )
    )

    assert result is True
    assert hass.services.calls == [
        ("switch", "turn_off", {"entity_id": "switch.charger_charge_control"}),
        ("switch", "turn_on", {"entity_id": "switch.charger_charge_control"}),
    ]
    assert sleeps == [1]


def test_generic_set_vehicle_amps_uses_input_number_and_skips_duplicate_start():
    hass = _Hass([
        _State("input_number.smart_charge_set_amps", "16"),
        _State("switch.charger_charge_control", "on"),
    ])

    result = asyncio.run(
        actions._set_vehicle_amps(
            hass,
            _Entry(),
            "generic_ev",
            12,
            {
                "charger_type": "generic",
                "charger_amps_entity": "input_number.smart_charge_set_amps",
                "charger_switch_entity": "switch.charger_charge_control",
            },
        )
    )

    assert result is True
    assert hass.services.calls == [
        (
            "input_number",
            "set_value",
            {"entity_id": "input_number.smart_charge_set_amps", "value": 12},
        )
    ]


def test_generic_direct_set_amps_uses_configured_entity_domain():
    hass = _Hass([_State("input_number.smart_charge_set_amps", "16")])

    result = asyncio.run(
        actions._action_set_ev_charging_amps(
            hass,
            _Entry(),
            {
                "charger_type": "generic",
                "charger_amps_entity": "input_number.smart_charge_set_amps",
                "amps": 10,
            },
        )
    )

    assert result is True
    assert hass.services.calls == [
        (
            "input_number",
            "set_value",
            {"entity_id": "input_number.smart_charge_set_amps", "value": 10},
        )
    ]


def test_generic_switch_stop_does_not_require_zero_amp_write():
    hass = _Hass([
        _State("input_number.smart_charge_set_amps", "6", {"min": 6, "max": 32}),
        _State("switch.charger_charge_control", "on"),
    ])

    result = asyncio.run(
        actions._set_vehicle_amps(
            hass,
            _Entry(),
            "generic_ev",
            0,
            {
                "charger_type": "generic",
                "charger_amps_entity": "input_number.smart_charge_set_amps",
                "charger_switch_entity": "switch.charger_charge_control",
            },
        )
    )

    assert result is True
    assert hass.services.calls == [
        ("switch", "turn_off", {"entity_id": "switch.charger_charge_control"})
    ]


def test_generic_amps_only_stop_sets_input_number_to_zero():
    hass = _Hass([_State("input_number.smart_charge_set_amps", "6")])

    result = asyncio.run(
        actions._set_vehicle_amps(
            hass,
            _Entry(),
            "generic_ev",
            0,
            {
                "charger_type": "generic",
                "charger_amps_entity": "input_number.smart_charge_set_amps",
            },
        )
    )

    assert result is True
    assert hass.services.calls == [
        (
            "input_number",
            "set_value",
            {"entity_id": "input_number.smart_charge_set_amps", "value": 0},
        )
    ]


def test_generic_effective_minimum_uses_authoritative_entity_floor():
    hass = _Hass([
        _State(
            "number.garage_ev_current",
            "16",
            {"min": 6, "max": 32},
        )
    ])

    assert actions._effective_min_charge_amps(
        {
            "charger_type": "generic",
            "charger_amps_entity": "number.garage_ev_current",
            "min_charge_amps": 5,
        },
        hass,
    ) == 6


def test_generic_direct_current_clamps_to_authoritative_entity_bounds():
    hass = _Hass([
        _State(
            "number.garage_ev_current",
            "16",
            {"min": 6, "max": 32},
        )
    ])

    result = asyncio.run(
        actions._action_set_ev_charging_amps(
            hass,
            _Entry(),
            {
                "charger_type": "generic",
                "charger_amps_entity": "number.garage_ev_current",
                "amps": 5,
            },
        )
    )

    assert result is True
    assert hass.services.calls == [
        (
            "number",
            "set_value",
            {"entity_id": "number.garage_ev_current", "value": 6},
        )
    ]


def test_generic_read_only_entity_attributes_support_bounds_direct_and_dynamic_start():
    entity_id = "number.garage_ev_current"
    read_only_attributes = MappingProxyType({"min": 6, "max": 32})
    hass = _Hass([
        _State(entity_id, "16", read_only_attributes),
    ])

    assert actions._generic_charger_entity_bounds(hass, entity_id) == (6, 32, True)
    direct_result = asyncio.run(
        actions._action_set_ev_charging_amps(
            hass,
            _Entry(),
            {
                "charger_type": "generic",
                "charger_amps_entity": entity_id,
                "amps": 5,
            },
        )
    )

    assert direct_result is True
    assert hass.services.calls == [
        (
            "number",
            "set_value",
            {"entity_id": entity_id, "value": 6},
        )
    ]

    actions._dynamic_ev_state.clear()
    dynamic_hass = _Hass([
        _State(entity_id, "16", read_only_attributes),
        _State("switch.garage_ev", "off"),
    ])
    dynamic_result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            dynamic_hass,
            _Entry(),
            {
                "dynamic_mode": "battery_target",
                "owner_mode": "scheduled",
                "charger_type": "generic",
                "charger_amps_entity": entity_id,
                "charger_switch_entity": "switch.garage_ev",
                "min_charge_amps": 5,
                "max_charge_amps": 32,
            },
        )
    )

    assert dynamic_result is True
    assert actions._dynamic_ev_state["entry-1"]["_default"]["active"] is True
    assert dynamic_hass.services.calls == [
        (
            "number",
            "set_value",
            {"entity_id": entity_id, "value": 32},
        ),
        ("switch", "turn_on", {"entity_id": "switch.garage_ev"}),
    ]


def test_generic_fractional_minimum_rounds_up_before_direct_write():
    hass = _Hass([
        _State(
            "number.garage_ev_current",
            "16",
            {"min": 6.5, "max": 32},
        )
    ])

    result = asyncio.run(
        actions._action_set_ev_charging_amps(
            hass,
            _Entry(),
            {
                "charger_type": "generic",
                "charger_amps_entity": "number.garage_ev_current",
                "amps": 5,
            },
        )
    )

    assert result is True
    assert hass.services.calls == [
        (
            "number",
            "set_value",
            {"entity_id": "number.garage_ev_current", "value": 7},
        )
    ]


def test_generic_fractional_maximum_rounds_down_before_direct_write():
    hass = _Hass([
        _State(
            "number.garage_ev_current",
            "6",
            {"min": 1, "max": 6.5},
        )
    ])

    result = asyncio.run(
        actions._action_set_ev_charging_amps(
            hass,
            _Entry(),
            {
                "charger_type": "generic",
                "charger_amps_entity": "number.garage_ev_current",
                "amps": 10,
            },
        )
    )

    assert result is True
    assert hass.services.calls == [
        (
            "number",
            "set_value",
            {"entity_id": "number.garage_ev_current", "value": 6},
        )
    ]


def test_generic_contradictory_entity_bounds_fail_closed():
    hass = _Hass([
        _State(
            "number.garage_ev_current",
            "7",
            {"min": 9, "max": 7},
        )
    ])

    result = asyncio.run(
        actions._action_set_ev_charging_amps(
            hass,
            _Entry(),
            {
                "charger_type": "generic",
                "charger_amps_entity": "number.garage_ev_current",
                "amps": 10,
            },
        )
    )

    assert result is False
    assert hass.services.calls == []


def test_generic_invalid_entity_bounds_fail_closed():
    hass = _Hass([
        _State(
            "number.garage_ev_current",
            "16",
            {"min": "invalid", "max": 32},
        )
    ])

    result = asyncio.run(
        actions._action_set_ev_charging_amps(
            hass,
            _Entry(),
            {
                "charger_type": "generic",
                "charger_amps_entity": "number.garage_ev_current",
                "amps": 10,
            },
        )
    )

    assert result is False
    assert hass.services.calls == []


def test_generic_non_dict_entity_attributes_fail_closed():
    hass = _Hass([
        _State(
            "number.garage_ev_current",
            "16",
            ["invalid attributes"],
        )
    ])

    result = asyncio.run(
        actions._action_set_ev_charging_amps(
            hass,
            _Entry(),
            {
                "charger_type": "generic",
                "charger_amps_entity": "number.garage_ev_current",
                "amps": 10,
            },
        )
    )

    assert result is False
    assert hass.services.calls == []


def test_generic_dynamic_start_rejects_no_positive_integer_range(monkeypatch):
    timer_calls: list[tuple] = []

    def fake_track_interval(*args, **kwargs):
        timer_calls.append((args, kwargs))
        return lambda: None

    monkeypatch.setattr(actions, "async_track_time_interval", fake_track_interval)
    actions._dynamic_ev_state.clear()
    hass = _Hass([
        _State(
            "number.garage_ev_current",
            "0",
            {"min": 0, "max": 0.5},
        ),
        _State("switch.garage_ev", "off"),
    ])

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "dynamic_mode": "battery_target",
                "owner_mode": "scheduled",
                "charger_type": "generic",
                "charger_amps_entity": "number.garage_ev_current",
                "charger_switch_entity": "switch.garage_ev",
                "min_charge_amps": 5,
                "max_charge_amps": 32,
            },
        )
    )

    assert result is False
    assert actions._dynamic_ev_state == {}
    assert hass.services.calls == []
    assert timer_calls == []
    assert hass.data["power_sync"]["entry-1"].get("ev_ownership") in (None, {})


def test_solar_surplus_stays_paused_at_pause_soc_until_min_soc(monkeypatch):
    set_amps_calls: list[int] = []
    start_calls: list[dict] = []

    async def fake_clear_unplugged(*args, **kwargs):
        return False

    async def fake_full_soc_reason(*args, **kwargs):
        return None

    async def fake_live_status(*args, **kwargs):
        return {
            "battery_soc": 70,
            "grid_power": -5000,
            "solar_power": 0,
            "battery_power": 0,
            "load_power": 0,
        }

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append(amps)
        return True

    async def fake_start(hass, config_entry, params, context=None):
        start_calls.append(params)
        return True

    monkeypatch.setattr(actions, "_clear_ble_dynamic_session_if_unplugged", fake_clear_unplugged)
    monkeypatch.setattr(actions, "_dynamic_ev_full_soc_reason", fake_full_soc_reason)
    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)
    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start)
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "generic_ev": {
            "active": True,
            "charging_started": True,
            "paused": True,
            "current_amps": 0,
            "target_amps": 0,
            "high_surplus_start": datetime.now() - timedelta(minutes=5),
            "params": {
                "dynamic_mode": "solar_surplus",
                "charger_type": "generic",
                "charger_amps_entity": "number.garage_ev_current",
                "min_charge_amps": 5,
                "max_charge_amps": 32,
                "min_battery_soc": 80,
                "pause_below_soc": 70,
                "stop_at_battery_floor": True,
                "household_buffer_kw": 0,
                "sustained_surplus_minutes": 2,
                "stop_delay_minutes": 5,
                "voltage": 240,
                "phases": 1,
            },
        }
    }
    hass = _Hass([
        _State("number.garage_ev_current", "16", {"min": 6, "max": 32}),
    ])

    asyncio.run(actions._dynamic_ev_update(hass, _Entry(), "entry-1", "generic_ev"))

    assert start_calls == []
    assert set_amps_calls == []


def test_solar_surplus_does_not_start_below_generic_entity_floor(monkeypatch):
    set_amps_calls: list[int] = []
    start_calls: list[dict] = []

    async def fake_clear_unplugged(*args, **kwargs):
        return False

    async def fake_full_soc_reason(*args, **kwargs):
        return None

    async def fake_live_status(*args, **kwargs):
        return {
            "battery_soc": 80,
            # 5.5 A at 240 V: still below the entity's authoritative 6 A
            # floor and must not be rounded up into a charging start.
            "grid_power": -1320,
            "solar_power": 0,
            "battery_power": 0,
            "load_power": 0,
        }

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append(amps)
        return True

    async def fake_start(hass, config_entry, params, context=None):
        start_calls.append(params)
        return True

    monkeypatch.setattr(actions, "_clear_ble_dynamic_session_if_unplugged", fake_clear_unplugged)
    monkeypatch.setattr(actions, "_dynamic_ev_full_soc_reason", fake_full_soc_reason)
    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)
    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start)
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "generic_ev": {
            "active": True,
            "charging_started": False,
            "paused": False,
            "current_amps": 0,
            "target_amps": 0,
            "high_surplus_start": datetime.now() - timedelta(minutes=5),
            "params": {
                "dynamic_mode": "solar_surplus",
                "charger_type": "generic",
                "charger_amps_entity": "number.garage_ev_current",
                "min_charge_amps": 5,
                "max_charge_amps": 32,
                "min_battery_soc": 80,
                "pause_below_soc": 70,
                "household_buffer_kw": 0,
                "sustained_surplus_minutes": 2,
                "stop_delay_minutes": 5,
                "voltage": 240,
                "phases": 1,
            },
        }
    }
    hass = _Hass([
        _State("number.garage_ev_current", "16", {"min": 6, "max": 32}),
    ])

    asyncio.run(actions._dynamic_ev_update(hass, _Entry(), "entry-1", "generic_ev"))

    assert start_calls == []
    assert set_amps_calls == []
    state = actions._dynamic_ev_state["entry-1"]["generic_ev"]
    assert state["paused"] is False
    assert state["charging_started"] is False


def test_solar_surplus_stops_active_generic_session_with_invalid_entity_bounds(monkeypatch):
    set_amps_calls: list[int] = []

    async def fake_clear_unplugged(*args, **kwargs):
        return False

    async def fake_full_soc_reason(*args, **kwargs):
        return None

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append(amps)
        return True

    monkeypatch.setattr(actions, "_clear_ble_dynamic_session_if_unplugged", fake_clear_unplugged)
    monkeypatch.setattr(actions, "_dynamic_ev_full_soc_reason", fake_full_soc_reason)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "generic_ev": {
            "active": True,
            "charging_started": True,
            "current_amps": 0,
            "target_amps": 0,
            "params": {
                "dynamic_mode": "solar_surplus",
                "charger_type": "generic",
                "charger_amps_entity": "number.garage_ev_current",
                "min_charge_amps": 5,
                "max_charge_amps": 32,
                "notify_on_complete": False,
            },
        }
    }
    hass = _Hass([
        _State("number.garage_ev_current", "0", {"min": "nan", "max": 32}),
    ])

    asyncio.run(actions._dynamic_ev_update(hass, _Entry(), "entry-1", "generic_ev"))

    assert set_amps_calls == [0]
    assert "entry-1" not in actions._dynamic_ev_state


def test_solar_surplus_resumes_at_pause_threshold_when_floor_stop_disabled(monkeypatch):
    set_amps_calls: list[int] = []
    start_calls: list[dict] = []

    async def fake_clear_unplugged(*args, **kwargs):
        return False

    async def fake_full_soc_reason(*args, **kwargs):
        return None

    async def fake_live_status(*args, **kwargs):
        return {
            "battery_soc": 70,
            "grid_power": -5000,
            "solar_power": 0,
            "battery_power": 0,
            "load_power": 0,
        }

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append(amps)
        return True

    async def fake_start(hass, config_entry, params, context=None):
        start_calls.append(params)
        return True

    monkeypatch.setattr(actions, "_clear_ble_dynamic_session_if_unplugged", fake_clear_unplugged)
    monkeypatch.setattr(actions, "_dynamic_ev_full_soc_reason", fake_full_soc_reason)
    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)
    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start)
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "generic_ev": {
            "active": True,
            "charging_started": True,
            "paused": True,
            "current_amps": 0,
            "target_amps": 0,
            "high_surplus_start": datetime.now() - timedelta(minutes=5),
            "params": {
                "dynamic_mode": "solar_surplus",
                "charger_type": "generic",
                "charger_amps_entity": "number.garage_ev_current",
                "min_charge_amps": 5,
                "max_charge_amps": 32,
                "min_battery_soc": 80,
                "pause_below_soc": 70,
                "stop_at_battery_floor": False,
                "household_buffer_kw": 0,
                "sustained_surplus_minutes": 2,
                "stop_delay_minutes": 5,
                "voltage": 240,
                "phases": 1,
            },
        }
    }
    hass = _Hass([
        _State("number.garage_ev_current", "16", {"min": 6, "max": 32}),
    ])

    asyncio.run(actions._dynamic_ev_update(hass, _Entry(), "entry-1", "generic_ev"))

    assert start_calls
    assert set_amps_calls
    assert actions._dynamic_ev_state["entry-1"]["generic_ev"]["paused"] is False


def test_ocpp_pre_charge_wake_blocks_when_connector_available():
    hass = _Hass([
        _State("switch.evse_1_charge_control", "off"),
        _State("sensor.evse_1_status_connector", "Available"),
        _State("switch.byd_aircon", "off"),
    ])

    result = asyncio.run(
        actions._action_start_ev_charging(
            hass,
            _Entry(),
            {
                "charger_type": "ocpp",
                "ocpp_charger_id": "evse_1",
                "pre_charge_wake_entity": "switch.byd_aircon",
                "pre_charge_wake_duration_seconds": 0,
            },
        )
    )

    assert result is False
    assert hass.services.calls == []


def test_ocpp_set_vehicle_amps_runs_pre_charge_wake_before_start():
    central = _OcppCentralSystem(accepted=True, state_accepted=True)
    hass = _Hass([
        _State("switch.evse_1_charge_control", "off"),
        _State("sensor.evse_1_status_connector", "Preparing"),
        _State("switch.byd_aircon", "off"),
    ])
    hass.data["ocpp"] = {"ocpp-entry": central}

    result = asyncio.run(
        actions._set_vehicle_amps(
            hass,
            _Entry(),
            "ocpp_evse_1",
            16,
            {
                "charger_type": "ocpp",
                "ocpp_charger_id": "evse_1",
                "pre_charge_wake_entity": "switch.byd_aircon",
                "pre_charge_wake_duration_seconds": 0,
            },
        )
    )

    assert result is True
    assert central.calls == [("evse_1", 16.0, 0)]
    assert central.state_calls == [("evse_1", "service_charge_start", True, 1)]
    assert hass.services.calls == [
        ("switch", "turn_on", {"entity_id": "switch.byd_aircon"}),
        ("switch", "turn_off", {"entity_id": "switch.byd_aircon"}),
    ]


def test_direct_ev_start_action_records_manual_ownership(monkeypatch):
    async def fake_start(*args, **kwargs):
        return True

    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start)
    hass = _Hass([])

    result = asyncio.run(
        actions._execute_single_action(
            hass,
            _Entry(),
            "start_ev_charging",
            {
                "charger_type": "generic",
                "charger_switch_entity": "switch.garage_ev",
            },
        )
    )

    assert result is True
    lease = hass.data["power_sync"]["entry-1"]["ev_ownership"]["generic_ev"]
    assert lease["owner_mode"] == "manual"
    assert lease["last_command"]["command"] == "start"


def test_direct_manual_start_preempts_solar_surplus_ownership(monkeypatch):
    from power_sync.automations import ev_ownership

    async def fake_start(*args, **kwargs):
        return True

    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start)
    hass = _Hass([])
    cancelled = []
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "generic_ev": {
            "active": True,
            "params": {
                "dynamic_mode": "solar_surplus",
                "owner_mode": "solar_surplus",
                "charger_type": "generic",
                "notify_on_complete": False,
            },
            "cancel_timer": lambda: cancelled.append(True),
            "session_id": None,
        }
    }
    ev_ownership.claim_ev_ownership(
        hass,
        _Entry(),
        "generic_ev",
        owner_mode="solar_surplus",
    )

    result = asyncio.run(
        actions._execute_single_action(
            hass,
            _Entry(),
            "start_ev_charging",
            {
                "vehicle_id": "generic_ev",
                "charger_type": "generic",
                "charger_switch_entity": "switch.garage_ev",
            },
        )
    )

    assert result is True
    assert cancelled == [True]
    state = actions._dynamic_ev_state["entry-1"]["generic_ev"]
    assert state["params"]["owner_mode"] == "manual"
    lease = hass.data["power_sync"]["entry-1"]["ev_ownership"]["generic_ev"]
    assert lease["owner_mode"] == "manual"


def test_solar_surplus_disable_cannot_release_concurrent_manual_takeover(
    monkeypatch,
):
    from power_sync.automations import ev_ownership

    async def run_race():
        hass = _Hass([])
        actions._dynamic_ev_state.clear()
        actions._dynamic_ev_state["entry-1"] = {
            "generic_ev": {
                "active": True,
                "params": {
                    "dynamic_mode": "solar_surplus",
                    "owner_mode": "solar_surplus",
                    "charger_type": "generic",
                    "notify_on_complete": False,
                },
                "cancel_timer": lambda: None,
                "session_id": None,
            }
        }
        ev_ownership.claim_ev_ownership(
            hass,
            _Entry(),
            "generic_ev",
            owner_mode="solar_surplus",
        )

        start_entered = asyncio.Event()
        allow_start = asyncio.Event()

        async def delayed_start(*args, **kwargs):
            start_entered.set()
            await allow_start.wait()
            return True

        monkeypatch.setattr(
            actions,
            "_action_start_ev_charging",
            delayed_start,
        )

        manual_task = asyncio.create_task(
            actions._execute_single_action(
                hass,
                _Entry(),
                "start_ev_charging",
                {
                    "vehicle_id": "generic_ev",
                    "charger_type": "generic",
                    "charger_switch_entity": "switch.garage_ev",
                },
            )
        )
        await start_entered.wait()
        disable_task = asyncio.create_task(
            actions.stop_solar_surplus_ev_charging(hass, _Entry())
        )
        await asyncio.sleep(0)
        assert not disable_task.done()

        allow_start.set()
        assert await manual_task is True
        assert await disable_task is True
        return hass

    hass = asyncio.run(run_race())

    state = actions._dynamic_ev_state["entry-1"]["generic_ev"]
    assert state["params"]["owner_mode"] == "manual"
    lease = hass.data["power_sync"]["entry-1"]["ev_ownership"]["generic_ev"]
    assert lease["owner_mode"] == "manual"


def test_solar_surplus_disable_preserves_manual_solar_policy(monkeypatch):
    from power_sync.automations import ev_ownership

    hass = _Hass([])
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "automatic_ev": {
            "active": True,
            "params": {
                "dynamic_mode": "solar_surplus",
                "owner_mode": "solar_surplus",
            },
        },
        "manual_ev": {
            "active": True,
            "params": {
                "dynamic_mode": "solar_surplus",
                "owner_mode": "manual_solar_surplus",
            },
        },
    }
    for vehicle_id, owner_mode in (
        ("automatic_ev", "solar_surplus"),
        ("manual_ev", "manual_solar_surplus"),
    ):
        ev_ownership.claim_ev_ownership(
            hass,
            _Entry(),
            vehicle_id,
            owner_mode=owner_mode,
        )

    stopped = []

    async def fake_stop(hass, entry, params):
        vehicle_id = params["vehicle_id"]
        stopped.append(vehicle_id)
        actions._dynamic_ev_state["entry-1"].pop(vehicle_id)
        ev_ownership.release_ev_ownership(hass, entry, vehicle_id)
        return True

    monkeypatch.setattr(
        actions,
        "_action_stop_ev_charging_dynamic",
        fake_stop,
    )

    result = asyncio.run(
        actions.stop_solar_surplus_ev_charging(hass, _Entry())
    )

    assert result is True
    assert stopped == ["automatic_ev"]
    assert set(actions._dynamic_ev_state["entry-1"]) == {"manual_ev"}
    lease = hass.data["power_sync"]["entry-1"]["ev_ownership"]["manual_ev"]
    assert lease["owner_mode"] == "manual_solar_surplus"


def test_direct_ev_start_action_can_skip_ownership(monkeypatch):
    async def fake_start(*args, **kwargs):
        return True

    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start)
    hass = _Hass([])

    result = asyncio.run(
        actions._execute_single_action(
            hass,
            _Entry(),
            "start_ev_charging",
            {
                "charger_type": "generic",
                "charger_switch_entity": "switch.garage_ev",
                "skip_ownership": True,
            },
        )
    )

    assert result is True
    assert "ev_ownership" not in hass.data["power_sync"]["entry-1"]


def test_untracked_dynamic_stop_is_passive_by_default():
    hass = _Hass([_State("switch.evse_1_charge_control", "on")])
    actions._dynamic_ev_state.clear()

    result = asyncio.run(
        actions._action_stop_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_id": "ocpp_evse_1",
                "charger_type": "ocpp",
                "ocpp_charger_id": "evse_1",
            },
        )
    )

    assert result is True
    assert hass.services.calls == []


def test_explicit_untracked_dynamic_stop_controls_ocpp_charger():
    hass = _Hass([_State("switch.evse_1_charge_control", "on")])
    actions._dynamic_ev_state.clear()

    result = asyncio.run(
        actions._action_stop_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_id": "ocpp_evse_1",
                "charger_type": "ocpp",
                "ocpp_charger_id": "evse_1",
                "stop_untracked": True,
            },
        )
    )

    assert result is True
    assert hass.services.calls == [
        ("switch", "turn_off", {"entity_id": "switch.evse_1_charge_control"})
    ]
    assert (
        hass.data["power_sync"]["entry-1"]["ev_last_command"]["ocpp_evse_1"]["command"]
        == "stop"
    )


def test_dynamic_start_claims_business_owner_mode():
    central = _OcppCentralSystem(accepted=True, state_accepted=True)
    hass = _Hass([_State("switch.evse_1_charge_control", "off")])
    hass.data["ocpp"] = {"ocpp-entry": central}
    actions._dynamic_ev_state.clear()

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "ocpp_evse_1",
                "dynamic_mode": "battery_target",
                "owner_mode": "price_level_recovery",
                "charger_type": "ocpp",
                "ocpp_charger_id": "evse_1",
                "max_charge_amps": 16,
            },
            context=None,
        )
    )

    assert result is True
    assert central.calls == [("evse_1", 16.0, 0)]
    assert central.state_calls == [("evse_1", "service_charge_start", True, 1)]
    state = actions._dynamic_ev_state["entry-1"]["ocpp_evse_1"]
    assert state["params"]["dynamic_mode"] == "battery_target"
    assert state["params"]["owner_mode"] == "price_level_recovery"
    assert state["ownership"]["owner_mode"] == "price_level_recovery"
    assert (
        hass.data["power_sync"]["entry-1"]["ev_ownership"]["ocpp_evse_1"]["last_command"]["command"]
        == "start_price_level_recovery"
    )


def test_solar_surplus_dynamic_start_uses_home_power_max_over_idle_tesla_cap(monkeypatch):
    ev_planner = types.ModuleType("power_sync.automations.ev_charging_planner")

    async def plugged_in(*args, **kwargs):
        return True

    async def ev_soc(*args, **kwargs):
        return 50.0

    ev_planner.is_ev_plugged_in = plugged_in
    ev_planner.get_ev_battery_level = ev_soc
    monkeypatch.setitem(
        sys.modules,
        "power_sync.automations.ev_charging_planner",
        ev_planner,
    )

    async def fake_get_tesla_ev_entity(*args, **kwargs):
        return "number.car_charging_amps"

    monkeypatch.setattr(actions, "_get_tesla_ev_entity", fake_get_tesla_ev_entity)
    hass = _Hass([
        _State("number.car_charging_amps", "16", {"min": 5, "max": 16}),
    ])
    hass.data["power_sync"]["entry-1"]["automation_store"] = SimpleNamespace(
        _data={
            "home_power_settings": {
                "max_charge_speed_enabled": True,
                "max_amps_per_phase": 30,
            }
        }
    )
    actions._dynamic_ev_state.clear()

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "VIN123",
                "dynamic_mode": "solar_surplus",
                "charger_type": "tesla",
            },
            context=None,
        )
    )

    assert result is True
    params = actions._dynamic_ev_state["entry-1"]["VIN123"]["params"]
    assert params["max_charge_amps"] == 30
    assert params["max_charge_amps_source"] == "home_power"
    assert params["allow_stale_entity_max_override"] is True


def test_solar_surplus_restart_reclaims_exact_recovered_session(monkeypatch):
    vehicle_id = "5YJTEST0000000001"
    current_entity = "number.car_charge_current"
    charging_entity = "sensor.car_charging"
    ev_planner = types.ModuleType("power_sync.automations.ev_charging_planner")

    async def plugged_in(*args, **kwargs):
        return True

    async def ev_soc(*args, **kwargs):
        return 50.0

    async def at_home(*args, **kwargs):
        return "home"

    ev_planner.is_ev_plugged_in = plugged_in
    ev_planner.get_ev_battery_level = ev_soc
    ev_planner.get_ev_location = at_home
    monkeypatch.setitem(
        sys.modules,
        "power_sync.automations.ev_charging_planner",
        ev_planner,
    )

    ev_session = types.ModuleType("power_sync.automations.ev_charging_session")
    ev_session.get_session_manager = lambda: None
    monkeypatch.setitem(
        sys.modules,
        "power_sync.automations.ev_charging_session",
        ev_session,
    )

    async def active_charger(*args, **kwargs):
        return {
            "max_charge_amps": 32,
            "max_charge_amps_source": "active_charger",
            "voltage": 240,
            "phases": 1,
            "association_known": True,
            "capability_known": True,
        }

    async def charge_current(*args, **kwargs):
        return current_entity

    async def tesla_entity(hass, pattern, *args, **kwargs):
        if "charging_state|charging" in pattern:
            return charging_entity
        return current_entity

    monkeypatch.setattr(
        actions,
        "_resolve_tesla_active_charger_capability",
        active_charger,
    )
    monkeypatch.setattr(
        actions,
        "_resolve_tesla_charge_current_entity",
        charge_current,
    )
    monkeypatch.setattr(actions, "_get_tesla_ev_entity", tesla_entity)

    hass = _Hass([
        _State(current_entity, "1", {"min": 0, "max": 32}),
        _State(charging_entity, "charging"),
    ])
    hass.data["power_sync"]["entry-1"].update(
        {
            "ev_recovered_ownership": {
                vehicle_id: {
                    "owner": "powersync",
                    "owner_mode": "solar_surplus",
                    "charger_type": "tesla",
                    "session_id": "pre-restart-session",
                }
            },
            "ev_recovered_ownership_saved_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    actions._dynamic_ev_state.clear()

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": vehicle_id,
                "dynamic_mode": "solar_surplus",
                "owner_mode": "solar_surplus",
                "charger_type": "tesla",
                "min_charge_amps": 1,
                "max_charge_amps": 32,
            },
            context=None,
        )
    )

    assert result is True
    state = actions._dynamic_ev_state["entry-1"][vehicle_id]
    assert state["current_amps"] == 1
    assert state["charging_started"] is True
    assert state["external_start_detection_armed"] is False
    assert state["external_manual_override"] is False
    assert state["ownership"]["last_command"]["command"] == "resume_solar_surplus"
    assert state["ownership"]["last_commanded_amps"] == 1

    _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 90,
            "grid_power": 0,
            "battery_power": 0,
            "solar_power": 1000,
            "load_power": 1000,
        },
    )

    async def keep_resolved_capability(*args, **kwargs):
        return True

    async def observed_power(*args, **kwargs):
        return 0.24, True

    async def observed_power_kw(*args, **kwargs):
        return 0.24

    monkeypatch.setattr(
        actions,
        "_refresh_dynamic_tesla_charger_capability",
        keep_resolved_capability,
    )
    monkeypatch.setattr(
        actions,
        "_get_observed_ev_power_reading_kw",
        observed_power,
    )
    monkeypatch.setattr(
        actions,
        "_get_observed_ev_power_kw",
        observed_power_kw,
    )

    asyncio.run(
        actions._dynamic_ev_update_surplus(
            hass,
            _Entry(),
            "entry-1",
            vehicle_id,
        )
    )

    assert state["external_manual_override"] is False
    assert state["charging_started"] is True
    assert state["current_amps"] > 0
    assert state["low_surplus_start"] is not None


def test_solar_surplus_restart_rejects_changed_charge_current():
    vehicle_id = "5YJTEST0000000001"
    current_entity = "number.car_charge_current"
    charging_entity = "sensor.car_charging"
    hass = _Hass([
        _State(current_entity, "1", {"min": 0, "max": 32}),
        _State(charging_entity, "charging"),
    ])
    hass.data["power_sync"]["entry-1"].update(
        {
            "ev_recovered_ownership": {
                vehicle_id: {
                    "owner": "powersync",
                    "owner_mode": "solar_surplus",
                    "charger_type": "tesla",
                    "session_id": "pre-restart-session",
                    "last_commanded_amps": 5,
                }
            },
            "ev_recovered_ownership_saved_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    recovered_amps = actions._consume_recovered_solar_surplus_continuation_amps(
        hass,
        _Entry(),
        vehicle_id,
        {
            "charger_type": "tesla",
            "max_charge_amps": 32,
            "tesla_charge_current_entity": current_entity,
            "tesla_charging_state_entity": charging_entity,
        },
    )

    assert recovered_amps is None
    assert hass.data["power_sync"]["entry-1"]["ev_recovered_ownership"] == {}


def test_app_solar_surplus_start_rechecks_disabled_persisted_toggle():
    hass = _Hass([])
    hass.data["power_sync"]["entry-1"]["automation_store"] = SimpleNamespace(
        _data={"solar_surplus_config": {"enabled": False}}
    )
    actions._dynamic_ev_state.clear()

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "VIN123",
                "dynamic_mode": "solar_surplus",
                "owner_mode": "solar_surplus",
                "charger_type": "tesla",
            },
            context=None,
        )
    )

    assert result is False
    assert actions._dynamic_ev_state == {}
    command = hass.data["power_sync"]["entry-1"]["ev_last_command"]["VIN123"]
    assert command["success"] is False
    assert "disabled before the session started" in command["reason"]


def test_solar_surplus_dynamic_start_blocks_full_ev(monkeypatch):
    ev_planner = types.ModuleType("power_sync.automations.ev_charging_planner")

    async def plugged_in(*args, **kwargs):
        return True

    async def full_ev_soc(*args, **kwargs):
        return 100.0

    ev_planner.is_ev_plugged_in = plugged_in
    ev_planner.get_ev_battery_level = full_ev_soc
    monkeypatch.setitem(
        sys.modules,
        "power_sync.automations.ev_charging_planner",
        ev_planner,
    )
    hass = _Hass([])
    actions._dynamic_ev_state.clear()

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "VIN123",
                "dynamic_mode": "solar_surplus",
                "owner_mode": "solar_surplus",
                "charger_type": "tesla",
            },
            context=None,
        )
    )

    assert result is False
    assert actions._dynamic_ev_state == {}
    last_command = hass.data["power_sync"]["entry-1"]["ev_last_command"]["VIN123"]
    assert last_command["command"] == "start_solar_surplus"
    assert last_command["success"] is False
    assert last_command["reason"] == "EV 100.0% >= 100%, already full"


def test_solar_surplus_dynamic_start_blocks_unplugged_ev(monkeypatch):
    ev_planner = types.ModuleType("power_sync.automations.ev_charging_planner")

    async def unplugged(*args, **kwargs):
        return False

    async def ev_soc(*args, **kwargs):
        return 50.0

    ev_planner.is_ev_plugged_in = unplugged
    ev_planner.get_ev_battery_level = ev_soc
    monkeypatch.setitem(
        sys.modules,
        "power_sync.automations.ev_charging_planner",
        ev_planner,
    )
    hass = _Hass([])
    actions._dynamic_ev_state.clear()

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "VIN123",
                "dynamic_mode": "solar_surplus",
                "owner_mode": "solar_surplus",
                "charger_type": "tesla",
            },
            context=None,
        )
    )

    assert result is False
    assert actions._dynamic_ev_state == {}
    last_command = hass.data["power_sync"]["entry-1"]["ev_last_command"]["VIN123"]
    assert last_command["command"] == "start_solar_surplus"
    assert last_command["success"] is False
    assert last_command["reason"] == "vehicle is not plugged in"


def test_solar_surplus_active_default_session_debounces_resolved_vin_unplug(
    monkeypatch,
):
    ev_planner = types.ModuleType("power_sync.automations.ev_charging_planner")

    async def unplugged(*args, **kwargs):
        return False

    async def ev_soc(*args, **kwargs):
        return 50.0

    ev_planner.is_ev_plugged_in = unplugged
    ev_planner.get_ev_battery_level = ev_soc
    monkeypatch.setitem(
        sys.modules,
        "power_sync.automations.ev_charging_planner",
        ev_planner,
    )
    hass = _Hass([])
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        actions.DEFAULT_VEHICLE_ID: {
            "active": True,
            "params": {
                "dynamic_mode": "solar_surplus",
                "owner_mode": "solar_surplus",
                "charger_type": "tesla",
            },
        }
    }

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "VIN123",
                "dynamic_mode": "solar_surplus",
                "owner_mode": "solar_surplus",
                "charger_type": "tesla",
            },
            context=None,
        )
    )

    assert result is True
    assert set(actions._dynamic_ev_state["entry-1"]) == {
        actions.DEFAULT_VEHICLE_ID
    }


def test_dynamic_start_uses_home_power_grid_import_limit(monkeypatch):
    async def fake_live_status(*args, **kwargs):
        return {
            "battery_power": 0,
            "grid_power": 0,
            "solar_power": 0,
            "load_power": 0,
            "ev_power": 0,
            "battery_soc": 100,
        }

    async def fake_start(*args, **kwargs):
        return True

    async def fake_set_vehicle_amps(*args, **kwargs):
        return True

    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)
    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)

    hass = _Hass([])
    hass.data["power_sync"]["entry-1"]["automation_store"] = SimpleNamespace(
        _data={
            "home_power_settings": {
                "phase_type": "single",
                "max_grid_import_amps": 80,
                "default_voltage": 240,
            }
        }
    )
    actions._dynamic_ev_state.clear()

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "VIN123",
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
            },
            context=None,
        )
    )

    assert result is True
    params = actions._dynamic_ev_state["entry-1"]["VIN123"]["params"]
    assert params["max_grid_import_kw"] == 19.2


def test_dynamic_single_smart_schedule_start_waits_for_site_headroom(monkeypatch):
    """A first Smart Schedule EV must not worsen an existing site-cap breach."""
    start_calls: list[int | None] = []
    set_amps_calls: list[int] = []

    async def fake_live_status(*args, **kwargs):
        return {
            "battery_power": -15000,
            "grid_power": 13590,
            "solar_power": 7100,
            "load_power": 5690,
            "ev_power": 0,
            "battery_soc": 78,
        }

    async def fake_start(hass, config_entry, params, context=None):
        start_calls.append(params.get("amps"))
        return True

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append(amps)
        return True

    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)
    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)

    actions._dynamic_ev_state.clear()
    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            _Hass([]),
            _Entry(),
            {
                "vehicle_vin": "ble_tesla_yf88",
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
                "target_battery_charge_kw": 15,
                "max_grid_import_kw": 12.5,
                "min_charge_amps": 5,
                "max_charge_amps": 16,
                "voltage": 240,
                "phases": 3,
            },
            context=None,
        )
    )

    assert result is False
    assert start_calls == []
    assert set_amps_calls == []
    assert actions._dynamic_ev_state == {}


def test_dynamic_single_smart_schedule_start_uses_safe_site_headroom(monkeypatch):
    """The first Tesla command must use the live site budget, not charger max."""
    command_order: list[tuple[str, int | None]] = []

    async def fake_live_status(*args, **kwargs):
        return {
            "battery_power": -15000,
            "grid_power": 13590,
            "solar_power": 7100,
            "load_power": 5690,
            "ev_power": 0,
            "battery_soc": 78,
        }

    async def fake_start(hass, config_entry, params, context=None):
        command_order.append(("start", params.get("amps")))
        return True

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        command_order.append(("set_amps", amps))
        return True

    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)
    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)

    actions._dynamic_ev_state.clear()
    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            _Hass([]),
            _Entry(),
            {
                "vehicle_vin": "ble_tesla_yf88",
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
                "target_battery_charge_kw": 15,
                "max_grid_import_kw": 20,
                "min_charge_amps": 5,
                "max_charge_amps": 16,
                "voltage": 240,
                "phases": 3,
            },
            context=None,
        )
    )

    assert result is True
    assert command_order == [("set_amps", 8), ("start", 8)]
    state = actions._dynamic_ev_state["entry-1"]["ble_tesla_yf88"]
    assert state["current_amps"] == 8
    assert state["params"]["max_grid_import_kw"] == 20


def test_dynamic_single_smart_schedule_start_uses_measured_battery_taper_headroom(
    monkeypatch,
):
    """A taper below 95% must not strand proven site import headroom."""
    command_order: list[tuple[str, int | None]] = []

    async def fake_live_status(*args, **kwargs):
        return {
            "battery_power": -9700,
            "grid_power": 10800,
            "solar_power": 1100,
            "load_power": 2200,
            "ev_power": 0,
            "battery_soc": 82,
        }

    async def fake_start(hass, config_entry, params, context=None):
        command_order.append(("start", params.get("amps")))
        return True

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        command_order.append(("set_amps", amps))
        return True

    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)
    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)

    actions._dynamic_ev_state.clear()
    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            _Hass([]),
            _Entry(),
            {
                "vehicle_vin": "ble_tesla_yf88",
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
                "target_battery_charge_kw": 14.7,
                "max_grid_import_kw": 16,
                "min_charge_amps": 5,
                "max_charge_amps": 15,
                "voltage": 230,
                "phases": 1,
            },
            context=None,
        )
    )

    assert result is True
    assert command_order == [("set_amps", 15), ("start", 15)]
    state = actions._dynamic_ev_state["entry-1"]["ble_tesla_yf88"]
    assert state["current_amps"] == 15
    assert state["_battery_acceptance_learner"]["candidate_kw"] == 9.7
    assert state["_battery_acceptance_learner"]["candidate_samples"] == 1


def test_dynamic_single_smart_schedule_start_waits_for_live_site_status(monkeypatch):
    """Missing live telemetry must not permit an unbounded first Tesla start."""
    start_calls: list[bool] = []
    set_amps_calls: list[int] = []

    async def missing_live_status(*args, **kwargs):
        return None

    async def fake_start(*args, **kwargs):
        start_calls.append(True)
        return True

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append(amps)
        return True

    monkeypatch.setattr(actions, "_get_tesla_live_status", missing_live_status)
    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)

    actions._dynamic_ev_state.clear()
    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            _Hass([]),
            _Entry(),
            {
                "vehicle_vin": "ble_tesla_yf88",
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
                "target_battery_charge_kw": 15,
                "max_grid_import_kw": 20,
                "min_charge_amps": 5,
                "max_charge_amps": 16,
                "voltage": 240,
                "phases": 3,
            },
            context=None,
        )
    )

    assert result is False
    assert start_calls == []
    assert set_amps_calls == []
    assert actions._dynamic_ev_state == {}


def test_dynamic_single_smart_schedule_start_rejects_unusable_coordinator(monkeypatch):
    """Missing, failed, or stale cached telemetry must not authorize a start."""
    start_calls: list[bool] = []
    set_amps_calls: list[int] = []
    now = datetime(2026, 7, 31, 1, 30)

    async def fake_start(*args, **kwargs):
        start_calls.append(True)
        return True

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append(amps)
        return True

    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)
    monkeypatch.setattr(actions.dt_util, "utcnow", lambda: now)

    complete_data = {
        "grid_power": 1.0,
        "solar_power": 7.0,
        "battery_power": -5.0,
        "load_power": 3.0,
        "battery_level": 78,
        "telemetry_ready": True,
    }
    cases = (
        SimpleNamespace(
            data={key: value for key, value in complete_data.items() if key != "grid_power"},
            last_update_success=True,
        ),
        SimpleNamespace(
            data=complete_data,
            last_update_success=True,
        ),
        SimpleNamespace(
            data=complete_data,
            last_update_success=False,
        ),
        SimpleNamespace(
            data=complete_data,
            last_update_success=True,
            last_update_success_time=now - timedelta(minutes=5),
            update_interval=timedelta(seconds=30),
        ),
    )

    for coordinator in cases:
        hass = _Hass([])
        hass.data["power_sync"]["entry-1"]["sungrow_coordinator"] = coordinator
        actions._dynamic_ev_state.clear()
        result = asyncio.run(
            actions._action_start_ev_charging_dynamic(
                hass,
                _Entry(),
                {
                    "vehicle_vin": "ble_tesla_yf88",
                    "dynamic_mode": "battery_target",
                    "owner_mode": "smart_schedule",
                    "charger_type": "tesla",
                    "target_battery_charge_kw": 15,
                    "max_grid_import_kw": 20,
                    "min_charge_amps": 5,
                    "max_charge_amps": 16,
                    "voltage": 240,
                    "phases": 3,
                },
                context=None,
            )
        )

        assert result is False
        assert actions._dynamic_ev_state == {}

    assert start_calls == []
    assert set_amps_calls == []


def test_dynamic_single_smart_schedule_start_refreshes_coordinator(monkeypatch):
    """A production-shaped coordinator must refresh before the first write."""
    command_order: list[tuple[str, int | None]] = []

    class Coordinator:
        data = {
            "grid_power": 13.59,
            "solar_power": 7.1,
            "battery_power": -15.0,
            "load_power": 5.69,
            "battery_level": 78,
            "telemetry_ready": True,
        }
        last_update_success = True
        update_interval = timedelta(seconds=30)

        def __init__(self):
            self.refresh_calls = 0

        async def async_refresh(self):
            self.refresh_calls += 1

    async def fake_start(hass, config_entry, params, context=None):
        command_order.append(("start", params.get("amps")))
        return True

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        command_order.append(("set_amps", amps))
        return True

    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)

    coordinator = Coordinator()
    hass = _Hass([])
    hass.data["power_sync"]["entry-1"]["tesla_coordinator"] = coordinator
    actions._dynamic_ev_state.clear()
    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "ble_tesla_yf88",
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
                "target_battery_charge_kw": 15,
                "max_grid_import_kw": 20,
                "min_charge_amps": 5,
                "max_charge_amps": 16,
                "voltage": 240,
                "phases": 3,
            },
            context=None,
        )
    )

    assert result is True
    assert coordinator.refresh_calls == 1
    assert command_order == [("set_amps", 8), ("start", 8)]


def test_dynamic_start_defers_second_battery_target_vehicle(monkeypatch):
    """A second Smart Schedule must wait for aggregate site allocation."""
    start_calls: list[str | None] = []
    set_amps_calls: list[tuple[str, int]] = []

    async def fake_start(hass, config_entry, params, context=None):
        start_calls.append(params.get("vehicle_vin"))
        return True

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append((vehicle_id, amps))
        return True

    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)

    hass = _Hass([])
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "ble_tesla_flinn": {
            "active": True,
            "current_amps": 6,
            "target_amps": 6,
            "priority": 1,
            "params": {
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
                "vehicle_vin": "ble_tesla_flinn",
                "min_charge_amps": 5,
                "max_charge_amps": 32,
                "voltage": 230,
                "phases": 3,
            },
        }
    }

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "ble_tesla_yf88",
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
                "target_battery_charge_kw": 15,
                "max_grid_import_kw": 12.5,
                "min_charge_amps": 5,
                "max_charge_amps": 32,
                "voltage": 230,
                "phases": 3,
            },
            context=None,
        )
    )

    assert result is True
    assert start_calls == []
    assert set_amps_calls == []
    state = actions._dynamic_ev_state["entry-1"]["ble_tesla_yf88"]
    assert state["current_amps"] == 0
    assert state["charging_started"] is False


def test_dynamic_start_does_not_defer_no_grid_import_vehicle(monkeypatch):
    """No Grid Import retains its existing immediate-start behavior."""
    start_calls: list[str | None] = []

    async def fake_start(hass, config_entry, params, context=None):
        start_calls.append(params.get("vehicle_vin"))
        return True

    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start)

    hass = _Hass([])
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "ble_tesla_flinn": {
            "active": True,
            "current_amps": 6,
            "target_amps": 6,
            "priority": 1,
            "params": {
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
                "vehicle_vin": "ble_tesla_flinn",
                "no_grid_import": True,
                "min_charge_amps": 5,
                "max_charge_amps": 32,
                "voltage": 230,
                "phases": 3,
            },
        }
    }

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "ble_tesla_yf88",
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
                "target_battery_charge_kw": 15,
                "max_grid_import_kw": 12.5,
                "no_grid_import": True,
                "min_charge_amps": 5,
                "max_charge_amps": 32,
                "voltage": 230,
                "phases": 3,
            },
            context=None,
        )
    )

    assert result is True
    assert start_calls == ["ble_tesla_yf88"]
    state = actions._dynamic_ev_state["entry-1"]["ble_tesla_yf88"]
    assert state["current_amps"] > 0
    assert state["charging_started"] is True


def test_dynamic_start_prefers_tesla_site_meter_limit_over_home_power(monkeypatch):
    async def refresh_site():
        return None

    async def fake_live_status(*args, **kwargs):
        return {
            "battery_power": 0,
            "grid_power": 0,
            "solar_power": 0,
            "load_power": 0,
            "ev_power": 0,
            "battery_soc": 100,
        }

    async def fake_start(*args, **kwargs):
        return True

    async def fake_set_vehicle_amps(*args, **kwargs):
        return True

    monkeypatch.setattr(actions, "_get_tesla_live_status", fake_live_status)
    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)

    hass = _Hass([])
    hass.data["power_sync"]["entry-1"]["automation_store"] = SimpleNamespace(
        _data={
            "home_power_settings": {
                "phase_type": "single",
                "max_grid_import_amps": 80,
                "default_voltage": 240,
            }
        }
    )
    hass.data["power_sync"]["entry-1"]["tesla_coordinator"] = SimpleNamespace(
        _site_info_cache={"max_site_meter_power_ac": 16.1},
        data={
            "battery_power": 0,
            "grid_power": 0,
            "solar_power": 0,
            "load_power": 0,
            "battery_level": 100,
        },
        last_update_success=True,
        async_refresh=refresh_site,
    )
    actions._dynamic_ev_state.clear()

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "VIN123",
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
            },
            context=None,
        )
    )

    assert result is True
    params = actions._dynamic_ev_state["entry-1"]["VIN123"]["params"]
    assert params["max_grid_import_kw"] == 16.1


def test_dynamic_deadline_start_never_overrides_unknown_tesla_charger_cap(monkeypatch):
    vehicle_vin = "5YJTEST00000000C3"
    async def fake_get_tesla_ev_entity(*args, **kwargs):
        return "number.car_charging_amps"

    async def fake_start(*args, **kwargs):
        return True

    set_amps_calls: list[int] = []

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append(amps)
        return True

    monkeypatch.setattr(actions, "_get_tesla_ev_entity", fake_get_tesla_ev_entity)
    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)
    hass = _Hass([
        _State("number.car_charging_amps", "5", {"min": 5, "max": 5}),
    ])
    actions._dynamic_ev_state.clear()

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": vehicle_vin,
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
                "max_charge_amps": 32,
                "fixed_charge_amps": 32,
                "allow_stale_entity_max_override": True,
            },
            context=None,
        )
    )

    assert result is True
    state = actions._dynamic_ev_state["entry-1"][vehicle_vin]
    assert state["current_amps"] == 5
    assert state["target_amps"] == 5
    assert state["params"]["max_charge_amps"] == 5
    assert state["params"]["allow_stale_entity_max_override"] is False
    assert set_amps_calls == [5]


def test_dynamic_deadline_start_uses_live_active_charger_cap(monkeypatch):
    vehicle_vin = "5YJTEST00000000C3"
    set_amps_calls: list[int] = []

    async def active_charger(*args, **kwargs):
        return {
            "association_known": True,
            "capability_known": True,
            "max_charge_amps": 10,
            "max_charge_amps_source": "active_charger",
            "voltage": 240,
            "phases": 1,
        }

    async def fake_start(*args, **kwargs):
        return True

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append(amps)
        return True

    monkeypatch.setattr(
        actions,
        "_resolve_tesla_active_charger_capability",
        active_charger,
    )
    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)
    actions._dynamic_ev_state.clear()

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            _Hass([]),
            _Entry(),
            {
                "vehicle_vin": vehicle_vin,
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
                "max_charge_amps": 32,
                "fixed_charge_amps": 32,
            },
            context=None,
        )
    )

    state = actions._dynamic_ev_state["entry-1"][vehicle_vin]
    assert result is True
    assert set_amps_calls == [10]
    assert state["params"]["max_charge_amps"] == 10
    assert state["params"]["max_charge_amps_source"] == "active_charger"
    assert state["params"]["fixed_charge_amps"] == 10


def test_dynamic_deadline_start_preserves_exact_wall_connector_override(monkeypatch):
    """An exact Wall Connector association can command above stale BLE max."""
    vehicle_vin = "5YJTEST00000000C3"
    set_amps_calls: list[tuple[int, bool]] = []

    async def active_charger(*args, **kwargs):
        assert kwargs["configured_max_amps"] == 32
        return {
            "association_known": True,
            "capability_known": True,
            "max_charge_amps": 32,
            "max_charge_amps_source": "active_wall_connector_vehicle",
            "voltage": 240,
            "phases": 1,
            "allow_stale_entity_max_override": True,
            "prefer_vin_scoped_current_control": True,
        }

    async def fake_start(*args, **kwargs):
        return True

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append(
            (amps, params["allow_stale_entity_max_override"])
        )
        return True

    monkeypatch.setattr(
        actions,
        "_resolve_tesla_active_charger_capability",
        active_charger,
    )
    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)
    actions._dynamic_ev_state.clear()

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            _Hass([]),
            _Entry(),
            {
                "vehicle_vin": vehicle_vin,
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
                "max_charge_amps": 32,
                "fixed_charge_amps": 32,
            },
            context=None,
        )
    )

    state = actions._dynamic_ev_state["entry-1"][vehicle_vin]
    assert result is True
    assert set_amps_calls == [(32, True)]
    assert state["params"]["configured_max_charge_amps"] == 32
    assert state["params"]["max_charge_amps"] == 32
    assert state["params"]["fixed_charge_amps"] == 32
    assert state["params"]["allow_stale_entity_max_override"] is True
    assert state["params"]["prefer_vin_scoped_current_control"] is True


def test_dynamic_session_follows_active_charger_cap_changes(monkeypatch):
    vehicle_vin = "5YJTEST00000000C3"
    set_amps_calls: list[int] = []
    charger_caps = iter((10, 32))

    async def active_charger(*args, **kwargs):
        max_charge_amps = next(charger_caps)
        return {
            "association_known": True,
            "capability_known": True,
            "max_charge_amps": max_charge_amps,
            "max_charge_amps_source": "active_charger",
            "voltage": 240,
            "phases": 1,
        }

    async def not_unplugged(*args, **kwargs):
        return False

    async def not_full(*args, **kwargs):
        return None

    async def no_live_status(*args, **kwargs):
        return None

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append(amps)
        return True

    ev_planner = types.ModuleType("power_sync.automations.ev_charging_planner")

    async def home_location(*args, **kwargs):
        return "home"

    ev_planner.get_ev_location = home_location
    monkeypatch.setitem(
        sys.modules,
        "power_sync.automations.ev_charging_planner",
        ev_planner,
    )
    monkeypatch.setattr(
        actions,
        "_resolve_tesla_active_charger_capability",
        active_charger,
    )
    monkeypatch.setattr(
        actions,
        "_clear_ble_dynamic_session_if_unplugged",
        not_unplugged,
    )
    monkeypatch.setattr(actions, "_dynamic_ev_full_soc_reason", not_full)
    monkeypatch.setattr(actions, "_get_tesla_live_status", no_live_status)
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        vehicle_vin: {
            "active": True,
            "current_amps": 32,
            "target_amps": 32,
            "params": {
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "tesla",
                "vehicle_vin": vehicle_vin,
                "min_charge_amps": 5,
                "max_charge_amps": 32,
                "fixed_charge_amps": 32,
                "voltage": 240,
                "phases": 1,
            },
        }
    }

    asyncio.run(
        actions._dynamic_ev_update(
            _Hass([]),
            _Entry(),
            "entry-1",
            vehicle_vin,
        )
    )

    state = actions._dynamic_ev_state["entry-1"][vehicle_vin]
    assert set_amps_calls == [10]
    assert state["current_amps"] == 10
    assert state["target_amps"] == 10
    assert state["params"]["fixed_charge_amps"] == 10
    assert state["params"]["requested_fixed_charge_amps"] == 32

    asyncio.run(
        actions._dynamic_ev_update(
            _Hass([]),
            _Entry(),
            "entry-1",
            vehicle_vin,
        )
    )

    assert set_amps_calls == [10, 32]
    assert state["current_amps"] == 32
    assert state["target_amps"] == 32
    assert state["params"]["fixed_charge_amps"] == 32


def test_dynamic_sigenergy_start_uses_charger_abstraction(monkeypatch):
    set_amps_calls: list[tuple[str, int, str]] = []

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append((vehicle_id, amps, params["charger_type"]))
        return True

    async def fail_tesla_start(*args, **kwargs):
        raise AssertionError("Sigenergy dynamic starts must not use Tesla discovery")

    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)
    monkeypatch.setattr(actions, "_action_start_ev_charging", fail_tesla_start)
    actions._dynamic_ev_state.clear()
    hass = _Hass([])

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_id": "sigenergy_charger",
                "vehicle_vin": "sigenergy_charger",
                "dynamic_mode": "battery_target",
                "owner_mode": "smart_schedule",
                "charger_type": "sigenergy",
                "target_battery_charge_kw": 0,
                "min_charge_amps": 6,
                "max_charge_amps": 32,
            },
            context=None,
        )
    )

    assert result is True
    assert set_amps_calls == [("sigenergy_charger", 32, "sigenergy")]
    state = actions._dynamic_ev_state["entry-1"]["sigenergy_charger"]
    assert state["current_amps"] == 32
    assert state["params"]["target_battery_charge_kw"] == 0


def test_dynamic_update_holds_fixed_deadline_rate(monkeypatch):
    set_amps_calls: list[int] = []

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append(amps)
        return True

    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "VIN123": {
            "active": True,
            "current_amps": 5,
            "target_amps": 5,
            "params": {
                "dynamic_mode": "battery_target",
                "charger_type": "tesla",
                "min_charge_amps": 5,
                "max_charge_amps": 32,
                "fixed_charge_amps": 32,
                "voltage": 230,
                "phases": 1,
            },
        }
    }

    hass = _Hass([])
    asyncio.run(actions._dynamic_ev_update(hass, _Entry(), "entry-1", "VIN123"))

    state = actions._dynamic_ev_state["entry-1"]["VIN123"]
    assert state["current_amps"] == 32
    assert state["target_amps"] == 32
    assert set_amps_calls == [32]


def test_scheduled_dynamic_update_uses_solax_kilowatt_snapshot(monkeypatch):
    """Generic Scheduled dynamic control should consume the canonical SolaX coordinator."""
    set_amps_calls: list[int] = []

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append(amps)
        return True

    async def fake_clear_unplugged(*args, **kwargs):
        return False

    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)
    monkeypatch.setattr(
        actions,
        "_clear_ble_dynamic_session_if_unplugged",
        fake_clear_unplugged,
    )
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "generic_ev": {
            "active": True,
            "current_amps": 5,
            "target_amps": 5,
            "params": {
                "dynamic_mode": "battery_target",
                "owner_mode": "scheduled",
                "charger_type": "generic",
                "min_charge_amps": 5,
                "max_charge_amps": 32,
                "target_battery_charge_kw": 0,
                "voltage": 240,
                "phases": 1,
            },
        }
    }
    hass = _Hass([])
    hass.data["power_sync"]["entry-1"]["solax_coordinator"] = SimpleNamespace(
        last_update_success=True,
        data={
            "battery_level": 70,
            "grid_power": 0.0,
            "solar_power": 10.0,
            "battery_power": -5.0,
            "load_power": 3.0,
        }
    )

    asyncio.run(actions._dynamic_ev_update(hass, _Entry(), "entry-1", "generic_ev"))

    assert set_amps_calls == [26]

    hass.data["power_sync"]["entry-1"]["solax_coordinator"].data.update(
        {
            "grid_power": 12.5,
            "solar_power": 0.0,
            "battery_power": 1.2,
            "load_power": 13.7,
        }
    )

    asyncio.run(actions._dynamic_ev_update(hass, _Entry(), "entry-1", "generic_ev"))

    assert set_amps_calls == [26, 21]


def test_normalized_ev_live_data_rejects_unknown_failed_or_stale_health(monkeypatch):
    now = datetime(2026, 8, 5, 1, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(actions.dt_util, "utcnow", lambda: now)
    complete_data = {
        "battery_level": 70,
        "grid_power": 0.0,
        "solar_power": 10.0,
        "battery_power": -5.0,
        "load_power": 3.0,
    }

    coordinators = (
        SimpleNamespace(data=complete_data),
        SimpleNamespace(data=complete_data, last_update_success=False),
        SimpleNamespace(
            data=complete_data,
            last_update_success=True,
            last_update_success_time=now - timedelta(minutes=5),
            update_interval=timedelta(seconds=30),
        ),
    )

    assert all(
        not actions._coordinator_has_normalized_ev_live_data(coordinator)
        for coordinator in coordinators
    )


def test_scheduled_dynamic_update_does_not_write_without_solax_telemetry(monkeypatch):
    set_amps_calls: list[int] = []

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append(amps)
        return True

    async def fake_clear_unplugged(*args, **kwargs):
        return False

    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)
    monkeypatch.setattr(
        actions,
        "_clear_ble_dynamic_session_if_unplugged",
        fake_clear_unplugged,
    )
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "generic_ev": {
            "active": True,
            "current_amps": 5,
            "target_amps": 5,
            "params": {
                "dynamic_mode": "battery_target",
                "owner_mode": "scheduled",
                "charger_type": "generic",
                "min_charge_amps": 5,
                "max_charge_amps": 32,
                "target_battery_charge_kw": 0,
                "voltage": 240,
                "phases": 1,
            },
        }
    }
    hass = _Hass([])
    hass.data["power_sync"]["entry-1"]["solax_coordinator"] = SimpleNamespace(
        last_update_success=True,
        data={"battery_level": 70, "grid_power": 0.0}
    )

    asyncio.run(actions._dynamic_ev_update(hass, _Entry(), "entry-1", "generic_ev"))

    assert set_amps_calls == []


def test_dynamic_update_skips_sigenergy_evdc_rate_adjustment(monkeypatch):
    set_amps_calls: list[int] = []

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append(amps)
        return True

    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "sigenergy_charger": {
            "active": True,
            "current_amps": 32,
            "target_amps": 32,
            "params": {
                "dynamic_mode": "battery_target",
                "charger_type": "sigenergy",
                "sigenergy_charger_type": "evdc",
                "supports_rate_control": False,
                "min_charge_amps": 6,
                "max_charge_amps": 32,
                "fixed_charge_amps": 16,
                "voltage": 230,
                "phases": 1,
            },
        }
    }

    hass = _Hass([])
    asyncio.run(actions._dynamic_ev_update(hass, _Entry(), "entry-1", "sigenergy_charger"))

    state = actions._dynamic_ev_state["entry-1"]["sigenergy_charger"]
    assert set_amps_calls == []
    assert state["current_amps"] == 32
    assert state["target_amps"] == 32
    assert "rate control is unsupported" in state["reason"]


def test_dynamic_update_uses_detected_sigenergy_evdc_rate_entity(monkeypatch):
    set_amps_calls: list[tuple[int, dict]] = []

    async def fake_set_vehicle_amps(hass, config_entry, vehicle_id, amps, params):
        set_amps_calls.append((amps, dict(params)))
        return True

    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "sigenergy_charger": {
            "active": True,
            "current_amps": 32,
            "target_amps": 32,
            "params": {
                "dynamic_mode": "battery_target",
                "charger_type": "sigenergy",
                "sigenergy_charger_type": "evdc",
                "supports_rate_control": False,
                "min_charge_amps": 6,
                "max_charge_amps": 32,
                "fixed_charge_amps": 16,
                "voltage": 230,
                "phases": 1,
            },
        }
    }

    hass = _Hass([
        _State("number.sigen_inverter_dc_charger_max_charging_power_limit", "25")
    ])
    asyncio.run(actions._dynamic_ev_update(hass, _Entry(), "entry-1", "sigenergy_charger"))

    state = actions._dynamic_ev_state["entry-1"]["sigenergy_charger"]
    assert set_amps_calls == [
        (
            16,
            {
                "dynamic_mode": "battery_target",
                "charger_type": "sigenergy",
                "sigenergy_charger_type": "evdc",
                "supports_rate_control": True,
                "min_charge_amps": 6,
                "max_charge_amps": 32,
                "fixed_charge_amps": 16,
                "voltage": 230,
                "phases": 1,
                "supports_restart_while_plugged": False,
                "control_strategy": "one_shot",
                "solar_control_strategy": "dynamic_rate",
                "charger_capabilities": {
                    "charger_type": "evdc",
                    "supports_start_stop": True,
                    "supports_rate_control": True,
                    "supports_restart_while_plugged": False,
                    "control_strategy": "one_shot",
                    "solar_control_strategy": "dynamic_rate",
                    "sigenergy_charger_charge_power_limit_entity": (
                        "number.sigen_inverter_dc_charger_max_charging_power_limit"
                    ),
                    "sigenergy_charger_discharge_power_limit_entity": "",
                },
                "sigenergy_charger_charge_power_limit_entity": (
                    "number.sigen_inverter_dc_charger_max_charging_power_limit"
                ),
            },
        )
    ]
    assert state["current_amps"] == 16
    assert state["target_amps"] == 16


def test_dynamic_update_clears_unplugged_ble_session(monkeypatch):
    ev_planner = types.ModuleType("power_sync.automations.ev_charging_planner")
    plug_checks: list[str | None] = []

    async def is_ev_plugged_in(*args, vehicle_vin=None, **kwargs):
        plug_checks.append(vehicle_vin)
        return False

    ev_planner.is_ev_plugged_in = is_ev_plugged_in
    monkeypatch.setitem(sys.modules, "power_sync.automations.ev_charging_planner", ev_planner)

    cancelled = []
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "ble_ble_slater": {
            "active": True,
            "current_amps": 10,
            "target_amps": 10,
            "cancel_timer": lambda: cancelled.append(True),
            "session_id": None,
            "params": {
                "dynamic_mode": "battery_target",
                "charger_type": "tesla",
                "vehicle_vin": "ble_ble_slater",
                "vehicle_name": "Slater",
            },
        }
    }

    hass = _Hass([])
    asyncio.run(actions._dynamic_ev_update(hass, _Entry(), "entry-1", "ble_ble_slater"))

    assert actions._dynamic_ev_state == {}
    assert cancelled == [True]
    assert plug_checks == ["ble_ble_slater"]
    assert hass.services.calls == []
    assert (
        hass.data["power_sync"]["entry-1"]["ev_last_command"]["ble_ble_slater"]["command"]
        == "release"
    )
    assert (
        hass.data["power_sync"]["entry-1"]["ev_last_command"]["ble_ble_slater"]["reason"]
        == "vehicle unplugged"
    )


def test_dynamic_update_keeps_plugged_ble_session(monkeypatch):
    ev_planner = types.ModuleType("power_sync.automations.ev_charging_planner")

    async def is_ev_plugged_in(*args, **kwargs):
        return True

    ev_planner.is_ev_plugged_in = is_ev_plugged_in
    monkeypatch.setitem(sys.modules, "power_sync.automations.ev_charging_planner", ev_planner)

    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "ble_ble_phoenix": {
            "active": True,
            "current_amps": 10,
            "target_amps": 10,
            "params": {
                "dynamic_mode": "battery_target",
                "charger_type": "tesla",
                "vehicle_vin": "ble_ble_phoenix",
                "min_charge_amps": 5,
                "max_charge_amps": 32,
                "fixed_charge_amps": 10,
                "voltage": 240,
                "phases": 1,
            },
        }
    }

    hass = _Hass([])
    asyncio.run(actions._dynamic_ev_update(hass, _Entry(), "entry-1", "ble_ble_phoenix"))

    assert "ble_ble_phoenix" in actions._dynamic_ev_state["entry-1"]
    assert actions._dynamic_ev_state["entry-1"]["ble_ble_phoenix"]["current_amps"] == 10
    assert hass.services.calls == []


def test_tesla_set_amps_clamps_to_entity_max_by_default(monkeypatch):
    async def fake_get_tesla_ev_entity(*args, **kwargs):
        return "number.car_charging_amps"

    async def fake_wake(*args, **kwargs):
        return True

    monkeypatch.setattr(actions, "_get_tesla_ev_entity", fake_get_tesla_ev_entity)
    monkeypatch.setattr(actions, "_wake_tesla_ev", fake_wake)
    hass = _Hass([
        _State("number.car_charging_amps", "16", {"min": 5, "max": 16}),
    ])

    result = asyncio.run(
        actions._action_set_ev_charging_amps(
            hass,
            _Entry(),
            {"vehicle_vin": "VIN123", "amps": 30},
        )
    )

    assert result is True
    assert hass.services.calls == [
        ("number", "set_value", {"entity_id": "number.car_charging_amps", "value": 16})
    ]


def test_tesla_cloud_set_amps_honors_entity_positive_floor(monkeypatch):
    async def fake_get_tesla_ev_entity(*args, **kwargs):
        return "number.car_charging_amps"

    async def fake_wake(*args, **kwargs):
        return True

    monkeypatch.setattr(actions, "_get_tesla_ev_entity", fake_get_tesla_ev_entity)
    monkeypatch.setattr(actions, "_wake_tesla_ev", fake_wake)
    hass = _Hass([
        _State("number.car_charging_amps", "1", {"min": 0, "max": 32}),
    ])

    result = asyncio.run(
        actions._action_set_ev_charging_amps(
            hass,
            _Entry(),
            {"vehicle_vin": "VIN123", "amps": 1},
        )
    )

    assert result is True
    assert hass.services.calls == [
        ("number", "set_value", {"entity_id": "number.car_charging_amps", "value": 1})
    ]


def test_tesla_ble_set_amps_honors_entity_positive_floor(monkeypatch):
    async def fake_wake(*args, **kwargs):
        return True

    monkeypatch.setattr(actions, "_is_ble_available", lambda *args: True)
    monkeypatch.setattr(actions, "_wake_tesla_ble", fake_wake)
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={"ev_provider": "tesla_ble"},
    )
    hass = _Hass([
        _State("number.car_charging_amps", "1", {"min": 0, "max": 32}),
    ])

    result = asyncio.run(
        actions._action_set_ev_charging_amps(
            hass,
            entry,
            {"vehicle_vin": "ble_car", "amps": 1},
        )
    )

    assert result is True
    assert hass.services.calls == [
        ("number", "set_value", {"entity_id": "number.car_charging_amps", "value": 1})
    ]


def test_tesla_ble_set_amps_uses_safe_floor_without_proven_bounds(monkeypatch):
    async def fake_wake(*args, **kwargs):
        return True

    monkeypatch.setattr(actions, "_is_ble_available", lambda *args: True)
    monkeypatch.setattr(actions, "_wake_tesla_ble", fake_wake)
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={"ev_provider": "tesla_ble"},
    )
    hass = _Hass([
        _State("number.car_charging_amps", "1", {"max": 32}),
    ])

    result = asyncio.run(
        actions._action_set_ev_charging_amps(
            hass,
            entry,
            {"vehicle_vin": "ble_car", "amps": 1},
        )
    )

    assert result is True
    assert hass.services.calls == [
        ("number", "set_value", {"entity_id": "number.car_charging_amps", "value": 5})
    ]


def test_teslemetry_bt_set_amps_honors_entity_positive_floor(monkeypatch):
    monkeypatch.setattr(
        actions,
        "_resolve_teslemetry_bt_prefix",
        lambda *args: "VIN123",
    )
    monkeypatch.setattr(actions, "_is_teslemetry_bt_available", lambda *args: True)
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={"ev_provider": "teslemetry_bt"},
    )
    hass = _Hass([
        _State(
            "number.VIN123_charge_current_request",
            "1",
            {"min": 0, "max": 32},
        ),
    ])

    result = asyncio.run(
        actions._action_set_ev_charging_amps(
            hass,
            entry,
            {"vehicle_vin": "VIN123", "amps": 1},
        )
    )

    assert result is True
    assert hass.services.calls == [
        (
            "number",
            "set_value",
            {"entity_id": "number.VIN123_charge_current_request", "value": 1},
        )
    ]


def test_resolve_tesla_charge_current_entity_uses_ble_command_path(monkeypatch):
    monkeypatch.setattr(actions, "_is_ble_available", lambda *args: True)
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={"ev_provider": "tesla_ble"},
    )

    entity_id = asyncio.run(
        actions._resolve_tesla_charge_current_entity(
            _Hass([]),
            entry,
            "ble_car",
        )
    )

    assert entity_id == "number.car_charging_amps"


def test_tesla_set_amps_refuses_current_entity_unavailable_after_wake(monkeypatch):
    entity_id = "number.car_charging_amps"

    async def fake_get_tesla_ev_entity(*args, **kwargs):
        return entity_id

    monkeypatch.setattr(actions, "_get_tesla_ev_entity", fake_get_tesla_ev_entity)

    for post_wake_state in ("", "unavailable", "unknown", None):
        hass = _Hass([
            _State(entity_id, "16", {"min": 5, "max": 32}),
        ])

        async def fake_wake(*args, **kwargs):
            if post_wake_state is None:
                hass.states._states.pop(entity_id, None)
            else:
                hass.states.get(entity_id).state = post_wake_state
            return True

        monkeypatch.setattr(actions, "_wake_tesla_ev", fake_wake)

        result = asyncio.run(
            actions._action_set_ev_charging_amps(
                hass,
                _Entry(),
                {"vehicle_vin": "VIN123", "amps": 16},
            )
        )

        assert result is False
        assert hass.services.calls == []


def test_solar_surplus_tesla_set_amps_uses_configured_max_over_idle_entity_cap(monkeypatch):
    async def fake_get_tesla_ev_entity(*args, **kwargs):
        return "number.car_charging_amps"

    async def fake_wake(*args, **kwargs):
        return True

    monkeypatch.setattr(actions, "_get_tesla_ev_entity", fake_get_tesla_ev_entity)
    monkeypatch.setattr(actions, "_wake_tesla_ev", fake_wake)
    hass = _Hass([
        _State("number.car_charging_amps", "16", {"min": 5, "max": 16}),
    ])

    result = asyncio.run(
        actions._action_set_ev_charging_amps(
            hass,
            _Entry(),
            {
                "vehicle_vin": "VIN123",
                "amps": 30,
                "max_charge_amps": 30,
                "allow_stale_entity_max_override": True,
            },
        )
    )

    assert result is True
    assert hass.services.calls == [
        ("number", "set_value", {"entity_id": "number.car_charging_amps", "value": 30})
    ]


def test_solar_surplus_tesla_set_amps_falls_back_after_range_rejection(monkeypatch):
    async def fake_get_tesla_ev_entity(*args, **kwargs):
        return "number.car_charging_amps"

    async def fake_wake(*args, **kwargs):
        return True

    class _RejectFirstRangeServices(_Services):
        async def async_call(self, domain: str, service: str, data: dict, blocking: bool = True):
            self.calls.append((domain, service, data))
            if len(self.calls) == 1:
                raise Exception(
                    "Value 30.0 for number.car_charging_amps is outside valid "
                    "range 5.0 - 16.0"
                )

    monkeypatch.setattr(actions, "_get_tesla_ev_entity", fake_get_tesla_ev_entity)
    monkeypatch.setattr(actions, "_wake_tesla_ev", fake_wake)
    hass = _Hass([
        _State("number.car_charging_amps", "16", {"min": 5, "max": 16}),
    ])
    hass.services = _RejectFirstRangeServices()
    params = {
        "vehicle_vin": "VIN123",
        "amps": 30,
        "max_charge_amps": 30,
        "allow_stale_entity_max_override": True,
    }

    result = asyncio.run(actions._action_set_ev_charging_amps(hass, _Entry(), params))

    assert result is True
    assert hass.services.calls == [
        ("number", "set_value", {"entity_id": "number.car_charging_amps", "value": 30}),
        ("number", "set_value", {"entity_id": "number.car_charging_amps", "value": 16}),
    ]
    assert params["max_charge_amps"] == 16


def test_exact_wall_connector_range_fallback_continues_to_vin_provider(monkeypatch):
    """A stale BLE range may pulse safely before the exact-VIN 32A write."""
    async def awake(*args, **kwargs):
        return True

    async def fleet_current_entity(*args, **kwargs):
        return "number.car_charging_amps"

    class _RejectBleOverrideServices(_Services):
        async def async_call(
            self,
            domain: str,
            service: str,
            data: dict,
            blocking: bool = True,
        ):
            self.calls.append((domain, service, data))
            if data == {
                "entity_id": "number.teslable_charging_amps",
                "value": 32,
            }:
                raise Exception(
                    "Value 32.0 for number.teslable_charging_amps is outside "
                    "valid range 0.0 - 15.0"
                )

    monkeypatch.setattr(
        actions,
        "_get_ev_config",
        lambda _entry: {"ev_provider": actions.EV_PROVIDER_BOTH},
    )
    monkeypatch.setattr(
        actions,
        "_resolve_ble_prefix_for_vehicle",
        lambda *_args, **_kwargs: "teslable",
    )
    monkeypatch.setattr(actions, "_is_ble_available", lambda *_args: True)
    monkeypatch.setattr(actions, "_wake_tesla_ble", awake)
    monkeypatch.setattr(
        actions,
        "_resolve_teslemetry_bt_prefix",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        actions,
        "_is_teslemetry_bt_available",
        lambda *_args: False,
    )
    monkeypatch.setattr(actions, "_is_api_credit_available", lambda *_args: True)
    monkeypatch.setattr(actions, "_get_tesla_ev_entity", fleet_current_entity)
    monkeypatch.setattr(actions, "_wake_tesla_ev", awake)

    hass = _Hass([
        _State(
            "number.teslable_charging_amps",
            "5",
            {"min": 0, "max": 15},
        ),
        _State(
            "number.car_charging_amps",
            "15",
            {"min": 0, "max": 32},
        ),
    ])
    hass.services = _RejectBleOverrideServices()
    params = {
        "charger_type": "tesla",
        "max_charge_amps": 32,
        "allow_stale_entity_max_override": True,
        "prefer_vin_scoped_current_control": True,
    }

    result = asyncio.run(
        actions._set_vehicle_amps_unchecked(
            hass,
            _Entry(),
            "5YJTEST00000000C3",
            32,
            params,
        )
    )

    assert result is True
    assert hass.services.calls == [
        (
            "number",
            "set_value",
            {"entity_id": "number.teslable_charging_amps", "value": 32},
        ),
        (
            "number",
            "set_value",
            {"entity_id": "number.teslable_charging_amps", "value": 15},
        ),
        (
            "number",
            "set_value",
            {"entity_id": "number.car_charging_amps", "value": 32},
        ),
    ]
    assert params["max_charge_amps"] == 32
    assert "_tesla_entity_range_fallback_amps" not in params
    assert actions._phase_applied_amps(params, 32) == 32

    hass.services = _RejectBleOverrideServices()
    conservative_params = {
        "charger_type": "tesla",
        "max_charge_amps": 32,
        "allow_stale_entity_max_override": True,
    }
    conservative_result = asyncio.run(
        actions._set_vehicle_amps_unchecked(
            hass,
            _Entry(),
            "5YJTEST00000000C3",
            32,
            conservative_params,
        )
    )
    assert conservative_result is True
    assert hass.services.calls == [
        (
            "number",
            "set_value",
            {"entity_id": "number.teslable_charging_amps", "value": 32},
        ),
        (
            "number",
            "set_value",
            {"entity_id": "number.teslable_charging_amps", "value": 15},
        ),
    ]
    assert conservative_params["max_charge_amps"] == 15
    assert actions._phase_applied_amps(conservative_params, 32) == 15


def test_dynamic_start_is_blocked_by_manual_owner():
    hass = _Hass([_State("switch.evse_1_charge_control", "off")])
    actions._dynamic_ev_state.clear()
    hass.data["power_sync"]["entry-1"]["ev_ownership"] = {
        "ocpp_evse_1": {"owner": "powersync", "owner_mode": "manual"}
    }

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "ocpp_evse_1",
                "dynamic_mode": "battery_target",
                "owner_mode": "price_level_recovery",
                "allow_ownership_takeover": True,
                "charger_type": "ocpp",
                "ocpp_charger_id": "evse_1",
            },
            context=None,
        )
    )

    assert result is False
    assert hass.services.calls == []
    assert actions._dynamic_ev_state == {}
    last_command = hass.data["power_sync"]["entry-1"]["ev_last_command"]["ocpp_evse_1"]
    assert last_command["command"] == "start_price_level_recovery"
    assert last_command["success"] is False
    assert "manual already owns" in last_command["reason"]


def test_dynamic_start_updates_same_owner_family_without_restarting():
    hass = _Hass([_State("switch.evse_1_charge_control", "on")])
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "ocpp_evse_1": {
            "active": True,
            "params": {
                "dynamic_mode": "battery_target",
                "owner_mode": "price_level_recovery",
                "charger_type": "ocpp",
            },
            "session_id": "sess-1",
        }
    }
    hass.data["power_sync"]["entry-1"]["ev_ownership"] = {
        "ocpp_evse_1": {
            "owner": "powersync",
            "owner_mode": "price_level_recovery",
            "session_id": "sess-1",
        }
    }

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "ocpp_evse_1",
                "dynamic_mode": "battery_target",
                "owner_mode": "price_level_opportunity",
                "charger_type": "ocpp",
                "ocpp_charger_id": "evse_1",
            },
            context=None,
        )
    )

    assert result is True
    assert hass.services.calls == []
    state = actions._dynamic_ev_state["entry-1"]["ocpp_evse_1"]
    assert state["params"]["owner_mode"] == "price_level_opportunity"
    ownership = hass.data["power_sync"]["entry-1"]["ev_ownership"]["ocpp_evse_1"]
    assert ownership["owner_mode"] == "price_level_opportunity"
    assert ownership["last_command"]["command"] == "update_price_level_opportunity"


def test_dynamic_start_is_blocked_by_legacy_foreign_state():
    hass = _Hass([_State("switch.evse_1_charge_control", "off")])
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "ocpp_evse_1": {
            "active": True,
            "params": {
                "dynamic_mode": "solar_surplus",
                "charger_type": "ocpp",
            },
            "session_id": "sess-1",
        }
    }

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "ocpp_evse_1",
                "dynamic_mode": "battery_target",
                "owner_mode": "price_level_recovery",
                "charger_type": "ocpp",
                "ocpp_charger_id": "evse_1",
            },
            context=None,
        )
    )

    assert result is False
    assert hass.services.calls == []
    assert "ocpp_evse_1" in actions._dynamic_ev_state["entry-1"]
    last_command = hass.data["power_sync"]["entry-1"]["ev_last_command"]["ocpp_evse_1"]
    assert last_command["success"] is False
    assert "solar_surplus already owns" in last_command["reason"]


def test_dynamic_start_takes_over_legacy_solar_surplus_when_allowed():
    central = _OcppCentralSystem(accepted=True, state_accepted=True)
    hass = _Hass([_State("switch.evse_1_charge_control", "off")])
    hass.data["ocpp"] = {"ocpp-entry": central}
    cancelled = []
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "ocpp_evse_1": {
            "active": True,
            "params": {
                "dynamic_mode": "solar_surplus",
                "charger_type": "ocpp",
                "ocpp_charger_id": "evse_1",
            },
            "cancel_timer": lambda: cancelled.append(True),
            "session_id": "sess-1",
        }
    }

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "ocpp_evse_1",
                "dynamic_mode": "battery_target",
                "owner_mode": "price_level_recovery",
                "allow_ownership_takeover": True,
                "charger_type": "ocpp",
                "ocpp_charger_id": "evse_1",
                "max_charge_amps": 16,
            },
            context=None,
        )
    )

    assert result is True
    assert central.calls == [("evse_1", 16.0, 0)]
    assert central.state_calls == [
        ("evse_1", "service_charge_stop", False, 1),
        ("evse_1", "service_charge_start", True, 1),
    ]
    assert cancelled == [True]
    state = actions._dynamic_ev_state["entry-1"]["ocpp_evse_1"]
    assert state["params"]["dynamic_mode"] == "battery_target"
    assert state["params"]["owner_mode"] == "price_level_recovery"
    ownership = hass.data["power_sync"]["entry-1"]["ev_ownership"]["ocpp_evse_1"]
    assert ownership["owner_mode"] == "price_level_recovery"


def test_inflight_solar_surplus_update_cannot_command_after_scheduled_takeover(
    monkeypatch,
):
    hass = _Hass([])
    vehicle_id = "ocpp_evse_1"
    cancelled = []
    live_status_requested = asyncio.Event()
    release_live_status = asyncio.Event()
    live_status_calls = 0
    set_amps_calls: list[int] = []

    ev_planner = types.ModuleType("power_sync.automations.ev_charging_planner")

    async def get_ev_location(*args, **kwargs):
        return "home"

    async def get_ev_battery_level(*args, **kwargs):
        return None

    ev_planner.get_ev_location = get_ev_location
    ev_planner.get_ev_battery_level = get_ev_battery_level
    monkeypatch.setitem(
        sys.modules,
        "power_sync.automations.ev_charging_planner",
        ev_planner,
    )

    ev_session = types.ModuleType("power_sync.automations.ev_charging_session")
    ev_session.get_session_manager = lambda: None
    monkeypatch.setitem(
        sys.modules,
        "power_sync.automations.ev_charging_session",
        ev_session,
    )

    async def not_unplugged(*args, **kwargs):
        return False

    async def gated_live_status(*args, **kwargs):
        nonlocal live_status_calls
        live_status_calls += 1
        if live_status_calls == 1:
            live_status_requested.set()
            await release_live_status.wait()
        return {
            "battery_soc": 100,
            "grid_power": -5000,
            "battery_power": 0,
            "solar_power": 0,
            "load_power": 0,
        }

    async def fake_observed_ev_power_kw(*args, **kwargs):
        return 0.0

    async def fake_set_vehicle_amps(
        hass,
        config_entry,
        requested_vehicle_id,
        amps,
        params,
    ):
        set_amps_calls.append(amps)
        return True

    monkeypatch.setattr(
        actions,
        "_clear_ble_dynamic_session_if_unplugged",
        not_unplugged,
    )
    monkeypatch.setattr(actions, "_get_tesla_live_status", gated_live_status)
    monkeypatch.setattr(
        actions,
        "_get_observed_ev_power_kw",
        fake_observed_ev_power_kw,
    )
    monkeypatch.setattr(actions, "_set_vehicle_amps", fake_set_vehicle_amps)

    old_state = _solar_surplus_state(current_amps=8)
    old_state["params"].update(
        {
            "owner_mode": "solar_surplus",
            "charger_type": "ocpp",
            "ocpp_charger_id": "evse_1",
        }
    )
    old_state["cancel_timer"] = lambda: cancelled.append(True)
    old_state["session_id"] = "solar-session"
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {vehicle_id: old_state}

    async def run_takeover():
        stale_update = asyncio.create_task(
            actions._dynamic_ev_update_surplus(
                hass,
                _Entry(),
                "entry-1",
                vehicle_id,
            )
        )
        await live_status_requested.wait()
        takeover_result = await actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": vehicle_id,
                "dynamic_mode": "battery_target",
                "owner_mode": "scheduled",
                "allow_ownership_takeover": True,
                "charger_type": "ocpp",
                "ocpp_charger_id": "evse_1",
                "max_charge_amps": 16,
            },
            context=None,
        )
        replacement_state = actions._dynamic_ev_state["entry-1"][vehicle_id]
        release_live_status.set()
        await stale_update
        return takeover_result, replacement_state

    takeover_result, replacement_state = asyncio.run(run_takeover())

    assert takeover_result is True
    assert cancelled == [True]
    assert set_amps_calls == [0, 16]
    assert replacement_state["params"]["owner_mode"] == "scheduled"
    assert replacement_state["current_amps"] == 16


def test_dynamic_start_updates_solar_surplus_owner_when_same_mode_allowed(monkeypatch):
    ev_planner = types.ModuleType("power_sync.automations.ev_charging_planner")

    async def is_ev_plugged_in(*args, **kwargs):
        return True

    ev_planner.is_ev_plugged_in = is_ev_plugged_in
    monkeypatch.setitem(
        sys.modules,
        "power_sync.automations.ev_charging_planner",
        ev_planner,
    )

    hass = _Hass([_State("switch.evse_1_charge_control", "off")])
    actions._dynamic_ev_state.clear()
    cancelled = []
    actions._dynamic_ev_state["entry-1"] = {
        "ocpp_evse_1": {
            "active": True,
            "paused": True,
            "paused_reason": "Waiting for battery to reach 95% (currently 75%)",
            "reason": "stale standalone solar policy",
            "cancel_timer": lambda: cancelled.append(True),
            "params": {
                "dynamic_mode": "solar_surplus",
                "owner_mode": "solar_surplus",
                "charger_type": "ocpp",
                "ocpp_charger_id": "evse_1",
                "home_battery_minimum": 95,
                "min_battery_soc": 95,
                "pause_below_soc": 85,
                "household_buffer_kw": 1.25,
                "sustained_surplus_minutes": 7,
            },
            "session_id": "sess-1",
        }
    }
    hass.data["power_sync"]["entry-1"]["ev_ownership"] = {
        "ocpp_evse_1": {
            "owner": "powersync",
            "owner_mode": "solar_surplus",
            "session_id": "sess-1",
        }
    }

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "ocpp_evse_1",
                "dynamic_mode": "solar_surplus",
                "owner_mode": "smart_schedule_solar_surplus",
                "allow_ownership_takeover": True,
                "charger_type": "ocpp",
                "ocpp_charger_id": "evse_1",
                "home_battery_minimum": 20,
                "min_battery_soc": 20,
                "pause_below_soc": 10,
            },
            context=None,
        )
    )

    assert result is True
    assert hass.services.calls == []
    assert cancelled == []
    state = actions._dynamic_ev_state["entry-1"]["ocpp_evse_1"]
    assert state["params"]["owner_mode"] == "smart_schedule_solar_surplus"
    assert state["params"]["home_battery_minimum"] == 20
    assert state["params"]["min_battery_soc"] == 20
    assert state["params"]["pause_below_soc"] == 10
    assert state["params"]["household_buffer_kw"] == 1.25
    assert state["params"]["sustained_surplus_minutes"] == 7
    assert state["paused"] is False
    assert state["paused_reason"] is None
    assert state["reason"] == ""
    ownership = hass.data["power_sync"]["entry-1"]["ev_ownership"]["ocpp_evse_1"]
    assert ownership["owner_mode"] == "smart_schedule_solar_surplus"
    assert ownership["last_command"]["command"] == "update_smart_schedule_solar_surplus"


def test_solar_surplus_takeover_without_pause_uses_incoming_floor(monkeypatch):
    ev_planner = types.ModuleType("power_sync.automations.ev_charging_planner")

    async def is_ev_plugged_in(*args, **kwargs):
        return True

    ev_planner.is_ev_plugged_in = is_ev_plugged_in
    monkeypatch.setitem(
        sys.modules,
        "power_sync.automations.ev_charging_planner",
        ev_planner,
    )

    hass = _Hass([_State("switch.evse_1_charge_control", "off")])
    actions._dynamic_ev_state["entry-1"] = {
        "ocpp_evse_1": {
            "active": True,
            "paused": True,
            "params": {
                "dynamic_mode": "solar_surplus",
                "owner_mode": "solar_surplus",
                "charger_type": "ocpp",
                "ocpp_charger_id": "evse_1",
                "home_battery_minimum": 95,
                "min_battery_soc": 95,
            },
        }
    }
    hass.data["power_sync"]["entry-1"]["ev_ownership"] = {
        "ocpp_evse_1": {
            "owner": "powersync",
            "owner_mode": "solar_surplus",
        }
    }

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "ocpp_evse_1",
                "dynamic_mode": "solar_surplus",
                "owner_mode": "smart_schedule_solar_surplus",
                "allow_ownership_takeover": True,
                "charger_type": "ocpp",
                "ocpp_charger_id": "evse_1",
                "min_battery_soc": 20,
            },
            context=None,
        )
    )

    assert result is True
    state = actions._dynamic_ev_state["entry-1"]["ocpp_evse_1"]
    assert state["params"]["home_battery_minimum"] == 20
    assert state["params"]["min_battery_soc"] == 20
    assert state["params"]["pause_below_soc"] == 10


def test_solar_surplus_takeover_preserves_default_runtime_identity(monkeypatch):
    ev_planner = types.ModuleType("power_sync.automations.ev_charging_planner")

    async def is_ev_plugged_in(*args, **kwargs):
        return True

    ev_planner.is_ev_plugged_in = is_ev_plugged_in
    monkeypatch.setitem(
        sys.modules,
        "power_sync.automations.ev_charging_planner",
        ev_planner,
    )

    hass = _Hass([_State("switch.evse_1_charge_control", "off")])
    actions._dynamic_ev_state["entry-1"] = {
        "_default": {
            "active": True,
            "params": {
                "dynamic_mode": "solar_surplus",
                "owner_mode": "solar_surplus",
                "charger_type": "ocpp",
                "ocpp_charger_id": "evse_1",
                "min_battery_soc": 95,
            },
        }
    }
    hass.data["power_sync"]["entry-1"]["ev_ownership"] = {
        "_default": {
            "owner": "powersync",
            "owner_mode": "solar_surplus",
        }
    }

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": "ocpp_evse_1",
                "dynamic_mode": "solar_surplus",
                "owner_mode": "smart_schedule_solar_surplus",
                "allow_ownership_takeover": True,
                "charger_type": "ocpp",
                "ocpp_charger_id": "evse_1",
                "min_battery_soc": 20,
            },
            context=None,
        )
    )

    assert result is True
    assert set(actions._dynamic_ev_state["entry-1"]) == {"_default"}
    assert set(hass.data["power_sync"]["entry-1"]["ev_ownership"]) == {
        "_default"
    }
    assert (
        actions._dynamic_ev_state["entry-1"]["_default"]["params"]["owner_mode"]
        == "smart_schedule_solar_surplus"
    )


def test_smart_schedule_solar_refresh_preserves_owned_floor(monkeypatch):
    hass = _Hass([])
    hass.data["power_sync"]["entry-1"]["automation_store"] = SimpleNamespace(
        _data={
            "solar_surplus_config": {
                "enabled": False,
                "home_battery_minimum": 95,
                "household_buffer_kw": 1.25,
            }
        }
    )
    params = {
        "dynamic_mode": "solar_surplus",
        "owner_mode": "smart_schedule_solar_surplus",
        "home_battery_minimum": 20,
        "min_battery_soc": 20,
        "pause_below_soc": 10,
        "household_buffer_kw": 0.5,
    }

    refreshed = actions._refresh_solar_surplus_runtime_params(
        hass,
        "entry-1",
        params,
    )

    assert refreshed["home_battery_minimum"] == 20
    assert refreshed["min_battery_soc"] == 20
    assert refreshed["pause_below_soc"] == 10
    assert refreshed["household_buffer_kw"] == 1.25


def test_manual_session_replaces_existing_owner_without_physical_stop():
    hass = _Hass([_State("switch.ev_charge", "on")])
    cancelled = []
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "VIN123": {
            "active": True,
            "params": {"dynamic_mode": "solar_surplus"},
            "cancel_timer": lambda: cancelled.append(True),
            "session_id": None,
        }
    }

    asyncio.run(
        actions.record_manual_ev_charging_session(
            hass,
            _Entry(),
            "VIN123",
            {"charger_type": "tesla"},
        )
    )

    state = actions._dynamic_ev_state["entry-1"]["VIN123"]
    assert state["active"] is True
    assert state["charging_started"] is True
    assert state["params"]["dynamic_mode"] == "manual"
    assert state["ownership"]["owner_mode"] == "manual"
    assert state["ownership"]["last_command"]["command"] == "start"
    assert hass.data["power_sync"]["entry-1"]["ev_ownership"]["VIN123"]["owner_mode"] == "manual"
    assert cancelled == [True]
    assert hass.services.calls == []


def test_manual_session_records_quick_control_metadata():
    hass = _Hass([_State("switch.ev_charge", "on")])
    actions._dynamic_ev_state.clear()

    asyncio.run(
        actions.record_manual_ev_charging_session(
            hass,
            _Entry(),
            "VIN123",
            {
                "charger_type": "tesla",
                "source_mode": "grid_allowed",
                "duration_minutes": 90,
                "expires_at": "2026-05-01T01:30:00+00:00",
                "quick_control": True,
            },
        )
    )

    state = actions._dynamic_ev_state["entry-1"]["VIN123"]
    ownership = hass.data["power_sync"]["entry-1"]["ev_ownership"]["VIN123"]
    assert state["params"]["source_mode"] == "grid_allowed"
    assert state["params"]["duration_minutes"] == 90
    assert state["params"]["expires_at"] == "2026-05-01T01:30:00+00:00"
    assert state["params"]["quick_control"] is True
    assert ownership["source_mode"] == "grid_allowed"
    assert ownership["duration_minutes"] == 90
    assert ownership["expires_at"] == "2026-05-01T01:30:00+00:00"
    assert ownership["quick_control"] is True


def test_solar_surplus_stop_delay_reduces_to_minimum_on_first_low_sample(monkeypatch):
    hass = _Hass([])
    vehicle_id = "VIN123"
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        vehicle_id: _solar_surplus_state(current_amps=8),
    }
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 40,
            "grid_power": 2000,
            "battery_power": 0,
            "solar_power": 0,
            "load_power": 0,
        },
    )

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    state = actions._dynamic_ev_state["entry-1"][vehicle_id]
    assert state["current_amps"] == 5
    assert state["target_amps"] == 5
    assert state["low_surplus_start"] is not None
    assert set_amps_calls == [5]


def test_solar_surplus_update_stops_full_ev(monkeypatch):
    hass = _Hass([])
    vehicle_id = "VIN123"
    actions._dynamic_ev_state.clear()
    state = _solar_surplus_state(current_amps=8)
    state["params"].update({
        "charger_type": "generic",
        "notify_on_complete": False,
    })
    actions._dynamic_ev_state["entry-1"] = {vehicle_id: state}
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 40,
            "grid_power": -5000,
            "battery_power": 0,
            "solar_power": 0,
            "load_power": 0,
        },
        ev_soc=100.0,
    )

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    assert actions._dynamic_ev_state == {}
    assert set_amps_calls == [0]
    last_command = hass.data["power_sync"]["entry-1"]["ev_last_command"][vehicle_id]
    assert last_command["command"] == "stop"
    assert last_command["reason"] == "already full"


def test_solar_surplus_parallel_reserve_sheds_to_minimum_while_battery_charging(monkeypatch):
    hass = _Hass([])
    vehicle_id = "VIN123"
    actions._dynamic_ev_state.clear()
    state = _solar_surplus_state(current_amps=7)
    state["params"].update(
        {
            "household_buffer_kw": 2.0,
            "allow_parallel_charging": True,
            "max_battery_charge_rate_kw": 5.0,
            "min_battery_soc": 20,
            "pause_below_soc": 10,
        }
    )
    actions._dynamic_ev_state["entry-1"] = {vehicle_id: state}
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 38,
            "grid_power": 30,
            "battery_power": -4530,
            "solar_power": 0,
            "load_power": 0,
        },
    )

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    state = actions._dynamic_ev_state["entry-1"][vehicle_id]
    assert state["current_amps"] == 5
    assert state["low_surplus_start"] is not None
    assert set_amps_calls == [5]


def test_solar_surplus_full_battery_curtailment_ramps_from_visible_headroom(monkeypatch):
    hass = _Hass([])
    vehicle_id = "VIN123"
    actions._dynamic_ev_state.clear()
    state = _solar_surplus_state(current_amps=7)
    state["params"].update(
        {
            "household_buffer_kw": 1.5,
            "allow_parallel_charging": True,
            "max_battery_charge_rate_kw": 3.0,
            "min_battery_soc": 20,
            "pause_below_soc": 10,
        }
    )
    actions._dynamic_ev_state["entry-1"] = {vehicle_id: state}
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 100,
            "grid_power": 50,
            "battery_power": -2350,
            "solar_power": 0,
            "load_power": 0,
            "is_curtailed": True,
        },
    )

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    state = actions._dynamic_ev_state["entry-1"][vehicle_id]
    assert state["current_amps"] == 17
    assert state.get("low_surplus_start") is None
    assert set_amps_calls == [17]


def test_solar_surplus_below_floor_can_start_with_strict_surplus(monkeypatch):
    start_calls = []

    async def fake_start(*args, **kwargs):
        start_calls.append((args, kwargs))
        return True

    hass = _Hass([])
    vehicle_id = "VIN123"
    actions._dynamic_ev_state.clear()
    state = _solar_surplus_state(current_amps=0)
    state["charging_started"] = False
    state["high_surplus_start"] = datetime.now() - timedelta(minutes=4)
    state["params"].update(
        {
            "household_buffer_kw": 1.0,
            "allow_parallel_charging": True,
            "max_battery_charge_rate_kw": 5.0,
            "min_battery_soc": 20,
            "pause_below_soc": 10,
            "notify_on_start": False,
        }
    )
    actions._dynamic_ev_state["entry-1"] = {vehicle_id: state}
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 15,
            "grid_power": -4000,
            "battery_power": -5000,
            "solar_power": 0,
            "load_power": 0,
        },
    )
    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start)
    monkeypatch.setattr(actions, "_is_vehicle_charge_complete", lambda *args, **kwargs: False)

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    state = actions._dynamic_ev_state["entry-1"][vehicle_id]
    assert state["charging_started"] is True
    assert state["parallel_charging_mode"] is True
    assert start_calls
    assert set_amps_calls[-1] > 0


def test_solar_surplus_below_floor_continues_with_strict_surplus(monkeypatch):
    hass = _Hass([])
    vehicle_id = "VIN123"
    actions._dynamic_ev_state.clear()
    state = _solar_surplus_state(current_amps=7)
    state["parallel_charging_mode"] = True
    state["params"].update(
        {
            "household_buffer_kw": 1.0,
            "allow_parallel_charging": True,
            "max_battery_charge_rate_kw": 5.0,
            "min_battery_soc": 20,
            "pause_below_soc": 10,
        }
    )
    actions._dynamic_ev_state["entry-1"] = {vehicle_id: state}
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 15,
            "grid_power": -4000,
            "battery_power": -5000,
            "solar_power": 0,
            "load_power": 0,
        },
    )

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    state = actions._dynamic_ev_state["entry-1"][vehicle_id]
    assert state.get("paused") is not True
    assert state["parallel_charging_mode"] is True
    assert state["current_amps"] > 0
    assert set_amps_calls[-1] > 0


def test_solar_surplus_below_floor_no_reserved_surplus_uses_stop_delay(monkeypatch):
    hass = _Hass([])
    vehicle_id = "VIN123"
    actions._dynamic_ev_state.clear()
    state = _solar_surplus_state(current_amps=6)
    state["parallel_charging_mode"] = True
    state["params"].update(
        {
            "household_buffer_kw": 1.2,
            "allow_parallel_charging": True,
            "max_battery_charge_rate_kw": 3.0,
            "min_battery_soc": 20,
            "pause_below_soc": 10,
            "stop_delay_minutes": 10,
        }
    )
    actions._dynamic_ev_state["entry-1"] = {vehicle_id: state}
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 8,
            "grid_power": 0,
            "battery_power": -1110,
            "solar_power": 0,
            "load_power": 0,
        },
    )

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    state = actions._dynamic_ev_state["entry-1"][vehicle_id]
    assert state["current_amps"] == 5
    assert state["low_surplus_start"] is not None
    assert set_amps_calls == [5]


def test_solar_surplus_below_floor_pauses_when_battery_discharges(monkeypatch):
    hass = _Hass([])
    vehicle_id = "VIN123"
    actions._dynamic_ev_state.clear()
    state = _solar_surplus_state(current_amps=7)
    state["parallel_charging_mode"] = True
    state["params"].update(
        {
            "household_buffer_kw": 1.0,
            "allow_parallel_charging": True,
            "max_battery_charge_rate_kw": 5.0,
            "min_battery_soc": 20,
            "pause_below_soc": 10,
        }
    )
    actions._dynamic_ev_state["entry-1"] = {vehicle_id: state}
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 15,
            "grid_power": -2000,
            "battery_power": 800,
            "solar_power": 0,
            "load_power": 0,
        },
    )

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    state = actions._dynamic_ev_state["entry-1"][vehicle_id]
    assert state["paused"] is True
    assert "battery is discharging" in state["paused_reason"]
    assert state["current_amps"] == 0
    assert set_amps_calls == [0]


def test_solar_surplus_below_floor_pauses_on_grid_import(monkeypatch):
    hass = _Hass([])
    vehicle_id = "VIN123"
    actions._dynamic_ev_state.clear()
    state = _solar_surplus_state(current_amps=7)
    state["parallel_charging_mode"] = True
    state["params"].update(
        {
            "household_buffer_kw": 1.0,
            "allow_parallel_charging": True,
            "max_battery_charge_rate_kw": 5.0,
            "grid_import_tolerance_kw": 0.1,
            "min_battery_soc": 20,
            "pause_below_soc": 10,
        }
    )
    actions._dynamic_ev_state["entry-1"] = {vehicle_id: state}
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 15,
            "grid_power": 900,
            "battery_power": -5000,
            "solar_power": 0,
            "load_power": 0,
        },
    )

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    state = actions._dynamic_ev_state["entry-1"][vehicle_id]
    assert state["paused"] is True
    assert "grid import" in state["paused_reason"]
    assert state["current_amps"] == 0
    assert set_amps_calls == [0]


def test_solar_surplus_stop_delay_clamps_to_tesla_hardware_minimum(monkeypatch):
    hass = _Hass([])
    vehicle_id = "VIN123"
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        vehicle_id: _solar_surplus_state(current_amps=8),
    }
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 40,
            "grid_power": -10,
            "battery_power": 380,
            "solar_power": 0,
            "load_power": 0,
        },
    )

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    state = actions._dynamic_ev_state["entry-1"][vehicle_id]
    assert state["current_amps"] == 5
    assert state["low_surplus_start"] is not None
    assert set_amps_calls == [5]


def test_solar_surplus_stop_delay_uses_cloud_entity_positive_floor(monkeypatch):
    entity_id = "number.car_charging_amps"
    hass = _Hass([
        _State(entity_id, "7", {"min": 0, "max": 32}),
    ])
    vehicle_id = "VIN123"
    actions._dynamic_ev_state.clear()
    state = _solar_surplus_state(current_amps=7)
    state["params"].update({
        "min_charge_amps": 5,
        "tesla_charge_current_entity": entity_id,
    })
    actions._dynamic_ev_state["entry-1"] = {vehicle_id: state}
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 40,
            "grid_power": -10,
            "battery_power": 380,
            "solar_power": 0,
            "load_power": 0,
        },
    )

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    state = actions._dynamic_ev_state["entry-1"][vehicle_id]
    assert state["current_amps"] == 1
    assert state["low_surplus_start"] is not None
    assert set_amps_calls == [1]


def test_solar_surplus_external_tesla_start_suspends_until_charge_ends(monkeypatch):
    stop_command_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    hass = _Hass([
        _State(
            "sensor.VIN123_charging_state",
            "charging",
            last_changed=stop_command_at + timedelta(seconds=30),
        ),
    ])
    vehicle_id = "VIN123"
    actions._dynamic_ev_state.clear()
    state = _solar_surplus_state(current_amps=0)
    state["low_surplus_start"] = datetime.now() - timedelta(minutes=11)
    state["params"]["notify_on_complete"] = False
    state["params"]["owner_mode"] = "solar_surplus"
    state["external_start_detection_armed"] = False
    state["last_stop_command_at"] = stop_command_at
    actions._dynamic_ev_state["entry-1"] = {vehicle_id: state}
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 100,
            "grid_power": 0,
            "battery_power": 1000,
            "solar_power": 600,
            "load_power": 1600,
        },
    )

    observation = {"power_kw": 0.24}

    async def observed_power(*args, **kwargs):
        return observation["power_kw"], True

    monkeypatch.setattr(
        actions,
        "_get_observed_ev_power_reading_kw",
        observed_power,
    )

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    override_state = actions._dynamic_ev_state["entry-1"][vehicle_id]
    assert override_state["params"]["owner_mode"] == "solar_surplus"
    assert override_state["external_manual_override"] is True
    assert "rate control suspended" in override_state["reason"]
    assert set_amps_calls == []

    hass.states._states["sensor.VIN123_charging_state"].state = "stopped"
    observation["power_kw"] = 0

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    resumed_state = actions._dynamic_ev_state["entry-1"][vehicle_id]
    assert resumed_state["external_manual_override"] is False
    assert resumed_state["external_start_detection_armed"] is True
    assert resumed_state["charging_started"] is False
    assert set_amps_calls == []


def test_solar_surplus_external_tessie_start_uses_vin_device_entity(monkeypatch):
    stop_command_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    charging_entity = "sensor.n3bula_charging"
    hass = _Hass([
        _State(
            charging_entity,
            "charging",
            last_changed=stop_command_at + timedelta(seconds=30),
        ),
    ])
    vehicle_id = "LRW3F7FS1NC484342"
    actions._dynamic_ev_state.clear()
    state = _solar_surplus_state(current_amps=0)
    state["low_surplus_start"] = datetime.now() - timedelta(minutes=11)
    state["params"]["notify_on_complete"] = False
    state["params"]["owner_mode"] = "solar_surplus"
    state["params"]["tesla_charging_state_entity"] = charging_entity
    state["entity_max_rechecked"] = True
    state["external_start_detection_armed"] = False
    state["last_stop_command_at"] = stop_command_at
    actions._dynamic_ev_state["entry-1"] = {vehicle_id: state}
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 100,
            "grid_power": 0,
            "battery_power": 1000,
            "solar_power": 600,
            "load_power": 1600,
        },
    )

    async def observed_power(*args, **kwargs):
        return 0.24, True

    monkeypatch.setattr(
        actions,
        "_get_observed_ev_power_reading_kw",
        observed_power,
    )

    async def keep_resolved_capability(*args, **kwargs):
        return True

    monkeypatch.setattr(
        actions,
        "_refresh_dynamic_tesla_charger_capability",
        keep_resolved_capability,
    )

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    override_state = actions._dynamic_ev_state["entry-1"][vehicle_id]
    assert override_state["external_manual_override"] is True
    assert "rate control suspended" in override_state["reason"]
    assert set_amps_calls == []


def test_solar_surplus_external_tesla_override_survives_power_telemetry_gap(
    monkeypatch,
):
    hass = _Hass([
        _State("sensor.VIN123_charging_state", "charging"),
    ])
    vehicle_id = "VIN123"
    actions._dynamic_ev_state.clear()
    state = _solar_surplus_state(current_amps=0)
    state["external_manual_override"] = True
    state["params"]["owner_mode"] = "solar_surplus"
    actions._dynamic_ev_state["entry-1"] = {vehicle_id: state}
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 100,
            "grid_power": -5000,
            "battery_power": 0,
            "solar_power": 0,
            "load_power": 0,
        },
    )

    async def unavailable_power(*args, **kwargs):
        return 0.0, False

    monkeypatch.setattr(
        actions,
        "_get_observed_ev_power_reading_kw",
        unavailable_power,
    )

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    assert state["external_manual_override"] is True
    assert "rate control suspended" in state["reason"]
    assert set_amps_calls == []


def test_solar_surplus_external_tesla_override_survives_state_telemetry_gap(
    monkeypatch,
):
    charging_entity = "sensor.n3bula_charging"
    hass = _Hass([
        _State(charging_entity, "unavailable"),
    ])
    vehicle_id = "VIN123"
    actions._dynamic_ev_state.clear()
    state = _solar_surplus_state(current_amps=0)
    state["external_manual_override"] = True
    state["params"]["owner_mode"] = "solar_surplus"
    state["params"]["tesla_charging_state_entity"] = charging_entity
    actions._dynamic_ev_state["entry-1"] = {vehicle_id: state}
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 100,
            "grid_power": -5000,
            "battery_power": 0,
            "solar_power": 0,
            "load_power": 0,
        },
    )

    async def observed_power(*args, **kwargs):
        return 2.0, True

    monkeypatch.setattr(
        actions,
        "_get_observed_ev_power_reading_kw",
        observed_power,
    )

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    assert state["external_manual_override"] is True
    assert "rate control suspended" in state["reason"]
    assert set_amps_calls == []


def test_solar_surplus_does_not_mistake_post_stop_lag_for_manual_start(monkeypatch):
    stop_command_at = datetime.now(timezone.utc)
    hass = _Hass([
        _State(
            "sensor.VIN123_charging_state",
            "charging",
            last_changed=stop_command_at - timedelta(seconds=30),
        ),
    ])
    vehicle_id = "VIN123"
    actions._dynamic_ev_state.clear()
    state = _solar_surplus_state(current_amps=0)
    state["low_surplus_start"] = datetime.now() - timedelta(minutes=11)
    state["external_start_detection_armed"] = False
    state["last_stop_command_at"] = stop_command_at
    actions._dynamic_ev_state["entry-1"] = {vehicle_id: state}
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 100,
            "grid_power": 0,
            "battery_power": 1000,
            "solar_power": 600,
            "load_power": 1600,
        },
    )

    async def observed_power(*args, **kwargs):
        return 0.24, True

    monkeypatch.setattr(
        actions,
        "_get_observed_ev_power_reading_kw",
        observed_power,
    )

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    assert state.get("external_manual_override") is not True
    assert set_amps_calls == [0]


def test_solar_surplus_stop_delay_stops_after_elapsed_delay(monkeypatch):
    hass = _Hass([])
    vehicle_id = "VIN123"
    actions._dynamic_ev_state.clear()
    state = _solar_surplus_state(current_amps=8)
    state["low_surplus_start"] = datetime.now() - timedelta(minutes=6)
    actions._dynamic_ev_state["entry-1"] = {vehicle_id: state}
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 40,
            "grid_power": 2000,
            "battery_power": 0,
            "solar_power": 0,
            "load_power": 0,
        },
    )

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    assert actions._dynamic_ev_state["entry-1"][vehicle_id]["current_amps"] == 0
    assert set_amps_calls == [0]


def test_solar_surplus_restarts_tesla_when_stopped_observation_replaces_commanded_load(
    monkeypatch,
):
    """A stopped Tesla must not reserve its stale Solar Surplus amp target."""
    hass = _Hass([
        _State("sensor.VIN123_charging_state", "stopped"),
        _State("sensor.VIN123_charger_power", "0", {"unit_of_measurement": "W"}),
    ])
    vehicle_id = "VIN123"
    actions._dynamic_ev_state.clear()
    state = _solar_surplus_state(current_amps=24)
    state["params"]["vehicle_vin"] = vehicle_id
    state["params"]["charger_power_entity"] = "sensor.VIN123_charger_power"
    state["params"]["household_buffer_kw"] = 0.3
    state["high_surplus_start"] = datetime.now() - timedelta(minutes=4)
    actions._dynamic_ev_state["entry-1"] = {vehicle_id: state}

    start_calls: list[dict] = []

    async def fake_start_ev(hass, config_entry, params, context=None):
        start_calls.append(params)
        return True

    monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start_ev)
    monkeypatch.setattr(actions, "_is_vehicle_charge_complete", lambda *args: False)
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 100,
            "grid_power": -2100,
            "battery_power": 0,
            "solar_power": 0,
            "load_power": 0,
        },
    )

    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )

    assert start_calls and start_calls[0]["vehicle_vin"] == vehicle_id
    assert set_amps_calls == [8]
    assert actions._dynamic_ev_state["entry-1"][vehicle_id]["current_amps"] == 8
    assert actions._dynamic_ev_state["entry-1"][vehicle_id]["target_amps"] == 8
    assert "Target: 8A" in actions._dynamic_ev_state["entry-1"][vehicle_id]["reason"]
    assert actions._dynamic_ev_state["entry-1"][vehicle_id]["high_surplus_start"] is None

    # Stale stopped telemetry after the restart must not replay the start
    # command forever once the short post-command grace period expires.
    state["last_start_command_at"] = datetime.now() - timedelta(seconds=91)
    asyncio.run(
        actions._dynamic_ev_update_surplus(hass, _Entry(), "entry-1", vehicle_id)
    )
    assert len(start_calls) == 1
    assert set_amps_calls == [8]
    assert state["high_surplus_start"] is not None


def test_tesla_stopped_state_is_not_charge_complete_at_partial_soc():
    hass = _Hass([
        _State("sensor.VIN123_charging_state", "stopped"),
        _State("sensor.VIN123_battery_level", "69"),
    ])

    assert actions._is_vehicle_charge_complete(hass, "VIN123") is False


def test_stopped_tesla_requires_available_numeric_zero_power_for_stale_load_override():
    missing_hass = _Hass([])
    unavailable_hass = _Hass([
        _State("sensor.VIN123_charger_power", "unavailable"),
    ])
    nonnumeric_hass = _Hass([
        _State("sensor.VIN123_charger_power", "not-a-number"),
    ])
    params = {
        "charger_type": "tesla",
        "charger_power_entity": "sensor.VIN123_charger_power",
    }

    assert asyncio.run(
        actions._get_observed_ev_power_reading_kw(
            missing_hass,
            "VIN123",
            params,
        )
    ) == (0.0, False)
    assert asyncio.run(
        actions._get_observed_ev_power_reading_kw(
            unavailable_hass,
            "VIN123",
            params,
        )
    ) == (0.0, False)
    assert asyncio.run(
        actions._get_observed_ev_power_reading_kw(
            nonnumeric_hass,
            "VIN123",
            params,
        )
    ) == (0.0, False)


def test_solar_surplus_stopped_tesla_keeps_commanded_fallback_without_power_source(
    monkeypatch,
):
    for power_state in (None, "unavailable"):
        states = [_State("sensor.VIN123_charging_state", "stopped")]
        if power_state is not None:
            states.append(_State("sensor.VIN123_charger_power", power_state))
        hass = _Hass(states)
        vehicle_id = "VIN123"
        actions._dynamic_ev_state.clear()
        state = _solar_surplus_state(current_amps=24)
        state["params"].update(
            {
                "vehicle_vin": vehicle_id,
                "charger_power_entity": "sensor.VIN123_charger_power",
                "household_buffer_kw": 0.3,
            }
        )
        state["high_surplus_start"] = datetime.now() - timedelta(minutes=4)
        actions._dynamic_ev_state["entry-1"] = {vehicle_id: state}
        start_calls: list[dict] = []

        async def fake_start_ev(hass, config_entry, params, context=None):
            start_calls.append(params)
            return True

        monkeypatch.setattr(actions, "_action_start_ev_charging", fake_start_ev)
        set_amps_calls = _install_solar_surplus_runtime_stubs(
            monkeypatch,
            {
                "battery_soc": 100,
                "grid_power": -2100,
                "battery_power": 0,
                "solar_power": 0,
                "load_power": 0,
            },
        )

        asyncio.run(
            actions._dynamic_ev_update_surplus(
                hass,
                _Entry(),
                "entry-1",
                vehicle_id,
            )
        )

        assert start_calls == []
        assert set_amps_calls == [32]
        assert state["current_amps"] == 32


def test_tesla_restart_telemetry_pending_accepts_timezone_aware_timestamp():
    state = {"last_start_command_at": datetime.now(timezone.utc)}

    assert actions._tesla_restart_telemetry_pending(state) is True


def test_clear_tracked_session_does_not_send_physical_stop():
    hass = _Hass([_State("switch.ev_charge", "on")])
    cancelled = []
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "VIN123": {
            "active": True,
            "params": {"dynamic_mode": "manual", "charger_type": "tesla"},
            "cancel_timer": lambda: cancelled.append(True),
            "session_id": None,
        }
    }

    asyncio.run(actions.clear_tracked_ev_charging_session(hass, _Entry(), "VIN123"))

    assert actions._dynamic_ev_state == {}
    assert cancelled == [True]
    assert hass.data["power_sync"]["entry-1"]["ev_ownership"] == {}
    assert hass.data["power_sync"]["entry-1"]["ev_last_command"]["VIN123"]["command"] == "release"
    assert hass.services.calls == []


def test_zaptec_waiting_without_installation_current_fails_start():
    hass, client = _zaptec_hass({"charger_operation_mode": "connected_waiting"})

    result = asyncio.run(
        actions._action_start_ev_charging(
            hass,
            _zaptec_entry(),
            {"charger_type": "zaptec"},
        )
    )

    assert result is False
    assert client.calls == []


def test_zaptec_waiting_sets_current_without_resume():
    hass, client = _zaptec_hass({"charger_operation_mode": "connected_waiting"})

    result = asyncio.run(
        actions._action_start_ev_charging(
            hass,
            _zaptec_entry("installation-1"),
            {"charger_type": "zaptec"},
        )
    )

    assert result is True
    assert client.calls == [("set_installation_current", "installation-1", 16)]


def test_zaptec_already_charging_updates_current_without_resume():
    hass, client = _zaptec_hass({"charger_operation_mode": "charging"})

    result = asyncio.run(
        actions._action_start_ev_charging(
            hass,
            _zaptec_entry("installation-1"),
            {"charger_type": "zaptec", "amps": 12},
        )
    )

    assert result is True
    assert client.calls == [("set_installation_current", "installation-1", 12)]


def test_zaptec_idle_stop_is_passive_success():
    hass, client = _zaptec_hass({"charger_operation_mode": "connected_waiting"})

    result = asyncio.run(
        actions._action_stop_ev_charging(
            hass,
            _zaptec_entry("installation-1"),
            {"charger_type": "zaptec"},
        )
    )

    assert result is True
    assert client.calls == []


def test_sigenergy_charger_set_vehicle_amps_sets_limit_then_starts(monkeypatch):
    calls: list[tuple] = []

    class _SigenergyController:
        def __init__(self, **config):
            calls.append(("init", config))

        async def set_charging_amps(self, amps: int):
            calls.append(("set_charging_amps", amps))
            return True

        async def start_charging(self, amps=None):
            calls.append(("start_charging", amps))
            return True

        async def disconnect(self):
            calls.append(("disconnect",))

    monkeypatch.setattr(
        actions,
        "_new_sigenergy_charger",
        lambda config: _SigenergyController(**config),
    )
    hass = _Hass([])
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "sigenergy_charger_host": "192.0.2.10",
            "sigenergy_charger_port": 502,
            "sigenergy_charger_slave_id": 1,
            "sigenergy_charger_type": "evac",
        },
    )

    result = asyncio.run(
        actions._set_vehicle_amps(
            hass,
            entry,
            "sigenergy_charger",
            16,
            {"charger_type": "sigenergy"},
        )
    )

    assert result is True
    assert calls == [
        (
            "init",
            {
                "host": "192.0.2.10",
                "port": 502,
                "slave_id": 1,
                "charger_type": "evac",
            },
        ),
        ("set_charging_amps", 16),
        ("disconnect",),
        (
            "init",
            {
                "host": "192.0.2.10",
                "port": 502,
                "slave_id": 1,
                "charger_type": "evac",
            },
        ),
        ("start_charging", None),
        ("disconnect",),
    ]


def test_sigenergy_charger_stop_routes_to_modbus_controller(monkeypatch):
    calls: list[tuple] = []

    class _SigenergyController:
        def __init__(self, **config):
            calls.append(("init", config))

        async def stop_charging(self):
            calls.append(("stop_charging",))
            return True

        async def disconnect(self):
            calls.append(("disconnect",))

    monkeypatch.setattr(
        actions,
        "_new_sigenergy_charger",
        lambda config: _SigenergyController(**config),
    )
    hass = _Hass([])
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={"sigenergy_modbus_host": "192.0.2.20"},
        options={
            "sigenergy_charger_slave_id": 2,
            "sigenergy_charger_type": "evdc",
        },
    )

    result = asyncio.run(
        actions._action_stop_ev_charging(
            hass,
            entry,
            {"charger_type": "sigenergy"},
        )
    )

    assert result is True
    assert calls == [
        (
            "init",
            {
                "host": "192.0.2.20",
                "port": 502,
                "slave_id": 2,
                "charger_type": "evdc",
            },
        ),
        ("stop_charging",),
        ("disconnect",),
    ]


def test_automation_stop_context_routes_to_charging_ble_tesla():
    """A BLE EV trigger must not report success from another stopped Tesla."""
    registry_entities = {
        "sensor.tesla_flinn_charging": SimpleNamespace(
            entity_id="sensor.tesla_flinn_charging",
            device_id="fleet-a",
        ),
        "switch.tesla_flinn_charge": SimpleNamespace(
            entity_id="switch.tesla_flinn_charge",
            device_id="fleet-a",
        ),
        "sensor.tesla_yf88_charging": SimpleNamespace(
            entity_id="sensor.tesla_yf88_charging",
            device_id="fleet-b",
        ),
        "switch.tesla_yf88_charge": SimpleNamespace(
            entity_id="switch.tesla_yf88_charge",
            device_id="fleet-b",
        ),
    }
    registry_devices = {
        "fleet-a": SimpleNamespace(
            id="fleet-a",
            name="Tesla Flinn",
            identifiers={("teslemetry", "XP7YHCEL7TB811704")},
        ),
        "fleet-b": SimpleNamespace(
            id="fleet-b",
            name="Tesla YF88",
            identifiers={("teslemetry", "LRWYHCEKXTC687964")},
        ),
    }
    hass = _Hass(
        [
            _State("sensor.tesla_flinn_charging", "stopped"),
            _State("sensor.tesla_yf88_charging_state", "Charging"),
            _State("binary_sensor.tesla_yf88_ble_status", "on"),
            _State("binary_sensor.tesla_yf88_asleep", "off"),
            _State("button.tesla_yf88_wake_up", "unknown"),
            _State("switch.tesla_yf88_charger", "on"),
        ],
        registry_entities=registry_entities,
        registry_devices=registry_devices,
    )
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "ev_provider": "both",
            "tesla_ble_entity_prefix": "tesla_yf88",
        },
    )

    result = asyncio.run(
        actions._execute_single_action(
            hass,
            entry,
            "stop_ev_charging",
            {},
            {"ev_vehicle_id": "ble_tesla_yf88"},
        )
    )

    assert result is True
    assert hass.services.calls == [
        ("switch", "turn_off", {"entity_id": "switch.tesla_yf88_charger"})
    ]


def test_sigenergy_evdc_dynamic_start_skips_unsupported_current_limit(monkeypatch):
    calls: list[tuple] = []

    class _SigenergyController:
        def __init__(self, **config):
            calls.append(("init", config))

        async def set_charging_amps(self, amps: int):
            calls.append(("set_charging_amps", amps))
            return False

        async def start_charging(self, amps=None):
            calls.append(("start_charging", amps))
            return True

        async def disconnect(self):
            calls.append(("disconnect",))

    monkeypatch.setattr(
        actions,
        "_new_sigenergy_charger",
        lambda config: _SigenergyController(**config),
    )
    hass = _Hass([])
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "sigenergy_charger_host": "192.0.2.30",
            "sigenergy_charger_slave_id": 2,
            "sigenergy_charger_type": "evdc",
        },
    )

    result = asyncio.run(
        actions._set_vehicle_amps(
            hass,
            entry,
            "sigenergy_charger",
            24,
            {"charger_type": "sigenergy", "_sigenergy_start_after_rate_limit": True},
        )
    )

    assert result is True
    assert calls == [
        (
            "init",
            {
                "host": "192.0.2.30",
                "port": 502,
                "slave_id": 2,
                "charger_type": "evdc",
            },
        ),
        ("start_charging", None),
        ("disconnect",),
    ]


def test_sigenergy_evdc_one_shot_blocks_restart_until_unplug(monkeypatch):
    calls: list[tuple] = []

    class _SigenergyState:
        def __init__(self, connected: bool) -> None:
            self.is_connected = connected
            self.is_charging = False

    class _SigenergyController:
        connected = True

        def __init__(self, **config):
            calls.append(("init", config))

        async def read_state(self):
            calls.append(("read_state", self.connected))
            return _SigenergyState(self.connected)

        async def start_charging(self, amps=None):
            calls.append(("start_charging", amps))
            return True

        async def stop_charging(self):
            calls.append(("stop_charging",))
            return True

        async def disconnect(self):
            calls.append(("disconnect",))

    monkeypatch.setattr(
        actions,
        "_new_sigenergy_charger",
        lambda config: _SigenergyController(**config),
    )
    hass = _Hass([])
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "sigenergy_charger_host": "192.0.2.31",
            "sigenergy_charger_slave_id": 2,
            "sigenergy_charger_type": "evdc",
        },
    )
    params = {"charger_type": "sigenergy", "_sigenergy_start_after_rate_limit": True}

    assert asyncio.run(actions._set_vehicle_amps(hass, entry, "sigenergy_charger", 24, params))
    assert asyncio.run(actions._set_vehicle_amps(hass, entry, "sigenergy_charger", 0, params))
    assert not asyncio.run(actions._set_vehicle_amps(hass, entry, "sigenergy_charger", 24, params))

    _SigenergyController.connected = False
    assert asyncio.run(actions._set_vehicle_amps(hass, entry, "sigenergy_charger", 24, params))

    assert [call[0] for call in calls].count("start_charging") == 2
    assert ("read_state", True) in calls
    assert ("read_state", False) in calls


def test_sigenergy_evdc_rate_entity_sets_kw_then_starts(monkeypatch):
    calls: list[tuple] = []

    class _SigenergyController:
        def __init__(self, **config):
            calls.append(("init", config))

        async def start_charging(self, amps=None):
            calls.append(("start_charging", amps))
            return True

        async def disconnect(self):
            calls.append(("disconnect",))

    monkeypatch.setattr(
        actions,
        "_new_sigenergy_charger",
        lambda config: _SigenergyController(**config),
    )
    hass = _Hass([
        _State(
            "number.sigen_inverter_dc_charger_max_charging_power_limit",
            "25",
            {"min": 0, "max": 25.0},
        )
    ])
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "sigenergy_charger_host": "192.0.2.40",
            "sigenergy_charger_slave_id": 2,
            "sigenergy_charger_type": "evdc",
        },
    )

    result = asyncio.run(
        actions._set_vehicle_amps(
            hass,
            entry,
            "sigenergy_charger",
            24,
            {
                "charger_type": "sigenergy",
                "voltage": 230,
                "phases": 1,
                "_sigenergy_start_after_rate_limit": True,
            },
        )
    )

    assert result is True
    assert hass.services.calls == [
        (
            "number",
            "set_value",
            {
                "entity_id": "number.sigen_inverter_dc_charger_max_charging_power_limit",
                "value": 5.52,
            },
        )
    ]
    assert calls == [
        (
            "init",
            {
                "host": "192.0.2.40",
                "port": 502,
                "slave_id": 2,
                "charger_type": "evdc",
            },
        ),
        ("start_charging", None),
        ("disconnect",),
    ]


def test_sigenergy_evdc_rate_entity_clamps_to_entity_max(monkeypatch):
    hass = _Hass([
        _State(
            "number.evdc_charge_limit",
            "7",
            {"min": 0, "max": 7.0},
        )
    ])
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "sigenergy_charger_host": "192.0.2.41",
            "sigenergy_charger_slave_id": 2,
            "sigenergy_charger_type": "evdc",
            "sigenergy_charger_charge_power_limit_entity": "number.evdc_charge_limit",
        },
    )

    result = asyncio.run(
        actions._action_set_ev_charging_amps(
            hass,
            entry,
            {
                "charger_type": "sigenergy",
                "amps": 40,
                "voltage": 230,
                "phases": 1,
            },
        )
    )

    assert result is True
    assert hass.services.calls == [
        (
            "number",
            "set_value",
            {"entity_id": "number.evdc_charge_limit", "value": 7.0},
        )
    ]


def test_sigenergy_evdc_rate_entity_uses_default_25kw_cap_without_entity_max(monkeypatch):
    hass = _Hass([
        _State(
            "number.sigen_inverter_dc_charger_max_charging_power_limit",
            "25",
            {},
        )
    ])
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "sigenergy_charger_host": "192.0.2.42",
            "sigenergy_charger_slave_id": 2,
            "sigenergy_charger_type": "evdc",
        },
    )

    result = asyncio.run(
        actions._action_set_ev_charging_amps(
            hass,
            entry,
            {
                "charger_type": "sigenergy",
                "amps": 200,
                "voltage": 240,
                "phases": 1,
            },
        )
    )

    assert result is True
    assert hass.services.calls == [
        (
            "number",
            "set_value",
            {
                "entity_id": "number.sigen_inverter_dc_charger_max_charging_power_limit",
                "value": 25.0,
            },
        )
    ]


def test_sigenergy_evdc_solar_surplus_uses_native_handoff_without_amp_updates(monkeypatch):
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 100,
            "battery_power": 0,
            "grid_power": -2000,
            "solar_power": 6000,
            "load_power": 2000,
        },
    )
    mode_calls: list[str] = []

    class _SigenergyController:
        async def set_self_consumption_mode(self):
            mode_calls.append("self_consumption")
            return True

        async def disconnect(self):
            mode_calls.append("disconnect")

    async def fake_controller(config_entry):
        return _SigenergyController()

    monkeypatch.setattr(actions, "_get_sigenergy_controller", fake_controller)
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "sigenergy_charger": {
            "active": True,
            "current_amps": 0,
            "target_amps": 0,
            "charging_started": False,
            "entity_max_rechecked": True,
            "params": {
                "dynamic_mode": "solar_surplus",
                "charger_type": "sigenergy",
                "sigenergy_charger_type": "evdc",
                "supports_rate_control": False,
                "solar_control_strategy": "native_handoff",
                "min_charge_amps": 6,
                "max_charge_amps": 32,
                "voltage": 240,
                "phases": 1,
                "household_buffer_kw": 0.5,
                "min_battery_soc": 80,
            },
        }
    }

    hass = _Hass([])
    asyncio.run(actions._dynamic_ev_update(hass, _Entry(), "entry-1", "sigenergy_charger"))

    state = actions._dynamic_ev_state["entry-1"]["sigenergy_charger"]
    assert set_amps_calls == []
    assert mode_calls == ["self_consumption", "disconnect"]
    assert state["native_solar_mode_set"] is True
    assert state["target_amps"] == 0
    assert "native solar handoff" in state["reason"]


def test_sigenergy_evdc_solar_surplus_uses_dynamic_rate_when_entity_detected(monkeypatch):
    set_amps_calls = _install_solar_surplus_runtime_stubs(
        monkeypatch,
        {
            "battery_soc": 100,
            "battery_power": 0,
            "grid_power": -2000,
            "solar_power": 6000,
            "load_power": 2000,
        },
    )

    async def unexpected_controller(config_entry):
        raise AssertionError("EVDC with rate entity should not use native solar handoff")

    monkeypatch.setattr(actions, "_get_sigenergy_controller", unexpected_controller)
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        "sigenergy_charger": {
            "active": True,
            "current_amps": 6,
            "target_amps": 6,
            "charging_started": True,
            "entity_max_rechecked": True,
            "params": {
                "dynamic_mode": "solar_surplus",
                "charger_type": "sigenergy",
                "sigenergy_charger_type": "evdc",
                "supports_rate_control": False,
                "solar_control_strategy": "native_handoff",
                "min_charge_amps": 6,
                "max_charge_amps": 32,
                "voltage": 240,
                "phases": 1,
                "household_buffer_kw": 0.5,
                "surplus_calculation": "grid_based",
                "min_battery_soc": 80,
                "pause_below_soc": 70,
            },
        }
    }

    hass = _Hass([
        _State("number.sigen_inverter_dc_charger_max_charging_power_limit", "25")
    ])
    asyncio.run(actions._dynamic_ev_update(hass, _Entry(), "entry-1", "sigenergy_charger"))

    state = actions._dynamic_ev_state["entry-1"]["sigenergy_charger"]
    assert set_amps_calls == [12]
    assert state["target_amps"] == 12
    assert state["params"]["supports_rate_control"] is True
    assert state["params"]["solar_control_strategy"] == "dynamic_rate"


def _install_away_location_module(monkeypatch, location: str = "work") -> None:
    planner = types.ModuleType("power_sync.automations.ev_charging_planner")

    async def away_location(*args, **kwargs):
        return location

    planner.get_ev_location = away_location
    monkeypatch.setitem(
        sys.modules,
        "power_sync.automations.ev_charging_planner",
        planner,
    )


@pytest.mark.parametrize(
    "owner_mode,dynamic_mode",
    [
        ("solar_surplus", "solar_surplus"),
        ("scheduled", "battery_target"),
        ("smart_schedule", "battery_target"),
        ("price_level_recovery", "battery_target"),
    ],
)
def test_away_tesla_dynamic_stop_is_passive_for_every_smart_mode(
    monkeypatch,
    owner_mode,
    dynamic_mode,
):
    from power_sync.automations import ev_ownership

    vehicle_id = "LRW3F7FS1NC484342"
    hass = _Hass([])
    _install_away_location_module(monkeypatch)
    physical_stops = []
    cancelled = []

    async def physical_stop(*args, **kwargs):
        physical_stops.append((args, kwargs))
        return True

    monkeypatch.setattr(actions, "_action_stop_ev_charging", physical_stop)
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        vehicle_id: {
            "active": True,
            "current_amps": 16,
            "target_amps": 16,
            "cancel_timer": lambda: cancelled.append(True),
            "params": {
                "owner_mode": owner_mode,
                "dynamic_mode": dynamic_mode,
                "charger_type": "tesla",
                "vehicle_vin": vehicle_id,
                "notify_on_complete": False,
            },
        }
    }
    ev_ownership.claim_ev_ownership(
        hass,
        _Entry(),
        vehicle_id,
        owner_mode=owner_mode,
    )

    result = asyncio.run(
        actions._action_stop_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_id": vehicle_id,
                "stop_charging": True,
                "stop_reason": "mode no longer active",
            },
        )
    )

    assert result is True
    assert physical_stops == []
    assert cancelled == [True]
    assert actions._dynamic_ev_state == {}
    assert ev_ownership.get_ev_ownerships(hass, _Entry()) == {}
    last_command = ev_ownership.get_ev_last_commands(hass, _Entry())[vehicle_id]
    assert last_command["command"] == "release"


def test_away_untracked_smart_stop_does_not_touch_external_tesla_session(
    monkeypatch,
):
    from power_sync.automations import ev_ownership

    vehicle_id = "LRW3F7FS1NC484342"
    hass = _Hass([])
    _install_away_location_module(monkeypatch, "remote_charger")
    physical_stops = []

    async def physical_stop(*args, **kwargs):
        physical_stops.append((args, kwargs))
        return True

    monkeypatch.setattr(actions, "_action_stop_ev_charging", physical_stop)
    actions._dynamic_ev_state.clear()
    ev_ownership.claim_ev_ownership(
        hass,
        _Entry(),
        vehicle_id,
        owner_mode="scheduled",
    )

    result = asyncio.run(
        actions._action_stop_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_id": vehicle_id,
                "vehicle_vin": vehicle_id,
                "charger_type": "tesla",
                "owner_mode": "scheduled",
                "stop_charging": True,
                "stop_untracked": True,
                "stop_reason": "outside scheduled window",
            },
        )
    )

    assert result is True
    assert physical_stops == []
    assert ev_ownership.get_ev_ownerships(hass, _Entry()) == {}
    last_command = ev_ownership.get_ev_last_commands(hass, _Entry())[vehicle_id]
    assert last_command["command"] == "release"


def test_away_solar_surplus_timer_releases_without_rate_or_stop_command(monkeypatch):
    vehicle_id = "LRW3F7FS1NC484342"
    hass = _Hass([])
    _install_away_location_module(monkeypatch, "remote_charger")
    charger_commands = []

    async def charger_command(*args, **kwargs):
        charger_commands.append((args, kwargs))
        return True

    monkeypatch.setattr(actions, "_set_vehicle_amps", charger_command)
    monkeypatch.setattr(actions, "_action_stop_ev_charging", charger_command)
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        vehicle_id: {
            "active": True,
            "current_amps": 12,
            "target_amps": 12,
            "params": {
                "owner_mode": "solar_surplus",
                "dynamic_mode": "solar_surplus",
                "charger_type": "tesla",
                "vehicle_vin": vehicle_id,
                "notify_on_complete": False,
            },
        }
    }

    asyncio.run(
        actions._dynamic_ev_update_surplus(
            hass,
            _Entry(),
            "entry-1",
            vehicle_id,
        )
    )

    assert charger_commands == []
    assert actions._dynamic_ev_state == {}


def test_away_battery_target_timer_releases_before_current_write(monkeypatch):
    vehicle_id = "LRW3F7FS1NC484342"
    hass = _Hass([])
    _install_away_location_module(monkeypatch)
    current_writes = []

    async def set_amps(*args, **kwargs):
        current_writes.append((args, kwargs))
        return True

    monkeypatch.setattr(actions, "_set_vehicle_amps", set_amps)
    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        vehicle_id: {
            "active": True,
            "current_amps": 24,
            "target_amps": 24,
            "params": {
                "owner_mode": "smart_schedule",
                "dynamic_mode": "battery_target",
                "charger_type": "tesla",
                "vehicle_vin": vehicle_id,
                "notify_on_complete": False,
            },
        }
    }

    asyncio.run(
        actions._dynamic_ev_update(
            hass,
            _Entry(),
            "entry-1",
            vehicle_id,
        )
    )

    assert current_writes == []
    assert actions._dynamic_ev_state == {}


def test_smart_schedule_group_removes_away_vehicle_before_group_write(monkeypatch):
    home_vin = "5YJTEST00000000A1"
    away_vin = "5YJTEST00000000B2"
    hass = _Hass([])
    planner = types.ModuleType("power_sync.automations.ev_charging_planner")

    async def vehicle_location(*args, vehicle_vin=None, **kwargs):
        return "work" if vehicle_vin == away_vin else "home"

    planner.get_ev_location = vehicle_location
    monkeypatch.setitem(
        sys.modules,
        "power_sync.automations.ev_charging_planner",
        planner,
    )

    async def unexpected_live_status(*args, **kwargs):
        raise AssertionError("group telemetry must not run after an away session is found")

    monkeypatch.setattr(actions, "_get_tesla_live_status", unexpected_live_status)
    actions._dynamic_ev_state.clear()
    sessions = []
    for vehicle_id in (home_vin, away_vin):
        state = {
            "active": True,
            "current_amps": 16,
            "target_amps": 16,
            "params": {
                "owner_mode": "smart_schedule",
                "dynamic_mode": "battery_target",
                "charger_type": "tesla",
                "vehicle_vin": vehicle_id,
                "notify_on_complete": False,
            },
        }
        sessions.append((vehicle_id, state))
    actions._dynamic_ev_state["entry-1"] = dict(sessions)

    asyncio.run(
        actions._update_smart_schedule_battery_target_group(
            hass,
            _Entry(),
            "entry-1",
            sessions,
        )
    )

    assert set(actions._dynamic_ev_state["entry-1"]) == {home_vin}


def test_away_tesla_dynamic_start_is_blocked_before_physical_command(monkeypatch):
    vehicle_id = "LRW3F7FS1NC484342"
    hass = _Hass([])
    _install_away_location_module(monkeypatch)
    physical_starts = []

    async def physical_start(*args, **kwargs):
        physical_starts.append((args, kwargs))
        return True

    monkeypatch.setattr(actions, "_action_start_ev_charging", physical_start)
    monkeypatch.setattr(actions, "_set_vehicle_amps", physical_start)
    actions._dynamic_ev_state.clear()

    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            {
                "vehicle_vin": vehicle_id,
                "owner_mode": "scheduled",
                "dynamic_mode": "battery_target",
                "charger_type": "tesla",
            },
            context=None,
        )
    )

    assert result is False
    assert physical_starts == []
    assert actions._dynamic_ev_state == {}


def test_automated_tesla_command_boundaries_block_explicit_away_location(monkeypatch):
    vehicle_id = "LRW3F7FS1NC484342"
    hass = _Hass([])
    _install_away_location_module(monkeypatch, "remote_charger")
    params = {
        "vehicle_vin": vehicle_id,
        "vehicle_id": vehicle_id,
        "owner_mode": "smart_schedule",
        "dynamic_mode": "battery_target",
        "charger_type": "tesla",
    }

    start_result = asyncio.run(
        actions._action_start_ev_charging(
            hass,
            _Entry(),
            params,
            context=None,
        )
    )
    rate_result = asyncio.run(
        actions._set_vehicle_amps(
            hass,
            _Entry(),
            vehicle_id,
            16,
            params,
        )
    )
    stop_result = asyncio.run(
        actions._action_stop_ev_charging(
            hass,
            _Entry(),
            params,
        )
    )

    assert start_result is False
    assert rate_result is False
    assert stop_result is True
    assert hass.services.calls == []


def _handover_start_params(vin: str) -> dict:
    """Price Level taking over a loadpoint Solar Surplus currently owns."""
    return {
        "vehicle_id": vin,
        "vehicle_vin": vin,
        "dynamic_mode": "battery_target",
        "owner_mode": "price_level_opportunity",
        "charger_type": "tesla",
        "min_charge_amps": 1,
        "max_charge_amps": 32,
        "fixed_charge_amps": 32,
        "require_physical_start_confirmation": True,
        "allow_ownership_takeover": True,
    }


def _patch_handover_start(monkeypatch, *, start_calls, stop_calls, wait_result):
    """Stub the Tesla start path with pre-stop telemetry that has not settled."""

    async def active_capability(*args, **kwargs):
        return {
            "association_known": True,
            "capability_known": True,
            "max_charge_amps": 32,
            "max_charge_amps_source": "active_charger",
            "voltage": 240,
            "phases": 1,
        }

    async def none_result(*args, **kwargs):
        return None

    async def true_result(*args, **kwargs):
        return True

    async def record_start(*args, **kwargs):
        start_calls.append((args, kwargs))
        return True

    async def record_stop(hass, config_entry, params, *args, **kwargs):
        stop_calls.append(params)
        return True

    async def wait_for_start(*args, **kwargs):
        return wait_result

    monkeypatch.setattr(
        actions,
        "_resolve_tesla_active_charger_capability",
        active_capability,
    )
    monkeypatch.setattr(actions, "_resolve_tesla_charge_current_entity", none_result)
    monkeypatch.setattr(actions, "_tesla_vehicle_away_location", none_result)
    monkeypatch.setattr(actions, "_action_start_ev_charging", record_start)
    monkeypatch.setattr(actions, "_action_stop_ev_charging", record_stop)
    monkeypatch.setattr(actions, "_set_vehicle_amps", true_result)
    monkeypatch.setattr(actions, "_send_expo_push", none_result)
    # Cloud EV telemetry lags the stop command by a poll interval, so the
    # baseline read straight after the teardown still says "charging".
    monkeypatch.setattr(
        actions,
        "_tesla_physical_charging_snapshot",
        lambda *args, **kwargs: {
            "charging": True,
            "measurements": frozenset({"sensor.n3bula_charger_current=26.0A"}),
            "fresh_measurements": frozenset(),
        },
    )
    monkeypatch.setattr(actions, "_wait_for_tesla_physical_start", wait_for_start)


def test_handover_after_self_stop_still_issues_a_physical_start(monkeypatch):
    """PowerSync's own stop must not read back as 'already charging'.

    Solar Surplus → Price Level handover tears the previous session down with a
    real charger stop.  Reading the pre-stop telemetry immediately afterwards
    used to satisfy the already-charging recovery branch, which skipped the
    start command entirely and then confirmed it from that same stale sample.
    """
    vin = "LRW3F7FS1NC484342"
    start_calls: list[tuple] = []
    stop_calls: list[dict] = []
    timer_calls: list[tuple] = []

    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {vin: _solar_surplus_state(current_amps=26)}

    _patch_handover_start(
        monkeypatch,
        start_calls=start_calls,
        stop_calls=stop_calls,
        wait_result=(True, "sensor.n3bula_charger_current=26.0A"),
    )
    monkeypatch.setattr(
        actions,
        "async_track_time_interval",
        lambda *args, **kwargs: timer_calls.append((args, kwargs)),
    )

    hass = _Hass([])
    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            _handover_start_params(vin),
        )
    )

    assert result is True
    # Exactly one physical stop (the teardown) and one physical start.
    assert len(stop_calls) == 1
    assert len(start_calls) == 1
    assert len(timer_calls) == 1
    assert actions._dynamic_ev_state["entry-1"][vin]["active"] is True


def test_handover_start_that_is_never_confirmed_creates_no_session_or_lease(
    monkeypatch,
):
    """An unconfirmed handover start must fail closed, not block Solar Surplus."""
    vin = "LRW3F7FS1NC484342"
    start_calls: list[tuple] = []
    stop_calls: list[dict] = []
    timer_calls: list[tuple] = []

    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {vin: _solar_surplus_state(current_amps=26)}

    _patch_handover_start(
        monkeypatch,
        start_calls=start_calls,
        stop_calls=stop_calls,
        wait_result=(False, "no fresh VIN-scoped charging state and measured draw"),
    )
    monkeypatch.setattr(
        actions,
        "async_track_time_interval",
        lambda *args, **kwargs: timer_calls.append((args, kwargs)),
    )

    hass = _Hass([])
    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            _handover_start_params(vin),
        )
    )

    assert result is False
    assert len(start_calls) == 1
    assert timer_calls == []
    assert vin not in actions._dynamic_ev_state.get("entry-1", {})
    ownership = hass.data["power_sync"]["entry-1"].get("ev_ownership", {})
    assert ownership.get(vin, {}).get("owner_mode") != "price_level_opportunity"
    # Teardown stop plus the compensating stop for the unconfirmed start.
    assert len(stop_calls) == 2
    assert stop_calls[-1].get("_force_tesla_stop_request") is True


def test_self_stop_disqualification_is_scoped_to_the_stopped_vehicle(monkeypatch):
    """A stop for one VIN must not suppress another VIN's genuine recovery."""
    stopped_vin = "LRW3F7FS1NC484342"
    other_vin = "5YJTEST00000000B2"
    start_calls: list[tuple] = []
    stop_calls: list[dict] = []
    timer_calls: list[tuple] = []

    actions._dynamic_ev_state.clear()
    actions._dynamic_ev_state["entry-1"] = {
        stopped_vin: _solar_surplus_state(current_amps=26),
    }

    _patch_handover_start(
        monkeypatch,
        start_calls=start_calls,
        stop_calls=stop_calls,
        wait_result=(True, "sensor.car_b_charger_current=15.0A"),
    )
    monkeypatch.setattr(
        actions,
        "async_track_time_interval",
        lambda *args, **kwargs: timer_calls.append((args, kwargs)),
    )

    hass = _Hass([])
    result = asyncio.run(
        actions._action_start_ev_charging_dynamic(
            hass,
            _Entry(),
            _handover_start_params(other_vin),
        )
    )

    assert result is True
    # The other vehicle was never stopped, so its already-charging telemetry is
    # still trustworthy and the recovery branch is preserved.
    assert stop_calls == []
    assert start_calls == []
    assert len(timer_calls) == 1
    assert actions._dynamic_ev_state["entry-1"][stopped_vin]["active"] is True
    assert actions._dynamic_ev_state["entry-1"][other_vin]["active"] is True
