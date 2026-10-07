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


def _load_coordinator_method(name: str):
    tree = ast.parse(COORDINATOR.read_text())
    class_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "OptimizationCoordinator"
    )
    method = next(
        node
        for node in class_node.body
        if isinstance(node, ast.FunctionDef) and node.name == name
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


def test_optimizer_routes_total_discharge_for_adapter_contracts_that_need_it():
    method = _load_guard_method()

    for battery_system, expected_total in (
        ("solax", 3000),
        ("sigenergy", 3000),
        ("sungrow", None),
    ):
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


def test_sigenergy_optimizer_uses_solved_pcc_ceiling_not_battery_export_value():
    method = _load_coordinator_method("_solved_grid_export_power_w")
    action = SimpleNamespace(timestamp="slot-0", power_w=2478.8)
    coordinator = SimpleNamespace(
        _last_optimizer_result=SimpleNamespace(
            schedule=SimpleNamespace(actions=[action]),
            grid_export_w=[9999.98],
        )
    )

    assert method(coordinator, action) == 9999.98


def test_foxess_optimizer_export_uses_solved_whole_site_grid_target():
    """FoxESS grid-mode force discharge must include the solar surplus."""
    method = _load_coordinator_method("_export_command_power_w")
    solved_grid_method = _load_coordinator_method("_solved_grid_export_power_w")
    action = SimpleNamespace(
        timestamp="slot-0",
        power_w=615.3,
        battery_discharge_w=615.3,
    )

    for solved_grid_export_w, expected_command_w in (
        (1792.042, 1792.042),  # sunny site: battery target + solar surplus
        (615.3, 615.3),       # no solar: grid target equals battery export
    ):
        coordinator = SimpleNamespace(
            battery_system="foxess",
            _config=SimpleNamespace(
                max_discharge_w=24_000,
                max_grid_export_w=30_000,
            ),
            _supports_target_export_power=lambda: True,
            _last_optimizer_result=SimpleNamespace(
                schedule=SimpleNamespace(actions=[action]),
                grid_export_w=[solved_grid_export_w],
            ),
        )
        coordinator._solved_grid_export_power_w = (
            lambda candidate, _coordinator=coordinator: solved_grid_method(
                _coordinator,
                candidate,
            )
        )

        assert method(coordinator, action) == expected_command_w


def test_sigenergy_pv_only_export_requests_hardware_refresh_for_battery_target():
    method = _load_coordinator_method("_force_discharge_hardware_needs_refresh")
    coordinator = SimpleNamespace(
        battery_system="sigenergy",
        _get_energy_data=lambda: {
            "work_mode_name": "discharge_pv",
            "battery_power": 0.01,
            "grid_power": -2.48,
        },
    )

    assert method(coordinator, 2478.8) is True


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


def test_sigenergy_optimizer_hardware_paths_forward_battery_target():
    """Ticket #52: neither optimizer hardware path may fall back to PV-first."""
    source = INIT.read_text()
    tree = ast.parse(source)
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "handle_force_discharge"
    )
    hardware_only = next(
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.If)
        and "source" in ast.unparse(node.test)
        and "optimizer" in ast.unparse(node.test)
        and "extend_hardware" in ast.unparse(node.test)
    )
    sigenergy_calls = [
        node
        for node in ast.walk(hardware_only)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "force_discharge"
        and any(keyword.arg == "power_kw" for keyword in node.keywords)
    ]

    assert len(sigenergy_calls) == 2
    assert all(
        any(
            keyword.arg == "battery_discharge_kw"
            and "requested_battery_discharge_w" in ast.unparse(keyword.value)
            for keyword in call.keywords
        )
        for call in sigenergy_calls
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
