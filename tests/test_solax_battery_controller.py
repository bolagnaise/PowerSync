"""Regression tests for Solax Modbus entity mapping and Mode1 control."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import sys
import types


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"


def _install_stubs() -> None:
    ha_root = types.ModuleType("homeassistant")
    ha_helpers = types.ModuleType("homeassistant.helpers")
    ha_entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")
    ha_event = types.ModuleType("homeassistant.helpers.event")
    ha_util = types.ModuleType("homeassistant.util")
    ha_dt = types.ModuleType("homeassistant.util.dt")

    ha_entity_registry.async_get = lambda hass: hass.entity_registry
    ha_entity_registry.async_entries_for_config_entry = (
        lambda registry, entry_id: registry.entries_for(entry_id)
    )
    ha_event.async_call_later = lambda *args, **kwargs: (lambda: None)
    ha_dt.now = lambda *args, **kwargs: datetime(2026, 5, 3, tzinfo=timezone.utc)
    ha_dt.utcnow = lambda *args, **kwargs: datetime(2026, 5, 3, tzinfo=timezone.utc)
    ha_dt.UTC = timezone.utc

    ha_helpers.entity_registry = ha_entity_registry
    ha_helpers.event = ha_event
    ha_util.dt = ha_dt
    ha_root.helpers = ha_helpers
    ha_root.util = ha_util

    sys.modules["homeassistant"] = ha_root
    sys.modules["homeassistant.helpers"] = ha_helpers
    sys.modules["homeassistant.helpers.entity_registry"] = ha_entity_registry
    sys.modules["homeassistant.helpers.event"] = ha_event
    sys.modules["homeassistant.util"] = ha_util
    sys.modules["homeassistant.util.dt"] = ha_dt

    power_sync = types.ModuleType("power_sync")
    power_sync.__path__ = [str(COMPONENT_ROOT)]
    sys.modules["power_sync"] = power_sync

    inverters = types.ModuleType("power_sync.inverters")
    inverters.__path__ = [str(COMPONENT_ROOT / "inverters")]
    sys.modules["power_sync.inverters"] = inverters


_install_stubs()

from power_sync.inverters.solax_battery import SolaxBatteryController  # noqa: E402


class _FakeState:
    def __init__(
        self,
        entity_id: str,
        state: str = "0",
        options: list[str] | None = None,
        attributes: dict | None = None,
    ):
        self.entity_id = entity_id
        self.state = state
        self.attributes = {"options": options or [], **(attributes or {})}


class _FakeStates:
    def __init__(self, states: list[_FakeState]):
        self._states = {state.entity_id: state for state in states}

    def get(self, entity_id: str | None):
        return self._states.get(entity_id or "")

    def async_all(self, domain: str | None = None):
        if domain is None:
            return list(self._states.values())
        prefix = f"{domain}."
        return [state for state in self._states.values() if state.entity_id.startswith(prefix)]


class _FakeServices:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    async def async_call(self, domain: str, service: str, data: dict, blocking: bool = True):
        self.calls.append((domain, service, dict(data)))


class _FakeRegistry:
    def __init__(self, entries: dict[str, list[str]] | None = None):
        self._entries = entries or {}

    def entries_for(self, entry_id: str):
        return [
            SimpleNamespace(entity_id=entity_id)
            for entity_id in self._entries.get(entry_id, [])
        ]


class _FakeHass:
    def __init__(
        self,
        states: list[_FakeState],
        registry_entries: dict[str, list[str]] | None = None,
    ):
        self.states = _FakeStates(states)
        self.services = _FakeServices()
        self.entity_registry = _FakeRegistry(registry_entries)


def _base_states() -> list[_FakeState]:
    return [
        _FakeState("sensor.solax_battery_capacity", "55"),
        _FakeState("sensor.solax_total_battery_power_charge", "120"),
        _FakeState("sensor.solax_measured_power", "0"),
        _FakeState("select.solax_charger_use_mode", "Self Use Mode", ["Self Use Mode"]),
        _FakeState("number.solax_battery_charge_max_current", "25"),
        _FakeState("number.solax_battery_discharge_max_current", "25"),
        _FakeState("number.solax_selfuse_discharge_min_soc", "20"),
    ]


def _mode1_states() -> list[_FakeState]:
    return [
        _FakeState(
            "select.solax_remotecontrol_power_control",
            "Disabled",
            ["Disabled", "Enabled Battery Control", "Enabled Self Use"],
        ),
        _FakeState("number.solax_remotecontrol_active_power", "0"),
        _FakeState("number.solax_inverter_remotecontrol_autorepeat_duration_mode_1_9", "0"),
        _FakeState("button.solax_remotecontrol_trigger", "unknown"),
    ]


def _manual_states() -> list[_FakeState]:
    return [
        _FakeState("select.solax_manual_mode_select", "Stop Charge and Discharge"),
    ]


def _x3_ultra_states() -> list[_FakeState]:
    return [
        _FakeState("sensor.solax_inverter_bms_battery_capacity", "0"),
        _FakeState("sensor.solax_battery_capacity", "72"),
        _FakeState("sensor.solax_total_battery_power_charge", "-500"),
        _FakeState("sensor.solax_inverter_meter_2_measured_power", "999"),
        _FakeState("sensor.solax_measured_power", "-1500"),
        _FakeState("sensor.solax_inverter_power", "0"),
        _FakeState("sensor.solax_house_load", "2300"),
        _FakeState("sensor.solax_energy_dashboard_solax_solar_power", "0"),
        _FakeState("sensor.solax_pv_power_1", "2000"),
        _FakeState("sensor.solax_pv_power_2", "1600"),
        _FakeState("sensor.solax_pv_power_3", "900"),
        _FakeState("sensor.solax_pv_voltage_1", "420"),
        _FakeState("sensor.solax_pv_voltage_2", "410"),
        _FakeState("sensor.solax_pv_voltage_3", "405"),
        _FakeState("sensor.solax_pv_current_1", "4.8"),
        _FakeState("sensor.solax_pv_current_2", "3.9"),
        _FakeState("sensor.solax_pv_current_3", "2.2"),
        _FakeState("select.solax_inverter_charger_use_mode", "Self Use Mode", ["Self Use Mode"]),
        _FakeState("number.solax_inverter_battery_charge_max_current", "25"),
        _FakeState("number.solax_inverter_battery_discharge_max_current", "25"),
        _FakeState("select.solax_inverter_remotecontrol_power_control_mode_1", "Disabled", ["Disabled", "Enabled Battery Control"]),
        _FakeState("select.solax_inverter_remotecontrol_set_type_mode_1_9", "Set", ["Set"]),
        _FakeState("number.solax_inverter_remotecontrol_active_power_mode_1", "0"),
        _FakeState("number.solax_inverter_remotecontrol_duration_mode_1_8", "0"),
        _FakeState("number.solax_inverter_remotecontrol_autorepeat_duration_mode_1_9", "0"),
        _FakeState("button.solax_inverter_remotecontrol_trigger_mode_1_7", "unknown"),
    ]


def _without_entity(states: list[_FakeState], entity_id: str) -> list[_FakeState]:
    return [state for state in states if state.entity_id != entity_id]


async def _connect_mode1_controller():
    hass = _FakeHass(_base_states() + _mode1_states())
    controller = SolaxBatteryController(hass, entity_prefix="solax")
    assert await controller.connect()
    return hass, controller


def test_mode1_profile_validates_without_manual_mode_entities():
    hass, controller = asyncio.run(_connect_mode1_controller())

    assert controller._control_profile == "remote_control"
    status = controller.get_status()
    assert status["battery_level"] == 55.0
    assert status["battery_power"] == -0.12
    assert hass.services.calls == []


def test_mode1_profile_preferred_when_manual_mode_also_exists():
    hass = _FakeHass(_base_states() + _mode1_states() + _manual_states())
    controller = SolaxBatteryController(hass, entity_prefix="solax")

    assert asyncio.run(controller.connect())
    assert controller._control_profile == "remote_control"


def test_status_lazily_discovers_entities():
    hass = _FakeHass(_base_states() + _mode1_states())
    controller = SolaxBatteryController(hass, entity_prefix="solax")

    status = controller.get_status()

    assert status["battery_level"] == 55.0
    assert status["battery_power"] == -0.12


def test_status_reads_native_daily_energy_counters():
    hass = _FakeHass(
        _base_states()
        + _mode1_states()
        + [
            _FakeState("sensor.solax_today_s_solar_energy", "6.5"),
            _FakeState("sensor.solax_today_s_import_energy", "1.74"),
            _FakeState("sensor.solax_today_s_export_energy", "0.04"),
            _FakeState("sensor.solax_battery_input_energy_today", "4.26"),
            _FakeState("sensor.solax_battery_output_energy_today", "4.10"),
        ]
    )
    controller = SolaxBatteryController(hass, entity_prefix="solax")

    status = controller.get_status()

    assert status["daily_solar_energy_kwh"] == 6.5
    assert status["daily_grid_import_kwh"] == 1.74
    assert status["daily_grid_export_kwh"] == 0.04
    assert status["daily_battery_charge_kwh"] == 4.26
    assert status["daily_battery_discharge_kwh"] == 4.10


def test_mode1_force_charge_uses_remotecontrol_entities():
    hass, controller = asyncio.run(_connect_mode1_controller())

    assert asyncio.run(controller.force_charge(duration_minutes=30, power_w=2500))

    assert ("number", "set_value", {
        "entity_id": "number.solax_remotecontrol_active_power",
        "value": 2500,
    }) in hass.services.calls
    assert ("select", "select_option", {
        "entity_id": "select.solax_remotecontrol_power_control",
        "option": "Enabled Battery Control",
    }) in hass.services.calls
    assert ("number", "set_value", {
        "entity_id": "number.solax_inverter_remotecontrol_autorepeat_duration_mode_1_9",
        "value": 1800,
    }) in hass.services.calls
    assert ("button", "press", {
        "entity_id": "button.solax_remotecontrol_trigger",
    }) in hass.services.calls


def test_restore_normal_lazily_discovers_mode1_profile():
    hass = _FakeHass(_base_states() + _mode1_states())
    controller = SolaxBatteryController(hass, entity_prefix="solax")

    assert controller._control_profile == "unknown"
    assert asyncio.run(controller.restore_normal())

    assert controller._control_profile == "remote_control"
    assert ("select", "select_option", {
        "entity_id": "select.solax_remotecontrol_power_control",
        "option": "Disabled",
    }) in hass.services.calls
    assert all(
        call[2].get("entity_id") != "select.solax_manual_mode_select"
        for call in hass.services.calls
    )


def test_mode1_allows_missing_backup_reserve_entity():
    states = _without_entity(
        _base_states(),
        "number.solax_selfuse_discharge_min_soc",
    )
    hass = _FakeHass(states + _mode1_states())
    controller = SolaxBatteryController(hass, entity_prefix="solax")

    assert asyncio.run(controller.connect())
    assert controller._control_profile == "remote_control"
    assert not asyncio.run(controller.set_backup_reserve(30))


def test_backup_reserve_honors_entity_minimum_below_15_percent():
    states = _base_states()
    reserve = next(
        state
        for state in states
        if state.entity_id == "number.solax_selfuse_discharge_min_soc"
    )
    reserve.attributes.update({"min": 10, "max": 100})
    hass = _FakeHass(states + _mode1_states())
    controller = SolaxBatteryController(hass, entity_prefix="solax")

    assert asyncio.run(controller.set_backup_reserve(10))
    assert ("number", "set_value", {
        "entity_id": "number.solax_selfuse_discharge_min_soc",
        "value": 10,
    }) in hass.services.calls

    assert asyncio.run(controller.set_backup_reserve(12))
    assert ("number", "set_value", {
        "entity_id": "number.solax_selfuse_discharge_min_soc",
        "value": 12,
    }) in hass.services.calls


def test_backup_reserve_keeps_legacy_15_percent_fallback_without_bounds():
    hass = _FakeHass(_base_states() + _mode1_states())
    controller = SolaxBatteryController(hass, entity_prefix="solax")

    assert asyncio.run(controller.set_backup_reserve(10))
    assert ("number", "set_value", {
        "entity_id": "number.solax_selfuse_discharge_min_soc",
        "value": 15,
    }) in hass.services.calls


def test_force_time_backup_reserve_uses_each_number_entities_own_bounds():
    states = _base_states()
    reserve = next(
        state
        for state in states
        if state.entity_id == "number.solax_selfuse_discharge_min_soc"
    )
    reserve.attributes.update({"min": 10, "max": 100})
    hass = _FakeHass(states + _mode1_states())
    controller = SolaxBatteryController(hass, entity_prefix="solax")
    assert asyncio.run(controller.connect())

    grid_tied = _FakeState(
        "number.solax_battery_minimum_capacity_grid_tied",
        "15",
        attributes={"min": 15, "max": 100},
    )
    hass.states._states[grid_tied.entity_id] = grid_tied
    controller._control_profile = "force_time"
    controller._entity_map["grid_tied_min_soc"] = grid_tied.entity_id

    assert asyncio.run(controller.set_backup_reserve(10))
    assert ("number", "set_value", {
        "entity_id": "number.solax_selfuse_discharge_min_soc",
        "value": 10,
    }) in hass.services.calls
    assert ("number", "set_value", {
        "entity_id": "number.solax_battery_minimum_capacity_grid_tied",
        "value": 15,
    }) in hass.services.calls


def test_discovery_prefers_live_state_over_stale_registry_entity():
    hass = _FakeHass(_base_states() + _mode1_states())
    controller = SolaxBatteryController(hass)

    entity_id = controller._resolve_entity_id(
        [
            "sensor.solax_inverter_bms_battery_capacity",
            "sensor.solax_battery_capacity",
        ],
        "sensor",
        ("battery_capacity",),
        legacy_prefix=None,
    )

    assert entity_id == "sensor.solax_battery_capacity"


def test_config_entry_discovery_falls_back_to_live_state_ids():
    hass = _FakeHass(
        _base_states() + _mode1_states(),
        registry_entries={
            "solax-entry": [
                "sensor.solax_inverter_bms_battery_capacity",
                "sensor.solax_inverter_meter_2_measured_power",
            ],
        },
    )
    controller = SolaxBatteryController(hass, solax_entry_id="solax-entry")

    assert asyncio.run(controller.connect())
    assert controller._entity_map["battery_level"] == "sensor.solax_battery_capacity"
    assert controller._entity_map["grid_power"] == "sensor.solax_measured_power"


def test_x3_ultra_entity_aliases_map_live_telemetry():
    entity_ids = [state.entity_id for state in _x3_ultra_states()]
    hass = _FakeHass(
        _x3_ultra_states(),
        registry_entries={"solax-entry": entity_ids},
    )
    controller = SolaxBatteryController(hass, solax_entry_id="solax-entry")

    assert asyncio.run(controller.connect())
    assert controller._control_profile == "remote_control"
    assert controller._entity_map["battery_level"] == "sensor.solax_battery_capacity"
    assert controller._entity_map["grid_power"] == "sensor.solax_measured_power"
    assert controller._entity_map["load_power"] == "sensor.solax_house_load"
    assert controller._entity_map["pv3_power"] == "sensor.solax_pv_power_3"
    assert controller._entity_map["remotecontrol_trigger"] == "button.solax_inverter_remotecontrol_trigger_mode_1_7"

    status = controller.get_status()
    assert status["battery_level"] == 72.0
    assert status["battery_power"] == 0.5
    assert status["grid_power"] == 1.5
    assert status["load_power"] == 2.3
    assert status["solar_power"] == 4.5
    assert status["pv1_power"] == 2.0
    assert status["pv2_power"] == 1.6
    assert status["pv3_power"] == 0.9
    assert status["pv1_voltage"] == 420.0
    assert status["pv2_current"] == 3.9


def test_x3_ultra_solar_total_uses_pv_string_sum_when_total_is_partial():
    states = _x3_ultra_states()
    for state in states:
        if state.entity_id == "sensor.solax_energy_dashboard_solax_solar_power":
            state.state = "3600"
    entity_ids = [state.entity_id for state in states]
    hass = _FakeHass(states, registry_entries={"solax-entry": entity_ids})
    controller = SolaxBatteryController(hass, solax_entry_id="solax-entry")

    assert asyncio.run(controller.connect())

    status = controller.get_status()
    assert status["solar_power"] == 4.5
