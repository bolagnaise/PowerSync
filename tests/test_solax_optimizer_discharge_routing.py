"""Focused optimizer routing regression for SolaX manual export control."""

from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
COORDINATOR = (
    ROOT / "custom_components" / "power_sync" / "optimization" / "coordinator.py"
)
INIT = ROOT / "custom_components" / "power_sync" / "__init__.py"


def _load_guard_method():
    tree = ast.parse(COORDINATOR.read_text())
    class_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "OptimizationCoordinator"
    )
    method = next(
        node
        for node in class_node.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_force_discharge_through_export_guard"
    )
    module = ast.Module(body=[method], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"Any": Any, "_LOGGER": logging.getLogger(__name__)}
    exec(compile(module, str(COORDINATOR), "exec"), namespace)
    return namespace[method.name]


def _guard_call_keywords() -> list[set[str]]:
    """Return keywords supplied by each optimizer export dispatch call site."""
    tree = ast.parse(COORDINATOR.read_text())
    return [
        {keyword.arg for keyword in node.keywords if keyword.arg is not None}
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_force_discharge_through_export_guard"
    ]


def _nested_function_source(path: Path, name: str) -> str:
    source = path.read_text()
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    )
    segment = ast.get_source_segment(source, function)
    assert segment is not None
    return segment


class _Battery:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def force_discharge(self, **kwargs: Any) -> bool:
        self.calls.append(kwargs)
        return True


def test_optimizer_routes_total_discharge_only_for_solax():
    method = _load_guard_method()

    for battery_system, expected_total in (("solax", 3000), ("sungrow", None)):
        battery = _Battery()
        coordinator = SimpleNamespace(
            battery_system=battery_system,
            _network_export_guard=lambda: None,
            _sigenergy_zero_export_curtailment_active=lambda: False,
        )
        result = asyncio.run(
            method(
                coordinator,
                battery,
                1000,
                total_battery_discharge_w=3000,
                duration_minutes=30,
            )
        )

        assert result == (True, 1000)
        assert battery.calls[0]["power_w"] == 1000
        if expected_total is None:
            assert "battery_discharge_w" not in battery.calls[0]
        else:
            assert battery.calls[0]["battery_discharge_w"] == expected_total


def test_solax_total_tracks_a_network_clamped_export_target():
    method = _load_guard_method()
    battery = _Battery()

    class _Guard:
        async def async_guard_write(self, requested_w, writer):
            assert requested_w == 1000
            return await writer(500)

    coordinator = SimpleNamespace(
        battery_system="solax",
        _network_export_guard=lambda: _Guard(),
        _sigenergy_zero_export_curtailment_active=lambda: False,
    )
    result = asyncio.run(
        method(
            coordinator,
            battery,
            1000,
            total_battery_discharge_w=3000,
            duration_minutes=30,
        )
    )

    assert result == (True, 500)
    assert battery.calls == [
        {
            "power_w": 500,
            "duration_minutes": 30,
            "battery_discharge_w": 2500,
        }
    ]


def test_initial_and_extension_dispatch_both_preserve_solax_total():
    """A hardware refresh must carry the same total-power contract as dispatch."""
    call_keywords = _guard_call_keywords()

    assert len(call_keywords) == 2
    assert all(
        "total_battery_discharge_w" in keywords
        for keywords in call_keywords
    )


def test_repeated_solax_dispatch_keeps_total_nonzero():
    method = _load_guard_method()
    battery = _Battery()
    coordinator = SimpleNamespace(
        battery_system="solax",
        _network_export_guard=lambda: None,
        _sigenergy_zero_export_curtailment_active=lambda: False,
    )

    for extend in (False, True):
        result = asyncio.run(
            method(
                coordinator,
                battery,
                1000,
                total_battery_discharge_w=3000,
                duration_minutes=30,
                _extend_hardware=extend,
            )
        )
        assert result == (True, 1000)

    assert [call["battery_discharge_w"] for call in battery.calls] == [3000, 3000]
    assert battery.calls[1]["_extend_hardware"] is True


def test_service_persists_total_after_initial_and_extension_writes():
    handler = _nested_function_source(INIT, "handle_force_discharge")
    persist = _nested_function_source(INIT, "persist_force_mode_state")

    assert 'if source == "optimizer"\n            else 0' in handler
    assert 'call.data.get("battery_discharge_w", 0)' in handler
    assert 'force_discharge_state["battery_discharge_w"] = (' in handler
    assert "solax_home_discharge_w + command_power_w" in handler
    assert 'force_discharge_state["battery_discharge_w"] = (' in handler
    assert "total_discharge_w or 0" in handler
    assert 'force_discharge_state.get("battery_discharge_w", 0)' in persist


def test_persisted_optimizer_force_is_cleaned_up_not_replayed():
    restore = _nested_function_source(INIT, "restore_force_mode_from_persistence")
    cleanup_message = "Ignoring persisted optimizer force"
    message_start = restore.index(cleanup_message)
    cleanup_start = restore.rfind(
        'if persisted_source == "optimizer":',
        0,
        message_start,
    )
    replay_start = restore.index('elif mode == "discharge":', cleanup_start)
    cleanup_branch = restore[cleanup_start:replay_start]

    assert cleanup_message in cleanup_branch
    assert "letting the LP recalculate" in cleanup_branch
    assert "return" in cleanup_branch
    assert "SERVICE_FORCE_DISCHARGE" not in cleanup_branch
