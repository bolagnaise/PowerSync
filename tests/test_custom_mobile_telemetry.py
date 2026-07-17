"""Regression tests for custom external-controller mobile telemetry."""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parent.parent
COORDINATOR_PATH = ROOT / "custom_components" / "power_sync" / "coordinator.py"
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"
SENSOR_PATH = ROOT / "custom_components" / "power_sync" / "sensor.py"


class _UpdateFailed(Exception):
    pass


class _DataUpdateCoordinator:
    def __init__(self, hass: Any, *_args: Any, **_kwargs: Any) -> None:
        self.hass = hass
        self.data = None


class _EnergyAccumulator:
    def __init__(self, _hass: Any, _store_key: str) -> None:
        self._last_update = None
        self.updated_with: tuple[Any, ...] | None = None

    async def async_restore(self) -> None:
        self._last_update = datetime(2026, 7, 17, tzinfo=timezone.utc)

    def update(self, *values: Any) -> None:
        self.updated_with = values

    def as_dict(self) -> dict[str, float]:
        return {"pv_today_kwh": 1.25}


def _custom_coordinator_namespace() -> dict[str, Any]:
    source = COORDINATOR_PATH.read_text()
    tree = ast.parse(source)
    normalize_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "normalize_custom_power_kw"
    )
    class_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "CustomEntityEnergyCoordinator"
    )
    module = ast.Module(body=[normalize_node, class_node], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace: dict[str, Any] = {
        "Any": Any,
        "HomeAssistant": object,
        "DataUpdateCoordinator": _DataUpdateCoordinator,
        "EnergyAccumulator": _EnergyAccumulator,
        "UpdateFailed": _UpdateFailed,
        "DOMAIN": "power_sync",
        "timedelta": timedelta,
        "UPDATE_INTERVAL_ENERGY": timedelta(seconds=15),
        "math": math,
        "dt_util": SimpleNamespace(
            utcnow=lambda: datetime(2026, 7, 17, 7, 30, tzinfo=timezone.utc)
        ),
        "_get_current_prices": lambda _hass, _entry_id: (0.25, 0.08),
        "_LOGGER": SimpleNamespace(
            debug=lambda *_args, **_kwargs: None,
            warning=lambda *_args, **_kwargs: None,
        ),
    }
    exec(compile(module, str(COORDINATOR_PATH), "exec"), namespace)
    return namespace


def _custom_coordinator_class():
    return _custom_coordinator_namespace()["CustomEntityEnergyCoordinator"]


def _state(value: str, unit: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        state=value,
        attributes={"unit_of_measurement": unit},
    )


def _coordinator(states: dict[str, Any]):
    coordinator_class = _custom_coordinator_class()
    hass = SimpleNamespace(
        states=SimpleNamespace(get=lambda entity_id: states.get(entity_id))
    )
    return coordinator_class(
        hass,
        source_entities={
            "battery_level": "sensor.source_soc",
            "battery_power": "sensor.source_battery",
            "grid_power": "sensor.source_grid",
            "solar_power": "sensor.source_solar",
            "load_power": "sensor.source_load",
        },
        entry_id="custom-entry",
    )


def test_custom_entity_coordinator_normalizes_mobile_energy_data():
    coordinator = _coordinator(
        {
            "sensor.source_soc": _state("62.5", "%"),
            "sensor.source_battery": _state("-2400", "W"),
            "sensor.source_grid": _state("-1.1", "kW"),
            "sensor.source_solar": _state("0.004", "MW"),
            "sensor.source_load": _state("1800", "W"),
        }
    )

    data = asyncio.run(coordinator._async_update_data())

    assert data["battery_level"] == pytest.approx(62.5)
    assert data["battery_power"] == pytest.approx(-2.4)
    assert data["grid_power"] == pytest.approx(-1.1)
    assert data["solar_power"] == pytest.approx(4.0)
    assert data["load_power"] == pytest.approx(1.8)
    assert data["energy_summary"] == {"pv_today_kwh": 1.25}
    assert coordinator._energy_acc.updated_with == pytest.approx(
        (4.0, -1.1, -2.4, 1.8, 0.25, 0.08)
    )


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (2400, "W", 2.4),
        (-2.4, "kW", -2.4),
        (0.004, "MW", 4.0),
        (2400, "", 2.4),
        (2.4, "", 2.4),
        (float("nan"), "kW", None),
        (float("inf"), "W", None),
    ],
)
def test_custom_power_normalization_is_finite_and_unit_aware(
    value: float,
    unit: str,
    expected: float | None,
):
    normalize = _custom_coordinator_namespace()["normalize_custom_power_kw"]

    result = normalize(value, unit)

    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


def test_custom_entity_coordinator_retries_initially_unavailable_source():
    coordinator = _coordinator(
        {
            "sensor.source_soc": _state("62.5", "%"),
            "sensor.source_battery": _state("unavailable", "W"),
            "sensor.source_grid": _state("0", "W"),
            "sensor.source_solar": _state("0", "W"),
            "sensor.source_load": _state("0", "W"),
        }
    )

    with pytest.raises(_UpdateFailed, match="sensor.source_battery"):
        asyncio.run(coordinator._async_update_data())


def test_custom_entity_coordinator_marks_post_success_outage_failed():
    states = {
        "sensor.source_soc": _state("62.5", "%"),
        "sensor.source_battery": _state("-2400", "W"),
        "sensor.source_grid": _state("-1.1", "kW"),
        "sensor.source_solar": _state("4", "kW"),
        "sensor.source_load": _state("1800", "W"),
    }
    coordinator = _coordinator(states)
    coordinator.data = asyncio.run(coordinator._async_update_data())
    states["sensor.source_grid"] = _state("unavailable", "kW")

    with pytest.raises(_UpdateFailed, match="sensor.source_grid"):
        asyncio.run(coordinator._async_update_data())


def test_custom_entity_coordinator_rejects_non_finite_source_value():
    coordinator = _coordinator(
        {
            "sensor.source_soc": _state("62.5", "%"),
            "sensor.source_battery": _state("nan", "W"),
            "sensor.source_grid": _state("0", "W"),
            "sensor.source_solar": _state("0", "W"),
            "sensor.source_load": _state("0", "W"),
        }
    )

    with pytest.raises(_UpdateFailed, match="sensor.source_battery"):
        asyncio.run(coordinator._async_update_data())


def test_custom_mobile_bridge_is_wired_to_sensors_and_calendar_history():
    init_source = INIT_PATH.read_text()
    sensor_source = SENSOR_PATH.read_text()

    assert "CustomEntityEnergyCoordinator(" in init_source
    assert '"custom_energy_coordinator": custom_energy_coordinator' in init_source
    assert '"is_custom_battery": is_custom_battery' in init_source
    assert 'custom_energy_coordinator = domain_data.get("custom_energy_coordinator")' in sensor_source
    assert "elif is_custom_battery:\n        energy_coordinator = custom_energy_coordinator" in sensor_source
    assert "description.key == SENSOR_TYPE_GRID_STATUS" in sensor_source
    assert (
        '("custom_energy_coordinator", "Custom external controller")'
        in init_source
    )
