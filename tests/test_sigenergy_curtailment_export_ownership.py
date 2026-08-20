"""Regression coverage for ticket #350: Sigenergy exported while PowerSync's own
zero-export curtailment was active.

Sigenergy DC curtailment (``curtail()``) and an optimizer force discharge
(``force_discharge()``) both write REG_GRID_EXPORT_LIMIT (40038). In the
reported production window PowerSync wrote ``[0, 0]`` at 10:11:00 because
export earnings were 0.56 c/kWh (below its own 1 c/kWh threshold), then wrote
``[0, 1686]`` to the same register at 10:14:43 for a force discharge, and the
site exported at ~0 c/kWh while ``sigenergy_curtailment_state`` still read
``curtailed``. A user "Resume Auto" then wrote the 5 kW safety cap over the
same active curtailment.

These tests pin both halves of the ownership contract:

1. the optimizer's single export-write choke point refuses to raise the export
   ceiling while curtailment is cached as active, and hands an already-armed
   force window back to self-consumption;
2. every non-native restore path reasserts zero export while curtailment is
   cached — not just optimizer-sourced ones.

Uses the AST source-extraction pattern from
tests/test_sungrow_curtailment_runtime.py so the real shipped code is executed
without a Home Assistant runtime.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"
COORDINATOR_PATH = COMPONENT_ROOT / "optimization" / "coordinator.py"
INIT_PATH = COMPONENT_ROOT / "__init__.py"

DOMAIN = "power_sync"
ENTRY_ID = "test_entry"


def _stub_package_namespace() -> dict[str, Any]:
    """Register a stub package so ``from ..const import DOMAIN`` resolves.

    The extracted method is exec'd outside the real package, so relative
    imports need a package context; importing the real one would pull in Home
    Assistant.
    """
    package = sys.modules.setdefault(
        "ps_curtailment_stub", types.ModuleType("ps_curtailment_stub")
    )
    package.__path__ = []  # type: ignore[attr-defined]
    subpackage = sys.modules.setdefault(
        "ps_curtailment_stub.optimization",
        types.ModuleType("ps_curtailment_stub.optimization"),
    )
    subpackage.__path__ = []  # type: ignore[attr-defined]
    const = sys.modules.setdefault(
        "ps_curtailment_stub.const", types.ModuleType("ps_curtailment_stub.const")
    )
    const.DOMAIN = DOMAIN  # type: ignore[attr-defined]
    return {
        "__name__": "ps_curtailment_stub.optimization.coordinator",
        "__package__": "ps_curtailment_stub.optimization",
    }


def _optimization_coordinator_method(name: str):
    """Exec one OptimizationCoordinator method in isolation."""
    source = COORDINATOR_PATH.read_text()
    tree = ast.parse(source)
    class_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "OptimizationCoordinator"
    )
    method = next(
        node
        for node in class_node.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and node.name == name
    )
    module = ast.Module(body=[method], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace: dict[str, Any] = {
        "Any": Any,
        "_LOGGER": logging.getLogger(__name__),
        **_stub_package_namespace(),
    }
    exec(compile(module, str(COORDINATOR_PATH), "exec"), namespace)
    return namespace[name]


def _setup_entry_function(name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(INIT_PATH.read_text())
    setup = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry"
    )
    return next(
        node
        for node in ast.walk(setup)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and node.name == name
    )


def _preserve_export_limit_expression(function_name: str) -> str:
    """Return the shipped ``preserve_export_limit`` gate for one service handler.

    Matches both spellings in __init__.py: the keyword argument passed straight
    to ``restore_normal(...)`` and the local assignment used before the call.
    """
    function = _setup_entry_function(function_name)
    for node in ast.walk(function):
        if isinstance(node, ast.keyword) and node.arg == "preserve_export_limit":
            expression = ast.unparse(node.value)
            if "sigenergy_curtailment_state" in expression:
                return expression
        if isinstance(node, ast.Assign):
            targets = [
                target.id
                for target in node.targets
                if isinstance(target, ast.Name)
            ]
            if "preserve_export_limit" in targets:
                expression = ast.unparse(node.value)
                if "sigenergy_curtailment_state" in expression:
                    return expression
    raise AssertionError(
        f"no sigenergy preserve_export_limit gate found in {function_name}"
    )


def _evaluate_gate(expression: str, *, source: str, curtailed: bool,
                   native_control: bool = False) -> bool:
    entry_data = {
        "sigenergy_curtailment_state": "curtailed" if curtailed else "normal"
    }
    return bool(
        eval(  # noqa: S307 — evaluating the shipped expression is the point
            expression,
            {},
            {
                "source": source,
                "native_control": native_control,
                "entry_data": entry_data,
            },
        )
    )


class _Battery:
    """Records the optimizer's hardware calls without touching Modbus."""

    def __init__(self) -> None:
        self.force_discharge_calls: list[dict[str, Any]] = []
        self.restore_normal_calls = 0

    async def force_discharge(self, **kwargs: Any) -> bool:
        self.force_discharge_calls.append(kwargs)
        return True

    async def restore_normal(self) -> bool:
        self.restore_normal_calls += 1
        return True


def _coordinator(battery_curtailed: bool, force_active: bool = False):
    return SimpleNamespace(
        battery_system="sigenergy",
        _network_export_guard=lambda: None,
        _sigenergy_zero_export_curtailment_active=lambda: battery_curtailed,
        _optimizer_force_state={"active": force_active, "type": "discharge"},
    )


def test_curtailment_helper_reads_the_cached_sigenergy_state():
    helper = _optimization_coordinator_method(
        "_sigenergy_zero_export_curtailment_active"
    )

    for state, expected in (("curtailed", True), ("normal", False), (None, False)):
        entry_data = {} if state is None else {"sigenergy_curtailment_state": state}
        coordinator = SimpleNamespace(
            hass=SimpleNamespace(data={DOMAIN: {ENTRY_ID: entry_data}}),
            entry_id=ENTRY_ID,
        )
        assert helper(coordinator) is expected


def test_optimizer_export_write_is_refused_while_curtailment_is_active():
    """The 10:14:43 production write must not happen while 40038 is held at 0."""
    guard = _optimization_coordinator_method(
        "_force_discharge_through_export_guard"
    )
    battery = _Battery()

    result = asyncio.run(
        guard(
            _coordinator(battery_curtailed=True),
            battery,
            1686.7,
            duration_minutes=11,
        )
    )

    assert result == (False, 0.0)
    assert battery.force_discharge_calls == []
    # Nothing was armed, so nothing needed standing down.
    assert battery.restore_normal_calls == 0


def test_refused_export_stands_down_an_already_armed_force_window():
    """A force window armed before curtailment started is handed back."""
    guard = _optimization_coordinator_method(
        "_force_discharge_through_export_guard"
    )
    battery = _Battery()

    result = asyncio.run(
        guard(
            _coordinator(battery_curtailed=True, force_active=True),
            battery,
            1686.7,
            duration_minutes=11,
            _extend_hardware=True,
        )
    )

    assert result == (False, 0.0)
    assert battery.force_discharge_calls == []
    assert battery.restore_normal_calls == 1


def test_export_still_dispatches_when_curtailment_is_not_active():
    guard = _optimization_coordinator_method(
        "_force_discharge_through_export_guard"
    )
    battery = _Battery()

    result = asyncio.run(
        guard(
            _coordinator(battery_curtailed=False),
            battery,
            1686.7,
            duration_minutes=11,
        )
    )

    assert result == (True, 1686.7)
    assert battery.force_discharge_calls == [
        {"power_w": 1686.7, "duration_minutes": 11}
    ]
    assert battery.restore_normal_calls == 0


@pytest.mark.parametrize("source", ["user", "optimizer", "force_timer", "unknown"])
def test_restore_normal_preserves_zero_export_for_every_non_native_source(source):
    """"Resume Auto" (source=user) must not write the 5 kW cap over curtailment."""
    expression = _preserve_export_limit_expression("handle_restore_normal")

    assert _evaluate_gate(expression, source=source, curtailed=True) is True
    assert _evaluate_gate(expression, source=source, curtailed=False) is False
    # Native/VPP handoff still hands the export cap back to the inverter.
    assert (
        _evaluate_gate(
            expression, source=source, curtailed=True, native_control=True
        )
        is False
    )


@pytest.mark.parametrize("source", ["user", "optimizer", "hold_soc"])
def test_self_consumption_preserves_zero_export_for_every_source(source):
    expression = _preserve_export_limit_expression("handle_set_self_consumption")

    assert _evaluate_gate(expression, source=source, curtailed=True) is True
    assert _evaluate_gate(expression, source=source, curtailed=False) is False


def test_refused_export_leaves_an_active_force_charge_alone():
    """Only a discharge window is stood down; a force charge is the LP's to end."""
    guard = _optimization_coordinator_method(
        "_force_discharge_through_export_guard"
    )
    battery = _Battery()
    coordinator = _coordinator(battery_curtailed=True, force_active=True)
    coordinator._optimizer_force_state["type"] = "charge"

    result = asyncio.run(guard(coordinator, battery, 1686.7, duration_minutes=11))

    assert result == (False, 0.0)
    assert battery.force_discharge_calls == []
    assert battery.restore_normal_calls == 0
