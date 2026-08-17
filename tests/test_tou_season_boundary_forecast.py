"""Regression coverage for season changes inside the optimizer horizon."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent.parent
COORDINATOR_PATH = ROOT / "custom_components" / "power_sync" / "optimization" / "coordinator.py"
TARIFF_TIME_PATH = ROOT / "custom_components" / "power_sync" / "tariff_time.py"
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"


def _tariff_time_module():
    spec = importlib.util.spec_from_file_location("season_boundary_tariff_time", TARIFF_TIME_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _coordinator(fixed_now: datetime):
    tariff_time = _tariff_time_module()
    source = COORDINATOR_PATH.read_text()
    tree = ast.parse(source)
    coordinator = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "OptimizationCoordinator"
    )
    method = next(
        node
        for node in coordinator.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_generate_tou_price_forecast"
    )
    extracted = ast.ClassDef(
        name="_ExtractedCoordinator",
        bases=[],
        keywords=[],
        body=[method],
        decorator_list=[],
    )
    namespace = {
        "dt_util": SimpleNamespace(now=lambda: fixed_now),
        "period_entries": tariff_time.period_entries,
        "find_matching_tou_period": tariff_time.find_matching_tou_period,
        "tariff_components_for_datetime": tariff_time.tariff_components_for_datetime,
        "_LOGGER": logging.getLogger(__name__),
    }
    module = ast.fix_missing_locations(ast.Module(body=[extracted], type_ignores=[]))
    exec(compile(module, str(COORDINATOR_PATH), "exec"), namespace)
    instance = object.__new__(namespace["_ExtractedCoordinator"])
    instance._config = SimpleNamespace(interval_minutes=30, horizon_hours=48)
    instance._interval_timestamps = lambda start, count, minutes: [
        (start.astimezone(timezone.utc) + timedelta(minutes=minutes * index)).astimezone(start.tzinfo)
        for index in range(count)
    ]
    instance._apply_saving_session_prices = lambda imports, exports: (imports, exports)
    instance._apply_demand_charge_penalty = lambda imports: imports
    return instance


def test_optimizer_uses_new_season_after_month_boundary():
    tz = ZoneInfo("Australia/Sydney")
    coordinator = _coordinator(datetime(2026, 10, 31, 23, 30, tzinfo=tz))
    all_day = {
        "OFF_PEAK": {"periods": [{
            "fromDayOfWeek": 0,
            "toDayOfWeek": 6,
            "fromHour": 0,
            "toHour": 24,
        }]},
    }
    summer = {
        **all_day,
        "PEAK": {"periods": [{
            "fromDayOfWeek": 0,
            "toDayOfWeek": 6,
            "fromHour": 15,
            "toHour": 21,
        }]},
    }
    tariff = {
        "tou_periods": all_day,
        "buy_rates": {"OFF_PEAK": 0.21},
        "sell_rates": {"OFF_PEAK": 0.05},
        "seasons": {
            "Shoulder": {"fromMonth": 9, "toMonth": 10, "tou_periods": all_day},
            "Summer": {"fromMonth": 11, "toMonth": 3, "tou_periods": summer},
        },
        "season_buy_rates": {
            "Shoulder": {"OFF_PEAK": 0.21},
            "Summer": {"OFF_PEAK": 0.21, "PEAK": 0.54},
        },
        "season_sell_rates": {
            "Shoulder": {"OFF_PEAK": 0.05},
            "Summer": {"OFF_PEAK": 0.05, "PEAK": 0.05},
        },
    }

    import_prices, _ = coordinator._generate_tou_price_forecast(tariff)
    target = datetime(2026, 11, 1, 15, 0, tzinfo=tz)
    target_index = coordinator._pending_price_timestamps.index(target)

    assert import_prices[target_index] == 0.54


def test_current_tariff_price_reselects_season_without_reload():
    tariff_time = _tariff_time_module()
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "get_current_price_from_tariff_schedule"
    )
    tz = ZoneInfo("Australia/Sydney")
    namespace = {
        "dt_util": SimpleNamespace(now=lambda: datetime(2026, 11, 1, 15, 0, tzinfo=tz)),
        "_LOGGER": logging.getLogger(__name__),
        "__package__": "power_sync",
    }
    # The extracted function imports this package-relative helper itself.
    import sys
    import types
    package = types.ModuleType("power_sync")
    package.__path__ = [str(ROOT / "custom_components" / "power_sync")]
    previous = sys.modules.get("power_sync")
    previous_tariff_time = sys.modules.get("power_sync.tariff_time")
    sys.modules["power_sync"] = package
    sys.modules["power_sync.tariff_time"] = tariff_time
    try:
        exec(ast.get_source_segment(source, function), namespace)
        get_price = namespace["get_current_price_from_tariff_schedule"]
        all_day = {
            "OFF_PEAK": {"periods": [{
                "fromDayOfWeek": 0,
                "toDayOfWeek": 6,
                "fromHour": 0,
                "toHour": 24,
            }]},
        }
        summer = {
            **all_day,
            "PEAK": {"periods": [{
                "fromDayOfWeek": 0,
                "toDayOfWeek": 6,
                "fromHour": 15,
                "toHour": 21,
            }]},
        }
        result = get_price({
            "tou_periods": all_day,
            "buy_rates": {"OFF_PEAK": 0.21},
            "sell_rates": {"OFF_PEAK": 0.05},
            "seasons": {
                "Shoulder": {"fromMonth": 9, "toMonth": 10, "tou_periods": all_day},
                "Summer": {"fromMonth": 11, "toMonth": 3, "tou_periods": summer},
            },
            "season_buy_rates": {
                "Shoulder": {"OFF_PEAK": 0.21},
                "Summer": {"OFF_PEAK": 0.21, "PEAK": 0.54},
            },
            "season_sell_rates": {
                "Shoulder": {"OFF_PEAK": 0.05},
                "Summer": {"OFF_PEAK": 0.05, "PEAK": 0.05},
            },
        })
    finally:
        if previous is None:
            sys.modules.pop("power_sync", None)
        else:
            sys.modules["power_sync"] = previous
        if previous_tariff_time is None:
            sys.modules.pop("power_sync.tariff_time", None)
        else:
            sys.modules["power_sync.tariff_time"] = previous_tariff_time

    assert result == (54.0, 5.0, "PEAK")


def test_custom_tariff_schedule_retains_rates_for_every_season():
    tariff_time = _tariff_time_module()
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "convert_custom_tariff_to_schedule"
    )
    tz = ZoneInfo("Australia/Sydney")
    namespace = {
        "normalize_currency": lambda value, fallback: value or fallback,
        "DEFAULT_CURRENCY": "AUD",
        "currency_metadata": lambda currency: {"price_unit": f"{currency}/kWh"},
        "dt_util": SimpleNamespace(now=lambda: datetime(2026, 10, 31, 23, 30, tzinfo=tz)),
        "_LOGGER": logging.getLogger(__name__),
        "__package__": "power_sync",
    }
    import sys
    import types
    package = types.ModuleType("power_sync")
    package.__path__ = [str(ROOT / "custom_components" / "power_sync")]
    previous = sys.modules.get("power_sync")
    previous_tariff_time = sys.modules.get("power_sync.tariff_time")
    sys.modules["power_sync"] = package
    sys.modules["power_sync.tariff_time"] = tariff_time
    try:
        exec(ast.get_source_segment(source, function), namespace)
        convert = namespace["convert_custom_tariff_to_schedule"]
        all_day = {
            "OFF_PEAK": {"periods": [{
                "fromDayOfWeek": 0,
                "toDayOfWeek": 6,
                "fromHour": 0,
                "toHour": 24,
            }]},
        }
        summer = {
            **all_day,
            "PEAK": {"periods": [{
                "fromDayOfWeek": 0,
                "toDayOfWeek": 6,
                "fromHour": 15,
                "toHour": 21,
            }]},
        }
        schedule = convert({
            "currency": "AUD",
            "seasons": {
                "Shoulder": {"fromMonth": 9, "toMonth": 10, "tou_periods": all_day},
                "Summer": {"fromMonth": 11, "toMonth": 3, "tou_periods": summer},
            },
            "energy_charges": {
                "Shoulder": {"OFF_PEAK": 0.21},
                "Summer": {"rates": {"OFF_PEAK": 0.21, "PEAK": 0.54}},
            },
            "sell_tariff": {"energy_charges": {
                "Shoulder": {"OFF_PEAK": 0.05},
                "Summer": {"rates": {"OFF_PEAK": 0.05, "PEAK": 0.05}},
            }},
        })
    finally:
        if previous is None:
            sys.modules.pop("power_sync", None)
        else:
            sys.modules["power_sync"] = previous
        if previous_tariff_time is None:
            sys.modules.pop("power_sync.tariff_time", None)
        else:
            sys.modules["power_sync.tariff_time"] = previous_tariff_time

    assert schedule["current_season"] == "Shoulder"
    assert schedule["buy_rates"] == {"OFF_PEAK": 0.21}
    assert schedule["season_buy_rates"]["Summer"]["PEAK"] == 0.54
    assert schedule["season_sell_rates"]["Summer"]["PEAK"] == 0.05
