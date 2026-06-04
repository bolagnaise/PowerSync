"""Regression tests for the FoxESS foxess_modbus entity bridge."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"


def _install_stubs() -> None:
    ha_root = types.ModuleType("homeassistant")
    ha_helpers = types.ModuleType("homeassistant.helpers")
    ha_device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    ha_entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")

    ha_device_registry.async_get = lambda hass: getattr(
        hass, "device_registry", SimpleNamespace(devices={})
    )
    ha_entity_registry.async_get = lambda hass: hass.entity_registry
    ha_entity_registry.async_entries_for_config_entry = (
        lambda registry, entry_id: registry.entries_for(entry_id)
    )

    ha_helpers.device_registry = ha_device_registry
    ha_helpers.entity_registry = ha_entity_registry
    ha_root.helpers = ha_helpers

    sys.modules["homeassistant"] = ha_root
    sys.modules["homeassistant.helpers"] = ha_helpers
    sys.modules["homeassistant.helpers.device_registry"] = ha_device_registry
    sys.modules["homeassistant.helpers.entity_registry"] = ha_entity_registry

    power_sync = types.ModuleType("power_sync")
    power_sync.__path__ = [str(COMPONENT_ROOT)]
    sys.modules["power_sync"] = power_sync

    inverters = types.ModuleType("power_sync.inverters")
    inverters.__path__ = [str(COMPONENT_ROOT / "inverters")]
    sys.modules["power_sync.inverters"] = inverters
    sys.modules.pop("power_sync.inverters.foxess_entity", None)


_install_stubs()

from power_sync.inverters.foxess_entity import FoxESSEntityController  # noqa: E402


class _FakeState:
    def __init__(
        self,
        entity_id: str,
        state: str = "0",
        attributes: dict | None = None,
    ) -> None:
        self.entity_id = entity_id
        self.state = state
        self.attributes = attributes or {}


class _FakeStates:
    def __init__(self, states: list[_FakeState]) -> None:
        self._states = {state.entity_id: state for state in states}

    def get(self, entity_id: str | None):
        return self._states.get(entity_id or "")

    def async_all(self, domain: str | None = None):
        if domain is None:
            return list(self._states.values())
        prefix = f"{domain}."
        return [
            state
            for state in self._states.values()
            if state.entity_id.startswith(prefix)
        ]


class _FakeServices:
    def __init__(
        self,
        available_services: set[tuple[str, str]] | None = None,
    ) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self._available_services = available_services or set()

    def has_service(self, domain: str, service: str) -> bool:
        return (domain, service) in self._available_services

    async def async_call(
        self,
        domain: str,
        service: str,
        data: dict,
        blocking: bool = True,
    ) -> None:
        self.calls.append((domain, service, dict(data)))


class _FakeRegistry:
    def __init__(self, entries: dict[str, list[str | SimpleNamespace]] | None = None) -> None:
        self._entries = entries or {}

    def entries_for(self, entry_id: str):
        entries = []
        for entry in self._entries.get(entry_id, []):
            if isinstance(entry, str):
                entries.append(SimpleNamespace(entity_id=entry))
            else:
                entries.append(entry)
        return entries


class _FakeConfigEntries:
    def __init__(
        self,
        titles: dict[str, str] | None = None,
        data: dict[str, dict] | None = None,
    ) -> None:
        self._titles = titles or {}
        self._data = data or {}

    def async_get_entry(self, entry_id: str):
        title = self._titles.get(entry_id)
        data = self._data.get(entry_id)
        if title is None and data is None:
            return None
        return SimpleNamespace(title=title or "", data=data or {}, options={})


class _FakeHass:
    def __init__(
        self,
        states: list[_FakeState],
        registry_entries: dict[str, list[str | SimpleNamespace]] | None = None,
        service_names: set[tuple[str, str]] | None = None,
        config_entry_titles: dict[str, str] | None = None,
        config_entry_data: dict[str, dict] | None = None,
        devices: dict[str, SimpleNamespace] | None = None,
    ) -> None:
        self.states = _FakeStates(states)
        self.services = _FakeServices(service_names)
        self.entity_registry = _FakeRegistry(registry_entries)
        self.config_entries = _FakeConfigEntries(config_entry_titles, config_entry_data)
        self.device_registry = SimpleNamespace(devices=devices or {})


def _kw() -> dict[str, str]:
    return {"unit_of_measurement": "kW"}


def _kw_range(minimum: float, maximum: float) -> dict[str, float | str]:
    return {"unit_of_measurement": "kW", "min": minimum, "max": maximum}


def _w() -> dict[str, str]:
    return {"unit_of_measurement": "W"}


def _kwh() -> dict[str, str]:
    return {"unit_of_measurement": "kWh"}


def _base_states(prefix: str = "foxess") -> list[_FakeState]:
    return [
        _FakeState(f"sensor.{prefix}_battery_soc", "62"),
        _FakeState(f"sensor.{prefix}_battery_soh", "97"),
        _FakeState(f"sensor.{prefix}_battery_voltage", "410"),
        _FakeState(f"sensor.{prefix}_battery_temp", "24"),
        _FakeState(f"sensor.{prefix}_invbatpower", "1.4", _kw()),
        _FakeState(f"sensor.{prefix}_grid_ct", "0.6", _kw()),
        _FakeState(f"sensor.{prefix}_pv_power_now", "4.2", _kw()),
        _FakeState(f"sensor.{prefix}_load_power", "2.2", _kw()),
        _FakeState(f"sensor.{prefix}_pv1_power", "2.1", _kw()),
        _FakeState(f"sensor.{prefix}_pv1_voltage", "405"),
        _FakeState(f"sensor.{prefix}_pv1_current", "5.2"),
        _FakeState(f"sensor.{prefix}_solar_energy_today", "12.5", _kwh()),
        _FakeState(f"sensor.{prefix}_grid_consumption_energy_today", "3.1", _kwh()),
        _FakeState(f"sensor.{prefix}_feed_in_energy_today", "4.4", _kwh()),
        _FakeState(f"sensor.{prefix}_battery_charge_today", "5.6", _kwh()),
        _FakeState(f"sensor.{prefix}_battery_discharge_today", "4.8", _kwh()),
        _FakeState(
            f"select.{prefix}_work_mode",
            "Self Use",
            {
                "options": [
                    "Self Use",
                    "Feed-in First",
                    "Back-up",
                    "Force Charge",
                    "Force Discharge",
                ],
            },
        ),
        _FakeState(f"number.{prefix}_force_charge_power", "0", _kw()),
        _FakeState(f"number.{prefix}_force_discharge_power", "0", _kw()),
        _FakeState(f"number.{prefix}_min_soc_on_grid", "20"),
        _FakeState(f"number.{prefix}_max_charge_current", "25"),
        _FakeState(f"number.{prefix}_max_discharge_current", "25"),
        _FakeState(f"number.{prefix}_export_power_limit", "99999"),
    ]


def _h3_smart_device() -> SimpleNamespace:
    return SimpleNamespace(
        identifiers={("foxess_modbus", "H3_SMART", "AUX", "Kitchen FoxESS")},
        model="H3_SMART - AUX",
    )


def _without_suffix(states: list[_FakeState], suffixes: tuple[str, ...]) -> list[_FakeState]:
    return [
        state
        for state in states
        if not any(state.entity_id.endswith(suffix) for suffix in suffixes)
    ]


def _unprefixed_states() -> list[_FakeState]:
    return [
        _FakeState(
            state.entity_id.replace(".foxess_", "."),
            state.state,
            dict(state.attributes),
        )
        for state in _base_states()
    ]


def test_prefix_discovery_maps_telemetry_and_daily_energy():
    hass = _FakeHass(_base_states())
    controller = FoxESSEntityController(hass, entity_prefix="foxess")

    assert asyncio.run(controller.connect())
    status = controller.get_status()

    assert status["battery_level"] == 62.0
    assert status["battery_soh"] == 97.0
    assert status["battery_temperature"] == 24.0
    assert status["battery_power"] == 1.4
    assert status["grid_power"] == -0.6
    assert status["solar_power"] == 4.2
    assert status["load_power"] == 2.2
    assert status["backup_reserve"] == 20.0
    assert status["daily_solar_energy_kwh"] == 12.5
    assert status["daily_grid_import_kwh"] == 3.1
    assert status["daily_grid_export_kwh"] == 4.4
    assert status["daily_battery_charge_kwh"] == 5.6
    assert status["daily_battery_discharge_kwh"] == 4.8
    assert status["pv1_power"] == 2.1
    assert status["pv1_voltage"] == 405.0
    assert status["pv1_current"] == 5.2
    assert status["battery_max_charge_power_w"] == 10250


def test_fallback_sensors_normalize_grid_and_battery_signs():
    states = _without_suffix(_base_states(), ("_invbatpower", "_grid_ct"))
    states.extend(
        [
            _FakeState("sensor.foxess_battery_charge", "0.2", _kw()),
            _FakeState("sensor.foxess_battery_discharge", "0.7", _kw()),
            _FakeState("sensor.foxess_grid_consumption", "1.2", _kw()),
            _FakeState("sensor.foxess_feed_in", "0.3", _kw()),
        ]
    )
    hass = _FakeHass(states)
    controller = FoxESSEntityController(hass, entity_prefix="foxess")

    assert asyncio.run(controller.connect())
    status = controller.get_status()

    assert status["battery_power"] == pytest.approx(0.5)
    assert status["grid_power"] == pytest.approx(0.9)


def test_solar_power_uses_pv_string_sum_when_reported_total_omits_pv4():
    states = _without_suffix(_base_states(), ("_pv_power_now", "_pv1_power"))
    states.extend(
        [
            _FakeState("sensor.foxess_pv_power_now", "345", _w()),
            _FakeState("sensor.foxess_pv1_power", "175", _w()),
            _FakeState("sensor.foxess_pv2_power", "170", _w()),
            _FakeState("sensor.foxess_pv3_power", "150", _w()),
            _FakeState("sensor.foxess_pv4_power", "160", _w()),
        ]
    )
    hass = _FakeHass(states)
    controller = FoxESSEntityController(hass, entity_prefix="foxess")

    assert asyncio.run(controller.connect())
    status = controller.get_status()

    assert status["solar_power"] == pytest.approx(0.655)
    assert status["pv4_power"] == pytest.approx(0.16)


def test_selected_config_entry_is_preferred_before_suffix_fallback():
    preferred_states = _base_states(prefix="renamed_fox")
    fallback_states = _base_states(prefix="foxess")
    registry_ids = [state.entity_id for state in preferred_states]
    hass = _FakeHass(
        preferred_states + fallback_states,
        registry_entries={"fox-entry": registry_ids},
    )
    controller = FoxESSEntityController(hass, foxess_entry_id="fox-entry")

    assert asyncio.run(controller.connect())

    assert controller._entity_map["battery_level"] == "sensor.renamed_fox_battery_soc"
    assert controller._entity_map["work_mode"] == "select.renamed_fox_work_mode"
    assert controller._entity_map["force_charge_power"] == (
        "number.renamed_fox_force_charge_power"
    )


def test_selected_config_entry_falls_back_to_live_suffix_discovery():
    states = _base_states()
    hass = _FakeHass(
        states,
        registry_entries={
            "fox-entry": [
                "sensor.foxess_battery_soc",
                "sensor.foxess_grid_ct",
            ],
        },
    )
    controller = FoxESSEntityController(hass, foxess_entry_id="fox-entry")

    assert asyncio.run(controller.connect())

    assert controller._entity_map["battery_level"] == "sensor.foxess_battery_soc"
    assert controller._entity_map["work_mode"] == "select.foxess_work_mode"
    assert controller._entity_map["force_discharge_power"] == (
        "number.foxess_force_discharge_power"
    )


def test_selected_config_entry_maps_unprefixed_foxess_modbus_entities():
    states = [
        _FakeState("sensor.battery_soc_1", "62"),
        _FakeState("sensor.battery_soh_1", "97"),
        _FakeState("sensor.battery_1_voltage", "410"),
        _FakeState("sensor.battery_temp_1", "24"),
        _FakeState("sensor.invbatpower", "1.4", _kw()),
        _FakeState("sensor.grid_ct", "0.6", _kw()),
        _FakeState("sensor.pv_power_now", "4.2", _kw()),
        _FakeState("sensor.load_power", "2.2", _kw()),
        _FakeState(
            "select.work_mode",
            "Self Use",
            {
                "options": [
                    "Self Use",
                    "Feed-in First",
                    "Back-up",
                    "Force Charge",
                    "Force Discharge",
                ],
            },
        ),
        _FakeState("number.force_charge_power", "0", _kw()),
        _FakeState("number.force_discharge_power", "0", _kw()),
        _FakeState("number.min_soc_on_grid", "20"),
    ]
    registry_ids = [state.entity_id for state in states]
    hass = _FakeHass(states, registry_entries={"fox-entry": registry_ids})
    controller = FoxESSEntityController(hass, foxess_entry_id="fox-entry")

    assert asyncio.run(controller.connect())
    status = controller.get_status()

    assert controller._entity_map["battery_level"] == "sensor.battery_soc_1"
    assert controller._entity_map["work_mode"] == "select.work_mode"
    assert controller._entity_map["force_charge_power"] == "number.force_charge_power"
    assert controller._entity_map["backup_reserve"] == "number.min_soc_on_grid"
    assert status["battery_level"] == 62.0
    assert status["grid_power"] == -0.6


def test_force_restore_reserve_work_mode_and_limit_controls():
    hass = _FakeHass(_base_states())
    controller = FoxESSEntityController(hass, entity_prefix="foxess")

    assert asyncio.run(controller.connect())
    assert asyncio.run(controller.force_charge(duration_minutes=30, power_w=5000))
    assert asyncio.run(controller.force_discharge(duration_minutes=30, power_w=4200))
    assert asyncio.run(controller.restore_normal())
    assert asyncio.run(controller.set_backup_reserve(33))
    assert asyncio.run(controller.set_work_mode("feed_in"))
    assert asyncio.run(controller.set_work_mode("backup"))
    assert asyncio.run(controller.set_charge_rate_limit(18))
    assert asyncio.run(controller.set_discharge_rate_limit(22))
    assert asyncio.run(controller.curtail(1500))

    assert hass.services.calls == [
        (
            "number",
            "set_value",
            {"entity_id": "number.foxess_force_charge_power", "value": 5.0},
        ),
        (
            "select",
            "select_option",
            {"entity_id": "select.foxess_work_mode", "option": "Force Charge"},
        ),
        (
            "number",
            "set_value",
            {"entity_id": "number.foxess_force_discharge_power", "value": 4.2},
        ),
        (
            "select",
            "select_option",
            {"entity_id": "select.foxess_work_mode", "option": "Force Discharge"},
        ),
        (
            "select",
            "select_option",
            {"entity_id": "select.foxess_work_mode", "option": "Self Use"},
        ),
        (
            "number",
            "set_value",
            {"entity_id": "number.foxess_min_soc_on_grid", "value": 33},
        ),
        (
            "select",
            "select_option",
            {"entity_id": "select.foxess_work_mode", "option": "Feed-in First"},
        ),
        (
            "select",
            "select_option",
            {"entity_id": "select.foxess_work_mode", "option": "Back-up"},
        ),
        (
            "number",
            "set_value",
            {"entity_id": "number.foxess_max_charge_current", "value": 18.0},
        ),
        (
            "number",
            "set_value",
            {"entity_id": "number.foxess_max_discharge_current", "value": 22.0},
        ),
        (
            "number",
            "set_value",
            {"entity_id": "number.foxess_export_power_limit", "value": 1500},
        ),
    ]


def test_force_charge_and_discharge_power_clamp_to_number_entity_range():
    states = _base_states()
    for state in states:
        if state.entity_id == "number.foxess_force_charge_power":
            state.attributes = _kw_range(0, 15)
        if state.entity_id == "number.foxess_force_discharge_power":
            state.attributes = _kw_range(0, 5)
    hass = _FakeHass(states)
    controller = FoxESSEntityController(hass, entity_prefix="foxess")

    assert asyncio.run(controller.connect())
    assert asyncio.run(controller.force_charge(duration_minutes=30, power_w=20000))
    assert asyncio.run(controller.force_discharge(duration_minutes=30, power_w=7000))

    assert hass.services.calls == [
        (
            "number",
            "set_value",
            {"entity_id": "number.foxess_force_charge_power", "value": 15.0},
        ),
        (
            "select",
            "select_option",
            {"entity_id": "select.foxess_work_mode", "option": "Force Charge"},
        ),
        (
            "number",
            "set_value",
            {"entity_id": "number.foxess_force_discharge_power", "value": 5.0},
        ),
        (
            "select",
            "select_option",
            {"entity_id": "select.foxess_work_mode", "option": "Force Discharge"},
        ),
    ]


def test_force_charge_rediscovers_entities_after_foxess_modbus_renames_controls():
    hass = _FakeHass(_base_states())
    controller = FoxESSEntityController(hass, entity_prefix="foxess")

    assert asyncio.run(controller.connect())
    hass.states = _FakeStates(_unprefixed_states())

    assert asyncio.run(controller.force_charge(duration_minutes=30, power_w=5000))

    assert hass.services.calls == [
        (
            "number",
            "set_value",
            {"entity_id": "number.force_charge_power", "value": 5.0},
        ),
        (
            "select",
            "select_option",
            {"entity_id": "select.work_mode", "option": "Force Charge"},
        ),
    ]


def test_force_charge_uses_foxess_modbus_remote_control_when_select_option_is_missing():
    states = _base_states()
    for state in states:
        if state.entity_id == "select.foxess_work_mode":
            state.attributes = {
                "options": ["Self Use", "Feed-in First", "Back-up"],
            }
    registry_ids = [
        SimpleNamespace(entity_id=state.entity_id, device_id="fox-device")
        for state in states
    ]
    hass = _FakeHass(
        states,
        registry_entries={"fox-entry": registry_ids},
        service_names={("foxess_modbus", "write_registers")},
        devices={"fox-device": _h3_smart_device()},
    )
    controller = FoxESSEntityController(hass, foxess_entry_id="fox-entry")
    controller._remote_control_settle_seconds = 0

    assert asyncio.run(controller.connect())
    assert asyncio.run(controller.force_charge(duration_minutes=30, power_w=4200))

    assert hass.services.calls == [
        (
            "number",
            "set_value",
            {"entity_id": "number.foxess_force_charge_power", "value": 4.2},
        ),
        (
            "foxess_modbus",
            "write_registers",
            {"inverter": "fox-device", "start_address": 49203, "values": "3"},
        ),
        (
            "foxess_modbus",
            "write_registers",
            {"inverter": "fox-device", "start_address": 46002, "values": "1800"},
        ),
        (
            "foxess_modbus",
            "write_registers",
            {"inverter": "fox-device", "start_address": 46001, "values": "1"},
        ),
        (
            "foxess_modbus",
            "write_registers",
            {"inverter": "fox-device", "start_address": 46003, "values": "65535,61336"},
        ),
    ]

    hass.services.calls.clear()
    assert asyncio.run(controller.restore_normal())
    assert hass.services.calls == [
        (
            "foxess_modbus",
            "write_registers",
            {"inverter": "fox-device", "start_address": 46001, "values": "0"},
        ),
        (
            "foxess_modbus",
            "write_registers",
            {"inverter": "fox-device", "start_address": 49203, "values": "1"},
        ),
    ]


def test_force_discharge_uses_foxess_modbus_remote_control_when_select_option_is_missing():
    states = _base_states()
    for state in states:
        if state.entity_id == "select.foxess_work_mode":
            state.attributes = {
                "options": ["Self Use", "Feed-in First", "Back-up"],
            }
    registry_ids = [
        SimpleNamespace(entity_id=state.entity_id, device_id="fox-device")
        for state in states
    ]
    hass = _FakeHass(
        states,
        registry_entries={"fox-entry": registry_ids},
        service_names={("foxess_modbus", "write_registers")},
        devices={"fox-device": _h3_smart_device()},
    )
    controller = FoxESSEntityController(hass, foxess_entry_id="fox-entry")
    controller._remote_control_settle_seconds = 0

    assert asyncio.run(controller.connect())
    assert asyncio.run(controller.force_discharge(duration_minutes=15, power_w=4200))

    assert hass.services.calls == [
        (
            "number",
            "set_value",
            {"entity_id": "number.foxess_force_discharge_power", "value": 4.2},
        ),
        (
            "foxess_modbus",
            "write_registers",
            {"inverter": "fox-device", "start_address": 49203, "values": "2"},
        ),
        (
            "foxess_modbus",
            "write_registers",
            {"inverter": "fox-device", "start_address": 46002, "values": "900"},
        ),
        (
            "foxess_modbus",
            "write_registers",
            {"inverter": "fox-device", "start_address": 46001, "values": "1"},
        ),
        (
            "foxess_modbus",
            "write_registers",
            {"inverter": "fox-device", "start_address": 46003, "values": "0,4200"},
        ),
    ]


def test_curtailment_returns_false_when_export_limit_is_missing():
    states = _without_suffix(_base_states(), ("_export_power_limit",))
    hass = _FakeHass(states)
    controller = FoxESSEntityController(hass, entity_prefix="foxess")

    assert asyncio.run(controller.connect())
    assert not asyncio.run(controller.curtail(1500))
    assert hass.services.calls == []


def test_curtailment_uses_foxess_modbus_remote_control_when_export_limit_is_missing():
    states = _without_suffix(_base_states(), ("_export_power_limit",))
    registry_ids = [
        SimpleNamespace(entity_id=state.entity_id, device_id="fox-device")
        for state in states
    ]
    hass = _FakeHass(
        states,
        registry_entries={"fox-entry": registry_ids},
        service_names={("foxess_modbus", "write_registers")},
        devices={"fox-device": _h3_smart_device()},
    )
    controller = FoxESSEntityController(hass, foxess_entry_id="fox-entry")
    controller._remote_control_settle_seconds = 0

    assert asyncio.run(controller.connect())
    assert asyncio.run(controller.curtail(1500))

    assert hass.services.calls == [
        (
            "foxess_modbus",
            "write_registers",
            {"inverter": "fox-device", "start_address": 46001, "values": "9"},
        ),
        (
            "foxess_modbus",
            "write_registers",
            {"inverter": "fox-device", "start_address": 46002, "values": "600"},
        ),
        (
            "foxess_modbus",
            "write_registers",
            {"inverter": "fox-device", "start_address": 46003, "values": "0,1500"},
        ),
    ]


def test_restore_disables_foxess_modbus_remote_control_when_export_limit_is_missing():
    states = _without_suffix(_base_states(), ("_export_power_limit",))
    registry_ids = [SimpleNamespace(entity_id=state.entity_id) for state in states]
    hass = _FakeHass(
        states,
        registry_entries={"fox-entry": registry_ids},
        service_names={("foxess_modbus", "write_registers")},
        config_entry_titles={"fox-entry": "Kitchen FoxESS"},
        config_entry_data={
            "fox-entry": {
                "inverters": [
                    {
                        "friendly_name": "Kitchen FoxESS",
                        "inverter_model": "H3_SMART",
                    }
                ]
            }
        },
    )
    controller = FoxESSEntityController(hass, foxess_entry_id="fox-entry")

    assert asyncio.run(controller.connect())
    assert asyncio.run(controller.restore())

    assert hass.services.calls == [
        (
            "foxess_modbus",
            "write_registers",
            {"inverter": "Kitchen FoxESS", "start_address": 46001, "values": "0"},
        ),
    ]


def test_h3_smart_remote_registers_are_not_used_for_other_models():
    states = _without_suffix(_base_states(), ("_export_power_limit",))
    registry_ids = [
        SimpleNamespace(entity_id=state.entity_id, device_id="fox-device")
        for state in states
    ]
    hass = _FakeHass(
        states,
        registry_entries={"fox-entry": registry_ids},
        service_names={("foxess_modbus", "write_registers")},
        devices={
            "fox-device": SimpleNamespace(
                identifiers={("foxess_modbus", "H3", "AUX", "Kitchen FoxESS")},
                model="H3 - AUX",
            )
        },
    )
    controller = FoxESSEntityController(hass, foxess_entry_id="fox-entry")
    controller._remote_control_settle_seconds = 0

    assert asyncio.run(controller.connect())
    assert not asyncio.run(controller.curtail(1500))
    assert hass.services.calls == []


def test_curtailment_maps_export_limit_name_variant():
    """foxess_modbus H3/KH expose the entity as `export_limit`, not
    `export_power_limit`; discovery should resolve the variant so curtailment
    works instead of silently failing."""
    states = _without_suffix(_base_states(), ("_export_power_limit",))
    states.append(_FakeState("number.foxess_export_limit", "99999", _w()))
    hass = _FakeHass(states)
    controller = FoxESSEntityController(hass, entity_prefix="foxess")

    assert asyncio.run(controller.connect())
    assert asyncio.run(controller.curtail(1500))
    assert hass.services.calls == [
        (
            "number",
            "set_value",
            {"entity_id": "number.foxess_export_limit", "value": 1500},
        ),
    ]


def test_missing_required_entities_raise_actionable_setup_error():
    states = _without_suffix(_base_states(), ("_force_charge_power",))
    hass = _FakeHass(states)
    controller = FoxESSEntityController(hass, entity_prefix="foxess")

    with pytest.raises(ValueError) as exc:
        asyncio.run(controller.connect())

    message = str(exc.value)
    assert message.startswith("foxess_missing_entities:")
    assert "number.foxess_force_charge_power" in message


def test_read_only_reserve_sensor_does_not_satisfy_writable_requirement():
    states = _without_suffix(_base_states(), ("_min_soc_on_grid",))
    states.append(_FakeState("sensor.foxess_min_soc", "20"))
    hass = _FakeHass(states)
    controller = FoxESSEntityController(hass, entity_prefix="foxess")

    with pytest.raises(ValueError) as exc:
        asyncio.run(controller.connect())

    assert "number.foxess_min_soc_on_grid" in str(exc.value)
