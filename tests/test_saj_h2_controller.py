"""Regression tests for SAJ H2 force-mode controls."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import types


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"


def _install_stubs() -> None:
    ha_root = types.ModuleType("homeassistant")
    ha_helpers = types.ModuleType("homeassistant.helpers")
    ha_entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")

    ha_entity_registry.async_get = lambda hass: hass.entity_registry
    ha_entity_registry.async_entries_for_config_entry = (
        lambda registry, entry_id: registry.entries_for(entry_id)
    )

    ha_helpers.entity_registry = ha_entity_registry
    ha_root.helpers = ha_helpers

    sys.modules["homeassistant"] = ha_root
    sys.modules["homeassistant.helpers"] = ha_helpers
    sys.modules["homeassistant.helpers.entity_registry"] = ha_entity_registry

    power_sync = types.ModuleType("power_sync")
    power_sync.__path__ = [str(COMPONENT_ROOT)]
    sys.modules["power_sync"] = power_sync

    inverters = types.ModuleType("power_sync.inverters")
    inverters.__path__ = [str(COMPONENT_ROOT / "inverters")]
    sys.modules["power_sync.inverters"] = inverters


_install_stubs()

from power_sync.inverters.saj_h2 import SajH2BatteryController  # noqa: E402


class _FakeState:
    def __init__(self, entity_id: str, state: str = "0"):
        self.entity_id = entity_id
        self.state = state
        self.attributes = {}


class _FakeStates:
    def __init__(self, states: list[_FakeState]):
        self._states = {state.entity_id: state for state in states}

    def get(self, entity_id: str | None):
        return self._states.get(entity_id or "")

    def set(self, entity_id: str, state: str) -> None:
        self._states[entity_id] = _FakeState(entity_id, state)


class _FakeServices:
    def __init__(
        self,
        states: _FakeStates,
        *,
        switch_turn_on_sticks: bool = True,
        fail_on: tuple[str, str, str] | None = None,
        mirror_app_mode: bool = True,
    ):
        self._states = states
        self._switch_turn_on_sticks = switch_turn_on_sticks
        self._fail_on = fail_on
        self._mirror_app_mode = mirror_app_mode
        self.calls: list[tuple[str, str, dict]] = []

    async def async_call(self, domain: str, service: str, data: dict, blocking: bool = True):
        self.calls.append((domain, service, dict(data)))
        entity_id = data.get("entity_id")
        if self._fail_on == (domain, service, entity_id):
            raise RuntimeError("service failed")
        if domain == "number" and service == "set_value":
            self._states.set(entity_id, str(data["value"]))
            if self._mirror_app_mode and entity_id == "number.saj_app_mode_input":
                self._states.set("sensor.saj_app_mode", str(data["value"]))
        elif domain == "text" and service == "set_value":
            self._states.set(entity_id, str(data["value"]))
        elif domain == "switch" and service == "turn_on" and self._switch_turn_on_sticks:
            self._states.set(entity_id, "on")
        elif domain == "switch" and service == "turn_off":
            self._states.set(entity_id, "off")


class _FakeRegistry:
    def __init__(self, entries: dict[str, list[tuple[str, str]]] | None = None):
        self._entries = entries or {}

    def entries_for(self, entry_id: str):
        return [
            types.SimpleNamespace(unique_id=unique_id, entity_id=entity_id)
            for unique_id, entity_id in self._entries.get(entry_id, [])
        ]


class _FakeHass:
    def __init__(
        self,
        states: list[_FakeState],
        *,
        switch_turn_on_sticks: bool = True,
        fail_on: tuple[str, str, str] | None = None,
        mirror_app_mode: bool = True,
        registry_entries: dict[str, list[tuple[str, str]]] | None = None,
    ):
        self.states = _FakeStates(states)
        self.services = _FakeServices(
            self.states,
            switch_turn_on_sticks=switch_turn_on_sticks,
            fail_on=fail_on,
            mirror_app_mode=mirror_app_mode,
        )
        self.entity_registry = _FakeRegistry(registry_entries)


def _passive_states(
    *,
    charge_power: str = "0",
    discharge_power: str = "0",
    passive_charge_control: str = "off",
) -> list[_FakeState]:
    return [
        _FakeState("number.saj_passive_bat_charge_power_input", charge_power),
        _FakeState("number.saj_passive_bat_discharge_power_input", discharge_power),
        _FakeState("switch.saj_passive_charge_control", passive_charge_control),
        _FakeState("sensor.saj_inverter_working_mode", "2"),
        _FakeState("sensor.saj_r_phase_inverter_voltage", "0"),
    ]


def _tou_states(
    *,
    charge_bitmask: str = "0",
    discharge_bitmask: str = "0",
    passive_charge_control: str = "off",
    passive_discharge_control: str = "off",
    charging_control: str = "off",
    discharging_control: str = "off",
) -> list[_FakeState]:
    return [
        _FakeState("text.saj_charge7_start_time_time", "00:00"),
        _FakeState("text.saj_charge7_end_time_time", "23:59"),
        _FakeState("number.saj_charge7_day_mask_input", "0"),
        _FakeState("number.saj_charge7_power_percent_input", "0"),
        _FakeState("text.saj_discharge7_start_time_time", "00:00"),
        _FakeState("text.saj_discharge7_end_time_time", "23:59"),
        _FakeState("number.saj_discharge7_day_mask_input", "0"),
        _FakeState("number.saj_discharge7_power_percent_input", "0"),
        _FakeState("number.saj_charge_time_enable_input", charge_bitmask),
        _FakeState("number.saj_discharge_time_enable_input", discharge_bitmask),
        _FakeState("sensor.saj_charge_time_enable_bitmask", charge_bitmask),
        _FakeState("sensor.saj_discharge_time_enable_bitmask", discharge_bitmask),
        _FakeState("sensor.saj_app_mode", "0"),
        _FakeState("number.saj_app_mode_input", "0"),
        _FakeState("sensor.saj_inverter_working_mode", "2"),
        _FakeState("sensor.saj_r_phase_inverter_voltage", "0"),
        _FakeState("switch.saj_passive_charge_control", passive_charge_control),
        _FakeState("switch.saj_passive_discharge_control", passive_discharge_control),
        _FakeState("switch.saj_charging_control", charging_control),
        _FakeState("switch.saj_discharging_control", discharging_control),
    ]


def _controller(hass: _FakeHass) -> SajH2BatteryController:
    controller = SajH2BatteryController(hass, saj_entry_id="saj-entry")
    controller._SWITCH_VERIFY_DELAY_SEC = 0
    controller._entity_map = {
        "charge_power": "number.saj_passive_bat_charge_power_input",
        "discharge_power": "number.saj_passive_bat_discharge_power_input",
        "passive_charge_control": "switch.saj_passive_charge_control",
        "inverter_working_mode": "sensor.saj_inverter_working_mode",
        "inverter_voltage_r": "sensor.saj_r_phase_inverter_voltage",
    }
    return controller


def _tou_controller(
    hass: _FakeHass,
    *,
    inverter_rated_kw: float = 10.0,
) -> SajH2BatteryController:
    controller = SajH2BatteryController(
        hass,
        saj_entry_id="saj-entry",
        inverter_rated_kw=inverter_rated_kw,
    )
    controller._entity_map = {
        "charge7_start_time": "text.saj_charge7_start_time_time",
        "charge7_end_time": "text.saj_charge7_end_time_time",
        "charge7_day_mask": "number.saj_charge7_day_mask_input",
        "charge7_power_percent": "number.saj_charge7_power_percent_input",
        "discharge7_start_time": "text.saj_discharge7_start_time_time",
        "discharge7_end_time": "text.saj_discharge7_end_time_time",
        "discharge7_day_mask": "number.saj_discharge7_day_mask_input",
        "discharge7_power_percent": "number.saj_discharge7_power_percent_input",
        "charge_time_enable": "number.saj_charge_time_enable_input",
        "discharge_time_enable": "number.saj_discharge_time_enable_input",
        "charge_time_enable_bitmask": "sensor.saj_charge_time_enable_bitmask",
        "discharge_time_enable_bitmask": "sensor.saj_discharge_time_enable_bitmask",
        "app_mode": "sensor.saj_app_mode",
        "app_mode_writable": "number.saj_app_mode_input",
        "inverter_working_mode": "sensor.saj_inverter_working_mode",
        "inverter_voltage_r": "sensor.saj_r_phase_inverter_voltage",
    }
    return controller


def _tou_controller_with_switches(hass: _FakeHass) -> SajH2BatteryController:
    controller = _tou_controller(hass)
    controller._entity_map.update(
        {
            "passive_charge_control": "switch.saj_passive_charge_control",
            "passive_discharge_control": "switch.saj_passive_discharge_control",
            "charging_control": "switch.saj_charging_control",
            "discharging_control": "switch.saj_discharging_control",
        }
    )
    return controller


def test_force_charge_fails_when_charge_slot_entities_are_unmapped():
    hass = _FakeHass(_tou_states())
    controller = _controller(hass)

    assert not asyncio.run(controller.force_charge(duration_minutes=30, power_w=2500))
    assert hass.services.calls == []


def test_status_uses_pv_string_sum_when_solar_total_omits_a_string():
    hass = _FakeHass(
        [
            _FakeState("sensor.saj_battery_soc", "81"),
            _FakeState("sensor.saj_battery_power", "0"),
            _FakeState("sensor.saj_grid_power", "0"),
            _FakeState("sensor.saj_total_pv_power", "345"),
            _FakeState("sensor.saj_pv1_power", "175"),
            _FakeState("sensor.saj_pv2_power", "170"),
            _FakeState("sensor.saj_pv3_power", "150"),
            _FakeState("sensor.saj_load_power", "1200"),
            _FakeState("sensor.saj_power_current_day", "9.9"),
            _FakeState("sensor.saj_feed_in_today_energy", "1.8"),
            _FakeState("sensor.saj_sell_today_energy", "0.7"),
        ]
    )
    controller = SajH2BatteryController(hass, saj_entry_id="saj-entry")
    controller._entity_map = {
        "battery_level": "sensor.saj_battery_soc",
        "battery_power": "sensor.saj_battery_power",
        "grid_power": "sensor.saj_grid_power",
        "solar_power": "sensor.saj_total_pv_power",
        "pv1_power": "sensor.saj_pv1_power",
        "pv2_power": "sensor.saj_pv2_power",
        "pv3_power": "sensor.saj_pv3_power",
        "load_power": "sensor.saj_load_power",
        "daily_solar_energy": "sensor.saj_power_current_day",
        "daily_grid_import": "sensor.saj_feed_in_today_energy",
        "daily_grid_export": "sensor.saj_sell_today_energy",
    }

    status = controller.get_status()

    assert status["solar_power"] == 0.495
    assert status["pv1_power"] == 0.175
    assert status["pv2_power"] == 0.17
    assert status["pv3_power"] == 0.15
    assert status["daily_solar_energy_kwh"] == 9.9
    assert status["daily_grid_import_kwh"] == 1.8
    assert status["daily_grid_export_kwh"] == 0.7


def test_config_entry_discovery_prefers_grid_load_power_and_current_daily_keys():
    hass = _FakeHass(
        [
            _FakeState("sensor.saj_battery_soc", "81"),
            _FakeState("sensor.saj_battery_power", "0"),
            _FakeState("sensor.saj_fast_grid_load_power", "650"),
            _FakeState("sensor.saj_fast_ct_grid_power_watt", "0"),
            _FakeState("sensor.saj_fast_total_grid_power", "2200"),
            _FakeState("sensor.saj_fast_ct_pv_power_watt", "1200"),
            _FakeState("sensor.saj_total_load_power", "1800"),
            _FakeState("sensor.saj_power_current_day", "9.9"),
            _FakeState("sensor.saj_feed_in_today_energy", "1.8"),
            _FakeState("sensor.saj_sell_today_energy", "0.7"),
        ],
        registry_entries={
            "saj-entry": [
                ("saj_Bat1SOC", "sensor.saj_battery_soc"),
                ("saj_fast_batteryPower", "sensor.saj_battery_power"),
                ("saj_fast_CT_GridPowerWatt", "sensor.saj_fast_ct_grid_power_watt"),
                ("saj_fast_totalgridPower", "sensor.saj_fast_total_grid_power"),
                ("saj_fast_gridPower", "sensor.saj_fast_grid_load_power"),
                ("saj_fast_CT_PVPowerWatt", "sensor.saj_fast_ct_pv_power_watt"),
                ("saj_fast_TotalLoadPower", "sensor.saj_total_load_power"),
                ("saj_todayenergy", "sensor.saj_power_current_day"),
                ("saj_feedin_today_energy", "sensor.saj_feed_in_today_energy"),
                ("saj_sell_today_energy", "sensor.saj_sell_today_energy"),
            ]
        },
    )
    controller = SajH2BatteryController(hass, saj_entry_id="saj-entry")

    assert asyncio.run(controller.connect())

    assert controller._entity_map["grid_power"] == "sensor.saj_fast_grid_load_power"
    assert controller._entity_map["daily_solar_energy"] == "sensor.saj_power_current_day"
    assert controller._entity_map["daily_grid_import"] == "sensor.saj_feed_in_today_energy"
    assert controller._entity_map["daily_grid_export"] == "sensor.saj_sell_today_energy"
    status = controller.get_status()
    assert status["grid_power"] == 0.65
    assert status["daily_solar_energy_kwh"] == 9.9
    assert status["daily_grid_import_kwh"] == 1.8
    assert status["daily_grid_export_kwh"] == 0.7


def test_force_charge_uses_tou_charge_slot_7_and_clears_discharge_slots():
    hass = _FakeHass(_tou_states(charge_bitmask="2", discharge_bitmask="5"))
    controller = _tou_controller(hass)

    assert asyncio.run(controller.force_charge(duration_minutes=30, power_w=2500))

    assert hass.services.calls == [
        (
            "text",
            "set_value",
            {"entity_id": "text.saj_charge7_start_time_time", "value": "00:00"},
        ),
        (
            "text",
            "set_value",
            {"entity_id": "text.saj_charge7_end_time_time", "value": "23:59"},
        ),
        (
            "number",
            "set_value",
            {"entity_id": "number.saj_charge7_day_mask_input", "value": 127},
        ),
        (
            "number",
            "set_value",
            {"entity_id": "number.saj_charge7_power_percent_input", "value": 100},
        ),
        (
            "number",
            "set_value",
            {"entity_id": "number.saj_discharge_time_enable_input", "value": 0},
        ),
        (
            "number",
            "set_value",
            {"entity_id": "number.saj_charge_time_enable_input", "value": 66},
        ),
        (
            "number",
            "set_value",
            {"entity_id": "number.saj_app_mode_input", "value": 1},
        ),
    ]
    assert controller._cached_discharge_enable == 5
    assert hass.states.get("sensor.saj_app_mode").state == "1"


def test_force_charge_clears_stale_switch_controls_before_tou_slot_control():
    hass = _FakeHass(
        _tou_states(
            passive_charge_control="on",
            passive_discharge_control="on",
            charging_control="on",
            discharging_control="on",
        )
    )
    controller = _tou_controller_with_switches(hass)

    assert asyncio.run(controller.force_charge(duration_minutes=30, power_w=2500))

    assert hass.services.calls[:4] == [
        (
            "switch",
            "turn_off",
            {"entity_id": "switch.saj_passive_charge_control"},
        ),
        (
            "switch",
            "turn_off",
            {"entity_id": "switch.saj_passive_discharge_control"},
        ),
        (
            "switch",
            "turn_off",
            {"entity_id": "switch.saj_charging_control"},
        ),
        (
            "switch",
            "turn_off",
            {"entity_id": "switch.saj_discharging_control"},
        ),
    ]
    assert hass.states.get("switch.saj_passive_charge_control").state == "off"
    assert hass.states.get("sensor.saj_app_mode").state == "1"


def test_force_discharge_clears_stale_switch_controls_before_tou_slot_control():
    hass = _FakeHass(
        _tou_states(
            passive_charge_control="on",
            passive_discharge_control="on",
            charging_control="on",
            discharging_control="on",
        )
    )
    controller = _tou_controller_with_switches(hass)

    assert asyncio.run(controller.force_discharge(duration_minutes=30, power_w=2500))

    assert hass.services.calls[:4] == [
        (
            "switch",
            "turn_off",
            {"entity_id": "switch.saj_passive_charge_control"},
        ),
        (
            "switch",
            "turn_off",
            {"entity_id": "switch.saj_passive_discharge_control"},
        ),
        (
            "switch",
            "turn_off",
            {"entity_id": "switch.saj_charging_control"},
        ),
        (
            "switch",
            "turn_off",
            {"entity_id": "switch.saj_discharging_control"},
        ),
    ]
    assert hass.states.get("switch.saj_passive_charge_control").state == "off"
    assert hass.states.get("sensor.saj_app_mode").state == "1"


def test_force_discharge_sets_tou_slot_power_from_requested_watts():
    hass = _FakeHass(_tou_states())
    controller = _tou_controller(hass, inverter_rated_kw=10.0)

    assert asyncio.run(controller.force_discharge(duration_minutes=30, power_w=2500))

    assert (
        "number",
        "set_value",
        {"entity_id": "number.saj_discharge7_power_percent_input", "value": 25},
    ) in hass.services.calls


def test_force_discharge_clamps_tou_slot_power_percent():
    hass = _FakeHass(_tou_states())
    controller = _tou_controller(hass, inverter_rated_kw=10.0)

    assert asyncio.run(controller.force_discharge(duration_minutes=30, power_w=25000))

    assert (
        "number",
        "set_value",
        {"entity_id": "number.saj_discharge7_power_percent_input", "value": 100},
    ) in hass.services.calls


def test_restore_normal_after_force_charge_clears_charge_slot_and_restores_discharge_slots():
    hass = _FakeHass(_tou_states(charge_bitmask="64", discharge_bitmask="3"))
    controller = _tou_controller(hass)

    assert asyncio.run(controller.force_charge(duration_minutes=30, power_w=2500))
    assert asyncio.run(controller.restore_normal())

    assert (
        "number",
        "set_value",
        {"entity_id": "number.saj_charge_time_enable_input", "value": 0},
    ) in hass.services.calls
    assert (
        "number",
        "set_value",
        {"entity_id": "number.saj_discharge_time_enable_input", "value": 3},
    ) in hass.services.calls
    assert (
        "number",
        "set_value",
        {"entity_id": "number.saj_app_mode_input", "value": 0},
    ) in hass.services.calls
    assert controller._cached_discharge_enable is None


def test_restore_normal_after_cross_type_force_does_not_reapply_stale_charge_bit():
    # force_charge sets slot-7's shared bit on charge_time_enable, then
    # force_discharge is called without an intervening restore — its
    # capture of charge_time_enable_bitmask must not cache PowerSync's own
    # bit, or restore_normal will write it straight back onto the user's
    # charge slots.
    hass = _FakeHass(_tou_states(charge_bitmask="2", discharge_bitmask="0"))
    controller = _tou_controller(hass)

    assert asyncio.run(controller.force_charge(duration_minutes=30, power_w=2500))
    # Simulate the inverter's bitmask sensor confirming the bit force_charge
    # just wrote (real hardware would report this on the next poll).
    hass.states.set("sensor.saj_charge_time_enable_bitmask", "66")

    assert asyncio.run(controller.force_discharge(duration_minutes=30, power_w=2500))
    assert asyncio.run(controller.restore_normal())

    charge_writes = [
        call[2]["value"]
        for call in hass.services.calls
        if call[2].get("entity_id") == "number.saj_charge_time_enable_input"
    ]
    assert charge_writes[-1] == 2


def test_force_charge_attempts_restore_normal_on_mid_sequence_exception():
    hass = _FakeHass(
        _tou_states(),
        fail_on=(
            "number",
            "set_value",
            "number.saj_charge7_power_percent_input",
        ),
    )
    controller = _tou_controller(hass)
    restore_calls = 0

    async def restore_normal():
        nonlocal restore_calls
        restore_calls += 1
        return True

    controller.restore_normal = restore_normal

    assert not asyncio.run(controller.force_charge(duration_minutes=30, power_w=2500))
    assert restore_calls == 1


def test_set_idle_fails_when_passive_switch_does_not_stick_on():
    hass = _FakeHass(_passive_states(), switch_turn_on_sticks=False)
    controller = _controller(hass)

    assert not asyncio.run(controller.set_idle())

    assert (
        "switch",
        "turn_on",
        {"entity_id": "switch.saj_passive_charge_control"},
    ) in hass.services.calls
    assert hass.states.get("switch.saj_passive_charge_control").state == "off"


def test_set_idle_drives_and_verifies_passive_app_mode_when_mapped():
    hass = _FakeHass(
        _passive_states()
        + [
            _FakeState("sensor.saj_app_mode", "0"),
            _FakeState("number.saj_app_mode_input", "0"),
        ],
    )
    controller = _controller(hass)
    controller._APP_MODE_VERIFY_DELAY_SEC = 0
    controller._entity_map["app_mode"] = "sensor.saj_app_mode"
    controller._entity_map["app_mode_writable"] = "number.saj_app_mode_input"

    assert asyncio.run(controller.set_idle())

    assert (
        "number",
        "set_value",
        {"entity_id": "number.saj_app_mode_input", "value": 3},
    ) in hass.services.calls
    assert hass.states.get("sensor.saj_app_mode").state == "3"
