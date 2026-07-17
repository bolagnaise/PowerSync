"""Regression coverage for periodic optimizer cost and quota settlement."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
COORDINATOR_PATH = (
    ROOT / "custom_components" / "power_sync" / "optimization" / "coordinator.py"
)


def _refresh_harness_class():
    source = COORDINATOR_PATH.read_text()
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != "OptimizationCoordinator":
            continue
        for child in node.body:
            if isinstance(child, ast.AsyncFunctionDef) and child.name == "_async_update_data":
                harness = ast.ClassDef(
                    name="RefreshHarness",
                    bases=[],
                    keywords=[],
                    body=[child],
                    decorator_list=[],
                )
                module = ast.fix_missing_locations(
                    ast.Module(body=[harness], type_ignores=[])
                )
                namespace = {"Any": Any}
                exec(compile(module, str(COORDINATOR_PATH), "exec"), namespace)
                return namespace["RefreshHarness"]
    raise AssertionError("OptimizationCoordinator._async_update_data not found")


def test_periodic_refresh_tracks_cost_before_boundary_action():
    """Quota settlement must not wait for a potentially delayed LP solve."""
    coordinator = _refresh_harness_class()()
    events = []

    coordinator._track_actual_cost = lambda: events.append("track")

    async def execute_cached_action():
        events.append("execute")

    coordinator._execute_cached_current_action_if_changed = execute_cached_action
    coordinator.get_api_data = lambda: {"settlement_refreshed": True}

    result = asyncio.run(coordinator._async_update_data())

    assert events == ["track", "execute"]
    assert result == {"settlement_refreshed": True}
