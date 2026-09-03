"""Regression coverage for Flow Power daily energy settlement routing."""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


COORDINATOR_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "optimization"
    / "coordinator.py"
)


def _settle_flow_power_measurements():
    tree = ast.parse(COORDINATOR_PATH.read_text())
    coordinator = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "OptimizationCoordinator"
    )
    method = next(
        node
        for node in coordinator.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_settle_flow_power_measurements"
    )
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            ),
            method,
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    namespace: dict[str, object] = {}
    exec(compile(module, str(COORDINATOR_PATH), "exec"), namespace)
    return namespace["_settle_flow_power_measurements"]


class _Ledger:
    rules = (SimpleNamespace(direction="export"),)

    def __init__(self) -> None:
        self.daily_total_calls = []

    def observe_daily_total(self, direction, total_kwh, observed_at):
        self.daily_total_calls.append((direction, total_kwh, observed_at))
        return 0.0

    def observe_power(self, *_args):
        raise AssertionError("daily energy totals must not use integrated-power fallback")


class _Coordinator:
    def __init__(self, ledger) -> None:
        self.ledger = ledger
        self.saved = False

    def _ensure_flow_power_ledger(self, *, now):
        return SimpleNamespace(), self.ledger

    def _get_energy_data(self):
        return {"energy_summary": {"grid_export_today_kwh": 5.0}}

    def _energy_summary_total_kwh(self, data, direction):
        return data["energy_summary"].get(f"grid_{direction}_today_kwh")

    def _schedule_cost_save(self):
        self.saved = True


def test_flow_power_routes_daily_energy_totals_to_reset_aware_ledger():
    ledger = _Ledger()
    coordinator = _Coordinator(ledger)
    now = datetime(2026, 9, 3, tzinfo=timezone.utc)

    settled = _settle_flow_power_measurements()(coordinator, now, 0.0, 1.0)

    assert settled == {"import": 0.0, "export": 0.0}
    assert ledger.daily_total_calls == [("export", 5.0, now)]
    assert coordinator.saved
