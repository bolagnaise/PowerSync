"""Regression tests for non-Tesla calendar-history energy rows."""

from __future__ import annotations

import ast
import asyncio
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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
        "_calendar_entry_with_detail_aliases",
        "_calendar_entry_has_energy",
        "_calendar_energy_state_wh",
        "_calendar_entry_from_energy_sensor_states",
        "_merge_calendar_energy_entries",
        "_calendar_current_entry",
        "_calendar_statistic_suffixes",
        "_find_calendar_statistic_entity_ids",
        "_calendar_residual_entry",
        "_calendar_range_includes_today",
        "_calendar_statistics_end_dt",
        "_calendar_history_bucket_timestamp",
        "_calendar_time_series_from_state_history_rows",
        "_calendar_time_series_totals_kwh",
        "_calculate_cost_from_tariff",
        "_find_season_for_month",
        "_weighted_avg_rates",
        "_calendar_result_from_energy_summary",
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
        elif (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "_CALENDAR_STATISTIC_FIELD_ALIASES"
                for target in node.targets
            )
        ):
            body.append(node)
        elif (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in wanted_functions
        ):
            body.append(node)

    module = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(module)

    namespace: dict[str, Any] = {
        "Any": Any,
        "datetime": datetime,
        "DOMAIN": "power_sync",
        "HomeAssistant": object,
        "dt_util": SimpleNamespace(
            now=lambda: datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc),
            as_local=lambda value: value,
        ),
        "_LOGGER": SimpleNamespace(
            info=lambda *args, **kwargs: None,
            debug=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
        ),
    }
    exec(compile(module, str(INIT_PATH), "exec"), namespace)
    return namespace


class _States:
    def __init__(self, states: dict[str, Any]) -> None:
        self._states = states

    def get(self, entity_id: str) -> Any:
        return self._states.get(entity_id)

    def async_all(self, domain: str) -> list[Any]:
        return []


def _state(value: str, unit: str = "kWh") -> SimpleNamespace:
    return SimpleNamespace(state=value, attributes={"unit_of_measurement": unit})


def _history_state(
    value: str,
    timestamp: datetime,
    unit: str = "kWh",
) -> SimpleNamespace:
    return SimpleNamespace(
        state=value,
        attributes={"unit_of_measurement": unit},
        last_changed=timestamp,
        last_updated=timestamp,
    )


@contextmanager
def _fake_entity_registry(entities: dict[str, Any]):
    helpers = SimpleNamespace()
    entity_registry = SimpleNamespace(
        async_get=lambda hass: SimpleNamespace(entities=entities)
    )
    helpers.entity_registry = entity_registry
    previous_homeassistant = sys.modules.get("homeassistant")
    previous_helpers = sys.modules.get("homeassistant.helpers")
    previous_registry = sys.modules.get("homeassistant.helpers.entity_registry")
    sys.modules["homeassistant"] = SimpleNamespace(helpers=helpers)
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.entity_registry"] = entity_registry
    try:
        yield
    finally:
        for module_name, previous in (
            ("homeassistant", previous_homeassistant),
            ("homeassistant.helpers", previous_helpers),
            ("homeassistant.helpers.entity_registry", previous_registry),
        ):
            if previous is None:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = previous


def test_calendar_range_includes_today_when_end_is_now_snapshot():
    namespace = _calendar_namespace()
    first_now = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)
    later_now = first_now + timedelta(microseconds=1)
    today_start = datetime(2026, 5, 16, tzinfo=timezone.utc)
    yesterday_start = today_start - timedelta(days=1)

    assert namespace["_calendar_range_includes_today"](
        today_start,
        first_now,
        later_now,
    )
    assert not namespace["_calendar_range_includes_today"](
        yesterday_start,
        today_start,
        later_now,
    )


def test_day_period_statistics_include_today_until_request_end():
    namespace = _calendar_namespace()
    now = datetime(2026, 5, 16, 18, 30, tzinfo=timezone.utc)
    end_dt = now

    assert namespace["_calendar_statistics_end_dt"](
        "day",
        end_dt,
        now,
        True,
    ) == end_dt


def test_month_period_statistics_exclude_today_to_avoid_live_duplicate():
    namespace = _calendar_namespace()
    now = datetime(2026, 5, 16, 18, 30, tzinfo=timezone.utc)
    end_dt = now

    assert namespace["_calendar_statistics_end_dt"](
        "month",
        end_dt,
        now,
        True,
    ) == datetime(2026, 5, 16, tzinfo=timezone.utc)


def test_calendar_residual_entry_subtracts_existing_hourly_rows():
    namespace = _calendar_namespace()
    current_entry = {
        "timestamp": "2026-05-16T18:30:00+00:00",
        "solar_generation": 10000,
        "battery_discharge": 4000,
        "battery_charge": 6000,
        "grid_import": 12000,
        "grid_export": 3000,
        "home_consumption": 15000,
    }
    existing_rows = [
        {
            "timestamp": "2026-05-16T08:00:00+00:00",
            "solar_generation": 2500,
            "battery_discharge": 500,
            "battery_charge": 1000,
            "grid_import": 2000,
            "grid_export": 0,
            "home_consumption": 3000,
        },
        {
            "timestamp": "2026-05-16T09:00:00+00:00",
            "solar_generation": 3500,
            "battery_discharge": 1500,
            "battery_charge": 2000,
            "grid_import": 4000,
            "grid_export": 500,
            "home_consumption": 5000,
        },
    ]

    residual = namespace["_calendar_residual_entry"](current_entry, existing_rows)

    assert residual["solar_generation"] == 4000
    assert residual["battery_discharge"] == 2000
    assert residual["battery_charge"] == 3000
    assert residual["grid_import"] == 6000
    assert residual["grid_export"] == 2500
    assert residual["home_consumption"] == 7000
    assert residual["solar_energy_exported"] == 4000
    assert residual["consumer_energy_imported"] == 7000


def test_calendar_state_history_rows_convert_daily_totals_to_hourly_deltas():
    namespace = _calendar_namespace()
    start = datetime(2026, 5, 16, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)
    history = {
        "sensor.power_sync_daily_solar_energy": [
            _history_state("2.0", datetime(2026, 5, 16, 9, 15, tzinfo=timezone.utc)),
            _history_state("4.5", datetime(2026, 5, 16, 10, 45, tzinfo=timezone.utc)),
        ],
        "sensor.power_sync_daily_grid_import": [
            _history_state("1.2", datetime(2026, 5, 16, 10, 10, tzinfo=timezone.utc)),
            _history_state("1.7", datetime(2026, 5, 16, 11, 5, tzinfo=timezone.utc)),
        ],
    }
    entity_to_field = {
        "sensor.power_sync_daily_solar_energy": "solar_generation",
        "sensor.power_sync_daily_grid_import": "grid_import",
    }

    rows = namespace["_calendar_time_series_from_state_history_rows"](
        history,
        entity_to_field,
        "day",
        start,
        end,
    )

    assert rows == [
        {
            "timestamp": "2026-05-16T09:00:00+00:00",
            "solar_generation": 2000,
            "battery_discharge": 0,
            "battery_charge": 0,
            "grid_import": 0,
            "grid_export": 0,
            "home_consumption": 0,
            "solar_energy_exported": 2000,
            "battery_energy_exported": 0,
            "battery_energy_imported": 0,
            "consumer_energy_imported": 0,
            "grid_energy_imported": 0,
            "grid_energy_exported": 0,
        },
        {
            "timestamp": "2026-05-16T10:00:00+00:00",
            "solar_generation": 2500,
            "battery_discharge": 0,
            "battery_charge": 0,
            "grid_import": 1200,
            "grid_export": 0,
            "home_consumption": 0,
            "solar_energy_exported": 2500,
            "battery_energy_exported": 0,
            "battery_energy_imported": 0,
            "consumer_energy_imported": 0,
            "grid_energy_imported": 1200,
            "grid_energy_exported": 0,
        },
        {
            "timestamp": "2026-05-16T11:00:00+00:00",
            "solar_generation": 0,
            "battery_discharge": 0,
            "battery_charge": 0,
            "grid_import": 500,
            "grid_export": 0,
            "home_consumption": 0,
            "solar_energy_exported": 0,
            "battery_energy_exported": 0,
            "battery_energy_imported": 0,
            "consumer_energy_imported": 0,
            "grid_energy_imported": 500,
            "grid_energy_exported": 0,
        },
    ]


def test_calendar_state_history_rows_ignore_same_day_transient_drop():
    namespace = _calendar_namespace()
    start = datetime(2026, 5, 16, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)
    history = {
        "sensor.power_sync_daily_battery_charge_foxess": [
            _history_state("47.0", datetime(2026, 5, 16, 9, 0, tzinfo=timezone.utc)),
            _history_state("0", datetime(2026, 5, 16, 9, 5, tzinfo=timezone.utc)),
            _history_state("47.6", datetime(2026, 5, 16, 9, 10, tzinfo=timezone.utc)),
            _history_state("48.1", datetime(2026, 5, 16, 10, 0, tzinfo=timezone.utc)),
        ],
    }
    entity_to_field = {
        "sensor.power_sync_daily_battery_charge_foxess": "battery_charge",
    }

    rows = namespace["_calendar_time_series_from_state_history_rows"](
        history,
        entity_to_field,
        "day",
        start,
        end,
    )

    assert sum(row["battery_charge"] for row in rows) == 48100
    assert rows[0]["battery_charge"] == 47600
    assert rows[1]["battery_charge"] == 500


def test_calendar_state_history_rows_allow_next_day_reset():
    namespace = _calendar_namespace()
    start = datetime(2026, 5, 16, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 5, 18, 0, 0, tzinfo=timezone.utc)
    history = {
        "sensor.power_sync_daily_solar_energy": [
            _history_state("8.0", datetime(2026, 5, 16, 23, 0, tzinfo=timezone.utc)),
            _history_state("0.5", datetime(2026, 5, 17, 7, 0, tzinfo=timezone.utc)),
            _history_state("3.0", datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)),
        ],
    }
    entity_to_field = {
        "sensor.power_sync_daily_solar_energy": "solar_generation",
    }

    rows = namespace["_calendar_time_series_from_state_history_rows"](
        history,
        entity_to_field,
        "week",
        start,
        end,
    )

    assert [row["solar_generation"] for row in rows] == [8000, 3000]


def test_calendar_statistic_finder_accepts_foxess_daily_battery_aliases():
    namespace = _calendar_namespace()
    entities = {
        "sensor.power_sync_daily_battery_charge_foxess": SimpleNamespace(
            domain="sensor",
            platform="power_sync",
            unique_id="entry-1_daily_battery_charge_foxess",
            entity_id="sensor.power_sync_daily_battery_charge_foxess",
        ),
        "sensor.power_sync_daily_battery_discharge_foxess": SimpleNamespace(
            domain="sensor",
            platform="power_sync",
            unique_id="entry-1_daily_battery_discharge_foxess",
            entity_id="sensor.power_sync_daily_battery_discharge_foxess",
        ),
    }
    hass = SimpleNamespace(
        states=_States(
            {
                "sensor.power_sync_daily_battery_charge_foxess": _state("47.5"),
                "sensor.power_sync_daily_battery_discharge_foxess": _state("42.7"),
            }
        )
    )

    with _fake_entity_registry(entities):
        entity_ids = namespace["_find_calendar_statistic_entity_ids"](
            hass, "entry-1"
        )

    assert entity_ids["battery_charge"] == "sensor.power_sync_daily_battery_charge_foxess"
    assert (
        entity_ids["battery_discharge"]
        == "sensor.power_sync_daily_battery_discharge_foxess"
    )


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
    assert entry["solar_energy_exported"] == 4200
    assert entry["grid_energy_exported"] == 750
    assert entry["grid_energy_imported"] == 1500
    assert entry["consumer_energy_imported"] == 5600


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


def test_current_calendar_entry_exposes_aggregate_tesla_style_aliases_only():
    namespace = _calendar_namespace()
    namespace["_find_calendar_statistic_entity_ids"] = lambda hass, entry_id: {}
    hass = SimpleNamespace(states=_States({}))
    coordinator = SimpleNamespace(
        data={
            "energy_summary": {
                "pv_today_kwh": 2.5,
                "discharge_today_kwh": 1.2,
                "charge_today_kwh": 3.4,
                "grid_import_today_kwh": 0.8,
                "grid_export_today_kwh": 0.6,
                "load_today_kwh": 2.1,
            }
        }
    )

    entry = namespace["_calendar_current_entry"](hass, coordinator, "entry-1")

    assert entry["solar_generation"] == 2500
    assert entry["battery_discharge"] == 1200
    assert entry["battery_charge"] == 3400
    assert entry["grid_import"] == 800
    assert entry["grid_export"] == 600
    assert entry["home_consumption"] == 2100
    assert entry["solar_energy_exported"] == 2500
    assert entry["battery_energy_exported"] == 1200
    assert entry["battery_energy_imported"] == 3400
    assert entry["grid_energy_imported"] == 800
    assert entry["grid_energy_exported"] == 600
    assert entry["consumer_energy_imported"] == 2100
    assert "battery_energy_imported_from_grid" not in entry
    assert "battery_energy_imported_from_solar" not in entry
    assert "consumer_energy_imported_from_grid" not in entry
    assert "consumer_energy_imported_from_solar" not in entry
    assert "consumer_energy_imported_from_battery" not in entry
    assert "grid_energy_exported_from_solar" not in entry
    assert "grid_energy_exported_from_battery" not in entry


def test_current_calendar_entry_does_not_invent_solar_or_battery_export_splits():
    namespace = _calendar_namespace()
    namespace["_find_calendar_statistic_entity_ids"] = lambda hass, entry_id: {}
    hass = SimpleNamespace(states=_States({}))
    coordinator = SimpleNamespace(
        data={
            "energy_summary": {
                "pv_today_kwh": 10.27,
                "discharge_today_kwh": 46.5,
                "charge_today_kwh": 52.1,
                "grid_import_today_kwh": 0,
                "grid_export_today_kwh": 33.73,
                "load_today_kwh": 0,
            }
        }
    )

    entry = namespace["_calendar_current_entry"](hass, coordinator, "entry-1")

    assert entry["solar_generation"] == 10270
    assert entry["grid_export"] == 33730
    assert entry["solar_energy_exported"] == 10270
    assert entry["grid_energy_exported"] == 33730
    assert "grid_energy_exported_from_solar" not in entry
    assert "grid_energy_exported_from_battery" not in entry


def test_energy_summary_period_costs_ignore_daily_recorder_reset_artifacts():
    namespace = _calendar_namespace()

    async def fake_statistics(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "timestamp": "2026-07-01T00:00:00+10:00",
                "grid_import": 100_000,
                "grid_export": 20_000,
                "home_consumption": 90_000,
                "solar_generation": 10_000,
                "battery_discharge": 5_000,
                "battery_charge": 6_000,
            },
            {
                "timestamp": "2026-07-02T00:00:00+10:00",
                "grid_import": 50_000,
                "grid_export": 5_000,
                "home_consumption": 45_000,
                "solar_generation": 8_000,
                "battery_discharge": 4_000,
                "battery_charge": 3_000,
            },
        ]

    async def reset_skewed_costs(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "import_cost": 1.62,
            "export_earnings": 0.88,
            "net_cost": 0.74,
            "estimated": False,
        }

    namespace["_calendar_time_series_from_statistics"] = fake_statistics
    namespace["_calculate_cost_from_statistics"] = reset_skewed_costs

    tariff_schedule = {
        "buy_rates": {"ALL": 0.40},
        "sell_rates": {"ALL": 0.10},
        "seasons": {},
        "tou_periods": {},
    }

    result = namespace["_calendar_result_from_energy_summary"](
        SimpleNamespace(),
        "month",
        None,
        SimpleNamespace(),
        "entry-1",
        tariff_schedule,
        "Sigenergy",
    )
    result = asyncio.run(result)

    assert result["cost_summary"]["estimated"] is True
    assert result["cost_summary"]["import_cost"] == 60.0
    assert result["cost_summary"]["export_earnings"] == 2.5
    assert result["cost_summary"]["net_cost"] == 57.5
