"""Regression coverage for partial-day Amber metered cost."""

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
COORDINATOR = ROOT / "custom_components" / "power_sync" / "coordinator.py"
SENSOR = ROOT / "custom_components" / "power_sync" / "sensor.py"


def _coordinator_method(name: str, now: datetime):
    tree = ast.parse(COORDINATOR.read_text())
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "AmberUsageCoordinator"
    )
    method = next(
        node
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )
    module = ast.Module(body=[method], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "timedelta": timedelta,
        "DayUsage": object,
        "dt_util": SimpleNamespace(now=lambda: now),
    }
    exec(compile(module, str(COORDINATOR), "exec"), namespace)
    return namespace[name]


def test_today_period_selects_only_current_partial_day():
    now = datetime(2026, 7, 22, 14, 0, tzinfo=timezone.utc)
    get_days = _coordinator_method("_get_days_for_period", now)
    today = object()
    yesterday = object()
    holder = SimpleNamespace(
        _days={"2026-07-22": today, "2026-07-21": yesterday}
    )

    assert get_days(holder, "today") == [today]
    assert get_days(holder, "yesterday") == [yesterday]


def test_today_sensor_rejects_stale_or_missing_usage_instead_of_reporting_zero():
    source = SENSOR.read_text()
    amber = source[source.index("class AmberUsageSensor"):]

    assert 'if self._period == "today" and not coord.is_fresh():' in amber
    assert 'if self._period == "today" and summary.get("days_count", 0) == 0:' in amber
    assert 'attrs["partial_day"] = True' in amber
    assert 'attrs["fresh"] = coord.is_fresh()' in amber


def test_partial_today_can_refresh_even_if_interval_quality_temporarily_drops():
    source = COORDINATOR.read_text()
    process = source[
        source.index("    def _process_intervals"):
        source.index("    def _prune_old_days")
    ]

    assert 'is_partial_today = day_key == dt_util.now().date().isoformat()' in process
    assert "if existing and not is_partial_today:" in process
