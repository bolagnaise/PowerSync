"""Regression tests for automation state coordinator discovery."""

from __future__ import annotations

import ast
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Dict, Optional


ROOT = Path(__file__).resolve().parent.parent
AUTOMATIONS_PATH = ROOT / "custom_components" / "power_sync" / "automations" / "__init__.py"
TRIGGERS_PATH = ROOT / "custom_components" / "power_sync" / "automations" / "triggers.py"


def _load_engine_method(name: str, namespace: dict[str, Any]):
    tree = ast.parse(AUTOMATIONS_PATH.read_text())
    engine = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "AutomationEngine"
    )
    method = next(
        node
        for node in engine.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )
    extracted = ast.ClassDef(
        name="_ExtractedEngine",
        bases=[],
        keywords=[],
        body=[method],
        decorator_list=[],
    )
    module = ast.fix_missing_locations(ast.Module(body=[extracted], type_ignores=[]))
    exec(compile(module, str(AUTOMATIONS_PATH), "exec"), namespace)
    return namespace["_ExtractedEngine"]


def _load_trigger_function(name: str, namespace: dict[str, Any]):
    tree = ast.parse(TRIGGERS_PATH.read_text())
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    exec(compile(module, str(TRIGGERS_PATH), "exec"), namespace)
    return namespace[name]


def test_automation_current_state_includes_supported_battery_coordinators():
    tree = ast.parse(AUTOMATIONS_PATH.read_text())
    method = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_async_get_current_state"
    )
    source = ast.get_source_segment(AUTOMATIONS_PATH.read_text(), method)

    for coordinator_key in (
        "tesla_coordinator",
        "sigenergy_coordinator",
        "sungrow_coordinator",
        "foxess_coordinator",
        "goodwe_coordinator",
        "alphaess_coordinator",
        "esy_sunhome_coordinator",
        "solax_coordinator",
        "esy_coordinator",
        "saj_h2_coordinator",
        "fronius_reserva_coordinator",
        "neovolt_coordinator",
        "solaredge_coordinator",
        "anker_solix_coordinator",
        "custom_energy_coordinator",
    ):
        assert coordinator_key in source


def test_automation_time_defaults_to_home_assistant_timezone_before_sydney():
    """Custom-tariff sites without NEM metadata must not fire on Sydney time."""
    tree = ast.parse(AUTOMATIONS_PATH.read_text())
    method = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_async_get_current_state"
    )
    source = ast.get_source_segment(AUTOMATIONS_PATH.read_text(), method)

    assert 'self._config_entry.options.get("timezone")' in source
    assert 'self._config_entry.data.get("timezone")' in source
    assert 'getattr(getattr(self._hass, "config", None), "time_zone", None)' in source
    assert 'configured_timezone or ha_timezone or "Australia/Sydney"' in source


def test_automation_current_state_consumes_coordinator_power_values_as_kw(monkeypatch):
    """Battery coordinators expose solar/grid/load/battery power in kW."""
    power_sync = ModuleType("power_sync")
    power_sync.__path__ = []
    const = ModuleType("power_sync.const")
    const.DOMAIN = "power_sync"
    const.CONF_AEMO_REGION = "aemo_region"
    monkeypatch.setitem(sys.modules, "power_sync", power_sync)
    monkeypatch.setitem(sys.modules, "power_sync.const", const)

    engine_class = _load_engine_method(
        "_async_get_current_state",
        {
            "Any": Any,
            "Dict": Dict,
            "datetime": datetime,
            "timezone": timezone,
            "_LOGGER": logging.getLogger(__name__),
            "__package__": "power_sync.automations",
        },
    )
    engine = object.__new__(engine_class)
    engine._config_entry = SimpleNamespace(
        entry_id="entry",
        options={},
        data={},
    )
    engine._hass = SimpleNamespace(
        config=SimpleNamespace(time_zone="UTC"),
        data={
            "power_sync": {
                "entry": {
                    "tesla_coordinator": SimpleNamespace(
                        data={
                            "battery_level": 73,
                            "backup_reserve_percent": 20,
                            "solar_power": 10.0,
                            "grid_power": 10.0,
                            "battery_power": -2.5,
                            "load_power": 17.5,
                            "grid_status": "Active",
                            "energy_summary": {
                                "grid_import_today_kwh": 4.5,
                                "grid_export_today_kwh": 7.25,
                            },
                        }
                    ),
                    # Prevent the extracted method from falling through to
                    # tariff fetching, which is outside this regression's scope.
                    "amber_coordinator": SimpleNamespace(data={"current": []}),
                    "force_charge_state": {},
                    "force_discharge_state": {},
                }
            }
        },
    )

    state = asyncio.run(engine._async_get_current_state())

    assert state["solar_power_kw"] == 10.0
    assert state["home_usage_kw"] == 17.5
    assert state["grid_import_kw"] == 10.0
    assert state["grid_import_energy_power_kw"] == 10.0
    assert state["grid_export_kw"] == 0
    assert state["grid_export_energy_power_kw"] == 0
    assert state["grid_import_today_kwh"] == 4.5
    assert state["grid_export_today_kwh"] == 7.25
    assert state["battery_charge_kw"] == 2.5
    assert state["battery_discharge_kw"] == 0

    evaluate_flow_condition = _load_trigger_function(
        "_evaluate_flow_condition",
        {
            "Any": Any,
            "Dict": Dict,
            "TriggerResult": SimpleNamespace,
        },
    )
    flow_result = evaluate_flow_condition(
        {
            "condition_type": "flow",
            "flow_source": "grid_import",
            "flow_comparison": "below",
            "flow_threshold_kw": 5.0,
        },
        state,
    )
    assert flow_result.triggered is False

    engine._hass.data["power_sync"]["entry"]["tesla_coordinator"].data.update(
        {
            "grid_power": -10.0,
            "battery_power": 2.5,
        }
    )
    state = asyncio.run(engine._async_get_current_state())

    assert state["grid_import_kw"] == 0
    assert state["grid_import_energy_power_kw"] == 0
    assert state["grid_export_kw"] == 10.0
    assert state["grid_export_energy_power_kw"] == 10.0
    assert state["battery_charge_kw"] == 0
    assert state["battery_discharge_kw"] == 2.5

    engine._hass.data["power_sync"]["entry"]["tesla_coordinator"].data[
        "grid_power"
    ] = True
    state = asyncio.run(engine._async_get_current_state())

    assert state["grid_import_kw"] is None
    assert state["grid_import_energy_power_kw"] is None
    assert state["grid_export_kw"] is None
    assert state["grid_export_energy_power_kw"] is None


def test_automation_current_state_skips_empty_or_failed_coordinators(monkeypatch):
    """The first registered object must not hide a later healthy snapshot."""
    power_sync = ModuleType("power_sync")
    power_sync.__path__ = []
    const = ModuleType("power_sync.const")
    const.DOMAIN = "power_sync"
    const.CONF_AEMO_REGION = "aemo_region"
    monkeypatch.setitem(sys.modules, "power_sync", power_sync)
    monkeypatch.setitem(sys.modules, "power_sync.const", const)

    engine_class = _load_engine_method(
        "_async_get_current_state",
        {
            "Any": Any,
            "Dict": Dict,
            "datetime": datetime,
            "timezone": timezone,
            "_LOGGER": logging.getLogger(__name__),
            "__package__": "power_sync.automations",
        },
    )
    engine = object.__new__(engine_class)
    engine._config_entry = SimpleNamespace(entry_id="entry", options={}, data={})
    engine._hass = SimpleNamespace(
        config=SimpleNamespace(time_zone="UTC"),
        data={
            "power_sync": {
                "entry": {
                    "tesla_coordinator": SimpleNamespace(
                        data={"grid_power": -99.0},
                        last_update_success=False,
                    ),
                    "sigenergy_coordinator": SimpleNamespace(
                        data={
                            "battery_level": 60,
                            "grid_power": -3.0,
                            "solar_power": 4.0,
                            "battery_power": 0.0,
                            "load_power": 1.0,
                            "energy_summary": {"grid_export_today_kwh": 8.0},
                        },
                        last_update_success=True,
                    ),
                    "amber_coordinator": SimpleNamespace(data={"current": []}),
                    "force_charge_state": {},
                    "force_discharge_state": {},
                }
            }
        },
    )

    state = asyncio.run(engine._async_get_current_state())

    assert state["grid_export_kw"] == 3.0
    assert state["grid_export_energy_power_kw"] == 3.0
    assert state["grid_export_today_kwh"] == 8.0


def test_automation_current_state_normalizes_only_known_grid_statuses(monkeypatch):
    """Missing or unsupported outage telemetry must remain unknown."""
    power_sync = ModuleType("power_sync")
    power_sync.__path__ = []
    const = ModuleType("power_sync.const")
    const.DOMAIN = "power_sync"
    const.CONF_AEMO_REGION = "aemo_region"
    monkeypatch.setitem(sys.modules, "power_sync", power_sync)
    monkeypatch.setitem(sys.modules, "power_sync.const", const)

    engine_class = _load_engine_method(
        "_async_get_current_state",
        {
            "Any": Any,
            "Dict": Dict,
            "datetime": datetime,
            "timezone": timezone,
            "_LOGGER": logging.getLogger(__name__),
            "__package__": "power_sync.automations",
        },
    )
    coordinator_data = {
        "battery_level": 73,
        "solar_power": 1.0,
        "grid_power": 0.0,
        "battery_power": 0.0,
        "load_power": 1.0,
    }
    engine = object.__new__(engine_class)
    engine._config_entry = SimpleNamespace(entry_id="entry", options={}, data={})
    engine._hass = SimpleNamespace(
        config=SimpleNamespace(time_zone="UTC"),
        data={
            "power_sync": {
                "entry": {
                    "tesla_coordinator": SimpleNamespace(data=coordinator_data),
                    "amber_coordinator": SimpleNamespace(data={"current": []}),
                    "force_charge_state": {},
                    "force_discharge_state": {},
                }
            }
        },
    )

    cases = (
        ("Active", "on_grid"),
        ("SystemGridConnected", "on_grid"),
        ("Inactive", "off_grid"),
        ("Islanded", "off_grid"),
        ("Off-Grid", "off_grid"),
        ("SystemIslandedActive", "off_grid"),
        ("SystemIslandedReady", "unknown"),
        ("SystemTransitionToGrid", "unknown"),
        ("SystemTransitionToIsland", "unknown"),
        ("SystemMicroGridFaulted", "unknown"),
        ("SystemWaitForUser", "unknown"),
        ("On-Grid", "unknown"),
        ("", "unknown"),
        (None, "unknown"),
        ("unsupported-provider-state", "unknown"),
    )
    for raw_status, expected in cases:
        if raw_status is None:
            coordinator_data.pop("grid_status", None)
        else:
            coordinator_data["grid_status"] = raw_status

        state = asyncio.run(engine._async_get_current_state())

        assert state["grid_status"] == expected


def test_grid_trigger_ignores_unknown_without_synthesizing_recovery():
    """An unknown sample must not replace the last known grid transition state."""

    class _Store:
        def __init__(self):
            self.value = 1.0
            self.updates = []

        def update_trigger_state(self, automation_id, value):
            self.value = value
            self.updates.append((automation_id, value))

    evaluate_grid_trigger = _load_trigger_function(
        "_evaluate_grid_trigger",
        {
            "Any": Any,
            "Dict": Dict,
            "Optional": Optional,
            "TriggerResult": SimpleNamespace,
        },
    )
    store = _Store()
    trigger = {"grid_condition": "on_grid"}

    unknown_result = evaluate_grid_trigger(
        trigger,
        {"grid_status": "unknown"},
        store.value,
        store,
        17,
    )
    recovered_result = evaluate_grid_trigger(
        trigger,
        {"grid_status": "on_grid"},
        store.value,
        store,
        17,
    )

    assert unknown_result.triggered is False
    assert unknown_result.reason == "Grid status unavailable"
    assert store.updates == [(17, 1.0)]
    assert recovered_result.triggered is False


def test_grid_condition_does_not_default_missing_status_to_on_grid():
    evaluate_grid_condition = _load_trigger_function(
        "_evaluate_grid_condition",
        {
            "Any": Any,
            "Dict": Dict,
            "TriggerResult": SimpleNamespace,
        },
    )

    result = evaluate_grid_condition({"grid_condition": "on_grid"}, {})

    assert result.triggered is False
    assert result.reason == "Grid status unavailable"


def test_automation_stop_context_preserves_triggered_ble_vehicle():
    calls = []

    async def execute_actions(hass, config_entry, actions, context):
        calls.append((hass, config_entry, actions, context))
        return True

    engine_class = _load_engine_method(
        "_async_execute_automation",
        {
            "Any": Any,
            "Dict": Dict,
            "_LOGGER": logging.getLogger(__name__),
            "execute_actions": execute_actions,
        },
    )
    engine = object.__new__(engine_class)
    engine._hass = object()
    engine._config_entry = object()

    result = asyncio.run(
        engine._async_execute_automation(
            {
                "name": "Stop BLE Tesla",
                "trigger": {
                    "trigger_type": "ev",
                    "time_window_start": "16:00",
                    "time_window_end": "09:00",
                },
                "actions": [
                    {
                        "action_type": "stop_ev_charging",
                        "parameters": {},
                    }
                ],
            },
            set(),
            {
                "user_timezone": "Australia/Brisbane",
                "ev_state": {"vehicle_id": "ble_tesla_yf88"},
            },
        )
    )

    assert result is True
    assert calls[0][3]["ev_vehicle_id"] == "ble_tesla_yf88"


def test_automation_ev_state_recognizes_user_named_ble_prefix():
    class _States:
        def __init__(self):
            self._states = {
                "sensor.tesla_yf88_charging_state": SimpleNamespace(
                    entity_id="sensor.tesla_yf88_charging_state",
                    state="Charging",
                ),
                "binary_sensor.tesla_yf88_charge_flap": SimpleNamespace(
                    entity_id="binary_sensor.tesla_yf88_charge_flap",
                    state="on",
                ),
                "binary_sensor.tesla_yf88_ble_status": SimpleNamespace(
                    entity_id="binary_sensor.tesla_yf88_ble_status",
                    state="on",
                ),
                "sensor.tesla_yf88_charge_level": SimpleNamespace(
                    entity_id="sensor.tesla_yf88_charge_level",
                    state="50",
                ),
            }

        def async_all(self):
            return list(self._states.values())

        def get(self, entity_id):
            return self._states.get(entity_id)

    engine_class = _load_engine_method(
        "_async_get_ev_state",
        {
            "Any": Any,
            "Dict": Dict,
            "_LOGGER": logging.getLogger(__name__),
            "__package__": "power_sync.automations",
        },
    )
    engine = object.__new__(engine_class)
    engine._hass = SimpleNamespace(
        states=_States(),
        config_entries=SimpleNamespace(async_entries=lambda _domain: []),
        data={},
    )

    state = asyncio.run(engine._async_get_ev_state())

    assert state["is_charging"] is True
    assert state["is_plugged_in"] is True
    assert state["location"] == "home"
    assert state["vehicle_id"] == "ble_tesla_yf88"


def test_automation_ev_state_prefers_active_second_ble_vehicle():
    """A sleeping first bridge must not hide another BLE Tesla charging."""

    class _States:
        def __init__(self):
            self._states = {
                "sensor.tesla_yf88_charging_state": SimpleNamespace(
                    entity_id="sensor.tesla_yf88_charging_state",
                    state="Unknown",
                ),
                "binary_sensor.tesla_yf88_ble_status": SimpleNamespace(
                    entity_id="binary_sensor.tesla_yf88_ble_status",
                    state="off",
                ),
                "sensor.tesla_flinn_charging_state": SimpleNamespace(
                    entity_id="sensor.tesla_flinn_charging_state",
                    state="Charging",
                ),
                "binary_sensor.tesla_flinn_charge_flap": SimpleNamespace(
                    entity_id="binary_sensor.tesla_flinn_charge_flap",
                    state="on",
                ),
                "binary_sensor.tesla_flinn_ble_status": SimpleNamespace(
                    entity_id="binary_sensor.tesla_flinn_ble_status",
                    state="on",
                ),
                "sensor.tesla_flinn_charge_level": SimpleNamespace(
                    entity_id="sensor.tesla_flinn_charge_level",
                    state="46",
                ),
            }

        def async_all(self):
            return list(self._states.values())

        def get(self, entity_id):
            return self._states.get(entity_id)

    engine_class = _load_engine_method(
        "_async_get_ev_state",
        {
            "Any": Any,
            "Dict": Dict,
            "_LOGGER": logging.getLogger(__name__),
            "__package__": "power_sync.automations",
        },
    )
    engine = object.__new__(engine_class)
    engine._hass = SimpleNamespace(
        states=_States(),
        config_entries=SimpleNamespace(async_entries=lambda _domain: []),
        data={},
    )

    state = asyncio.run(engine._async_get_ev_state())

    assert state["is_charging"] is True
    assert state["is_plugged_in"] is True
    assert state["battery_level"] == 46
    assert state["vehicle_id"] == "ble_tesla_flinn"


def test_automation_ev_state_prefers_targetable_ble_when_activity_ties():
    """A Fleet duplicate must not hide the BLE id needed by the stop action."""

    class _States:
        def __init__(self):
            self._states = {
                "sensor.tesla_fleet_charging": SimpleNamespace(
                    entity_id="sensor.tesla_fleet_charging",
                    state="Charging",
                ),
                "sensor.tesla_flinn_charging_state": SimpleNamespace(
                    entity_id="sensor.tesla_flinn_charging_state",
                    state="Charging",
                ),
                "binary_sensor.tesla_flinn_charge_flap": SimpleNamespace(
                    entity_id="binary_sensor.tesla_flinn_charge_flap",
                    state="on",
                ),
                "binary_sensor.tesla_flinn_ble_status": SimpleNamespace(
                    entity_id="binary_sensor.tesla_flinn_ble_status",
                    state="on",
                ),
            }

        def async_all(self):
            return list(self._states.values())

        def get(self, entity_id):
            return self._states.get(entity_id)

    engine_class = _load_engine_method(
        "_async_get_ev_state",
        {
            "Any": Any,
            "Dict": Dict,
            "_LOGGER": logging.getLogger(__name__),
            "__package__": "power_sync.automations",
        },
    )
    engine = object.__new__(engine_class)
    engine._hass = SimpleNamespace(
        states=_States(),
        config_entries=SimpleNamespace(async_entries=lambda _domain: []),
        data={},
    )

    state = asyncio.run(engine._async_get_ev_state())

    assert state["is_charging"] is True
    assert state["vehicle_id"] == "ble_tesla_flinn"
