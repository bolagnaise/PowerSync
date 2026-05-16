"""Regression tests for non-Tesla calendar-history energy rows."""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any


INIT_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "__init__.py"
)


def _calendar_namespace() -> dict[str, Any]:
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    wanted_functions = {
        "_energy_summary_wh",
        "_calendar_entry_from_energy_summary",
        "_calendar_entry_has_energy",
        "_calendar_energy_state_wh",
        "_calendar_entry_from_energy_sensor_states",
        "_merge_calendar_energy_entries",
        "_calendar_current_entry",
    }
    body: list[ast.stmt] = []
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "_CALENDAR_STATISTIC_FIELDS"
                for target in node.targets
            )
        ):
            body.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in wanted_functions:
            body.append(node)

    module = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(module)

    namespace: dict[str, Any] = {
        "Any": Any,
        "HomeAssistant": object,
        "dt_util": SimpleNamespace(
            now=lambda: datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)
        ),
    }
    exec(compile(module, str(INIT_PATH), "exec"), namespace)
    return namespace


class _States:
    def __init__(self, states: dict[str, Any]) -> None:
        self._states = states

    def get(self, entity_id: str) -> Any:
        return self._states.get(entity_id)


def _state(value: str, unit: str = "kWh") -> SimpleNamespace:
    return SimpleNamespace(state=value, attributes={"unit_of_measurement": unit})


def test_current_calendar_entry_uses_live_daily_sensor_states_when_accumulator_is_zero():
    namespace = _calendar_namespace()
    entity_ids = {
        "solar_generation": "sensor.power_sync_daily_solar_energy",
        "grid_import": "sensor.power_sync_daily_grid_import",
        "grid_export": "sensor.power_sync_daily_grid_export",
        "home_consumption": "sensor.power_sync_daily_load",
    }
    namespace["_find_calendar_statistic_entity_ids"] = lambda hass, entry_id: entity_ids
    hass = SimpleNamespace(
        states=_States(
            {
                "sensor.power_sync_daily_solar_energy": _state("4.2"),
                "sensor.power_sync_daily_grid_import": _state("1.5"),
                "sensor.power_sync_daily_grid_export": _state("0.75"),
                "sensor.power_sync_daily_load": _state("5.6"),
            }
        )
    )
    coordinator = SimpleNamespace(data={"energy_summary": {}})

    entry = namespace["_calendar_current_entry"](hass, coordinator, "entry-1")

    assert entry["solar_generation"] == 4200
    assert entry["grid_import"] == 1500
    assert entry["grid_export"] == 750
    assert entry["home_consumption"] == 5600


def test_current_calendar_entry_keeps_coordinator_values_and_fills_only_missing_fields():
    namespace = _calendar_namespace()
    entity_ids = {
        "solar_generation": "sensor.power_sync_daily_solar_energy",
        "grid_import": "sensor.power_sync_daily_grid_import",
    }
    namespace["_find_calendar_statistic_entity_ids"] = lambda hass, entry_id: entity_ids
    hass = SimpleNamespace(
        states=_States(
            {
                "sensor.power_sync_daily_solar_energy": _state("9.9"),
                "sensor.power_sync_daily_grid_import": _state("1800", "Wh"),
            }
        )
    )
    coordinator = SimpleNamespace(
        data={
            "energy_summary": {
                "pv_today_kwh": 1.25,
                "grid_import_today_kwh": 0,
            }
        }
    )

    entry = namespace["_calendar_current_entry"](hass, coordinator, "entry-1")

    assert entry["solar_generation"] == 1250
    assert entry["grid_import"] == 1800
