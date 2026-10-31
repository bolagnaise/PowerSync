"""Exercise Fleet power discovery through the real mobile vehicle-list view."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import logging
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import load_power_sync_function, load_source_node


COMPONENT = Path(__file__).resolve().parents[1] / "custom_components/power_sync"
NOW = datetime(2026, 10, 31, 8, 2, tzinfo=timezone.utc)


def _state(entity_id, value, unit=None, *, age=0, reported_age=None, device="car"):
    return SimpleNamespace(
        entity_id=entity_id,
        device_id=device,
        domain=entity_id.split(".")[0],
        state=value,
        attributes={"unit_of_measurement": unit},
        last_updated=NOW - timedelta(seconds=age),
        last_reported=(
            NOW - timedelta(seconds=reported_age) if reported_age is not None else None
        ),
    )


@pytest.fixture
def vehicle_list(monkeypatch):
    """Keep the real GET handler, power helpers and capacity resolver isolated."""
    import sys

    for name in ("power_sync", "power_sync.automations"):
        package = types.ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    for relative_path, name in (
        ("ev_load.py", "power_sync.ev_load"),
        ("automations/ev_vehicle_capacity.py", "power_sync.automations.ev_vehicle_capacity"),
    ):
        spec = importlib.util.spec_from_file_location(name, COMPONENT / relative_path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)

    const = types.ModuleType("power_sync.const")
    const.CONF_GENERIC_CHARGER_BATTERY_CAPACITY_KWH = "generic_charger_battery_capacity_kwh"
    monkeypatch.setitem(sys.modules, const.__name__, const)
    planner = types.ModuleType("power_sync.automations.ev_charging_planner")

    async def discover(hass, entry):
        return hass.discovered

    planner.discover_all_tesla_vehicles = discover
    monkeypatch.setitem(sys.modules, planner.__name__, planner)
    namespace = {
        "__package__": "power_sync",
        "HomeAssistantView": object,
        "datetime": datetime,
        "timezone": timezone,
        "DOMAIN": "power_sync",
        "CONF_EV_PROVIDER": "ev_provider",
        "EV_PROVIDER_FLEET_API": "fleet_api",
        "EV_PROVIDER_TESLA_BLE": "tesla_ble",
        "EV_PROVIDER_BOTH": "both",
        "er": SimpleNamespace(async_get=lambda hass: hass.entity_registry),
        "web": SimpleNamespace(
            json_response=lambda body, status=200: SimpleNamespace(body=body, status=status)
        ),
        "dt_util": SimpleNamespace(now=lambda: NOW, utcnow=lambda: NOW),
        "_LOGGER": logging.getLogger(__name__),
        "_generic_charger_observation_from_config": lambda *args: None,
        "_configured_sigenergy_charger_state": lambda *args: None,
    }
    for name in ("_ev_observed_at", "_ev_power_observed_at"):
        load_power_sync_function(name, namespace)
    view_node = load_source_node(COMPONENT / "__init__.py", "EVVehiclesView")
    exec(
        compile(
            ast.Module(body=[view_node], type_ignores=[]),
            str(COMPONENT / "__init__.py"),
            "exec",
            flags=__import__("__future__").annotations.compiler_flag,
        ),
        namespace,
    )

    def get(states, *, charging_state="charging", device_ids=("car",)):
        states = list(states) + [_state("sensor.car_charging_state", charging_state)]
        entry = SimpleNamespace(data={"ev_provider": "fleet_api"}, options={})
        hass = SimpleNamespace(
            states=SimpleNamespace(get={state.entity_id: state for state in states}.get),
            config_entries=SimpleNamespace(async_entries=lambda domain: [entry]),
            entity_registry=SimpleNamespace(
                entities={state.entity_id: state for state in states}
            ),
            data={"power_sync": {}},
            discovered=[{
                "source": "fleet_api",
                "vin": "5YJTEST0000000001",
                "device": SimpleNamespace(id="car", name="Test Tesla", model="Model 3"),
                "device_ids": device_ids,
            }],
        )
        view = namespace["EVVehiclesView"](hass)
        view._get_byd_vehicles = lambda **kwargs: []
        response = asyncio.run(view.get(None))
        assert response.status == 200, response.body
        assert response.body["success"] is True
        assert len(response.body["vehicles"]) == 1
        return response.body["vehicles"][0]

    return get


@pytest.mark.parametrize("reverse", [False, True])
def test_distance_charge_rate_cannot_replace_measured_kw(vehicle_list, reverse):
    states = [
        _state("sensor.car_charger_power", "1", "kW"),
        _state("sensor.car_charge_rate", "3.5405568", "km/h"),
    ]
    vehicle = vehicle_list(reversed(states) if reverse else states)
    assert vehicle["charger_power"] == 1.0


@pytest.mark.parametrize(
    "value,unit,expected",
    [("1250", "W", 1.25), ("1.25", "kW", 1.25), ("0.00125", "MW", 1.25),
     ("1250", " watts ", 1.25), ("1.25", "kilowatts", 1.25), ("0", "kW", 0.0)],
)
def test_vehicle_list_power_is_normalized_to_kw(vehicle_list, value, unit, expected):
    vehicle = vehicle_list([_state("sensor.car_charger_power", value, unit)])
    assert vehicle["charger_power"] == pytest.approx(expected)


@pytest.mark.parametrize("unit", ["km/h", "mi/h", "mph", "%", "kWh", None, ""])
def test_non_power_units_are_not_presented_as_kw(vehicle_list, unit):
    assert vehicle_list([_state("sensor.car_charge_rate", "3.54", unit)])["charger_power"] is None


@pytest.mark.parametrize("value", ["unknown", "unavailable", "None", "nan", "inf", "-inf", "-1"])
def test_unknown_or_invalid_power_is_unavailable(vehicle_list, value):
    assert vehicle_list([_state("sensor.car_charger_power", value, "kW")])["charger_power"] is None


@pytest.mark.parametrize("reverse", [False, True])
def test_explicit_power_precedes_legacy_rate_in_power_units(vehicle_list, reverse):
    states = [
        _state("sensor.car_charger_power", "1", "kW", age=20),
        _state("sensor.car_charge_rate", "2", "kW"),
    ]
    assert vehicle_list(reversed(states) if reverse else states)["charger_power"] == 1.0


def test_current_legacy_power_rate_is_usable_without_direct_power(vehicle_list):
    assert vehicle_list([_state("sensor.car_charge_rate", "1400", "W")])["charger_power"] == 1.4


@pytest.mark.parametrize("reverse", [False, True])
def test_merged_devices_choose_newest_direct_power_deterministically(vehicle_list, reverse):
    states = [
        _state("sensor.car_charger_power", "7", "kW", age=60),
        _state("sensor.duplicate_charging_power", "1", "kW", age=10, device="duplicate"),
    ]
    assert vehicle_list(
        reversed(states) if reverse else states, device_ids=("car", "duplicate")
    )["charger_power"] == 1.0


@pytest.mark.parametrize("reverse", [False, True])
def test_equal_time_candidates_have_stable_preference_including_zero(vehicle_list, reverse):
    states = [
        _state("sensor.a_charger_power", "7", "kW"),
        _state("sensor.b_charge_power", "0", "kW"),
    ]
    assert vehicle_list(reversed(states) if reverse else states)["charger_power"] == 0.0


@pytest.mark.parametrize("age", [90.001, 3600, -1])
def test_power_requires_its_own_current_timestamp(vehicle_list, age):
    # A fresh charging-state entity cannot refresh an old or future watt value.
    assert vehicle_list([_state("sensor.car_charger_power", "7", "kW", age=age)])["charger_power"] is None


def test_power_at_existing_freshness_boundary_is_available(vehicle_list):
    assert vehicle_list([_state("sensor.car_charger_power", "1", "kW", age=90)])["charger_power"] == 1.0


def test_power_without_a_report_or_update_timestamp_is_unavailable(vehicle_list):
    state = _state("sensor.car_charger_power", "7", "kW")
    state.last_updated = None
    assert vehicle_list([state])["charger_power"] is None


def test_unchanged_power_uses_its_own_report_timestamp(vehicle_list):
    state = _state("sensor.car_charger_power", "1", "kW", age=3600, reported_age=10)
    assert vehicle_list([state])["charger_power"] == 1.0


def test_stale_direct_power_does_not_hide_current_legacy_power(vehicle_list):
    states = [
        _state("sensor.car_charge_rate", "1400", "W"),
        _state("sensor.car_charger_power", "7", "kW", age=100),
    ]
    assert vehicle_list(states)["charger_power"] == 1.4


def test_non_charging_state_still_hides_power(vehicle_list):
    assert vehicle_list(
        [_state("sensor.car_charger_power", "1", "kW")], charging_state="stopped"
    )["charger_power"] is None
