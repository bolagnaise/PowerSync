"""Regression test for HD-2: _run_optimization's post-solve override chain
(spread-import, spread-export, bridge, disable-idle, off-grid overlay) must
build the overlaid schedule into a local variable and commit it to
self._current_schedule / result.schedule exactly once, after the whole chain
completes successfully. Reassigning self._current_schedule after every
overlay step (the pre-fix behaviour) means a mid-chain exception leaves
self._current_schedule half-overlaid — visible to any concurrent reader
(_get_current_action, sensors, to_api_response) for up to one cycle.

_run_optimization is a ~700-line async method with deep executor/forecast/EV
dependencies, so this uses the AST source-extraction pattern from
tests/test_sungrow_curtailment_runtime.py to verify the atomicity invariant
structurally on the real source rather than fully stubbing the method for
execution: within each override-chain block, self._current_schedule (and
result.schedule) must be assigned exactly once, and that single assignment
must come after every overlay call in the block.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
COORDINATOR_PATH = (
    ROOT / "custom_components" / "power_sync" / "optimization" / "coordinator.py"
)

# Disable Idle is solver-native. These are the remaining physical overlays,
# in the order _run_optimization applies them before one reconciliation.
_OVERLAY_CALL_MARKERS = (
    "self._spread_import_schedule(",
    "self._spread_export_schedule(",
    "self._bridge_short_export_gaps(",
    "self._apply_offgrid_overlay(",
)


def _run_optimization_source() -> str:
    source = COORDINATOR_PATH.read_text()
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == "OptimizationCoordinator":
            for child in node.body:
                if (
                    isinstance(child, ast.AsyncFunctionDef)
                    and child.name == "_run_optimization"
                ):
                    segment = ast.get_source_segment(source, child)
                    assert segment is not None
                    return segment
    raise AssertionError("_run_optimization not found")


def _chain_blocks(method_source: str) -> list[str]:
    """Split the method source into the two override-chain blocks.

    Each chain starts from the solve-local ``schedule = result.schedule`` and
    ends immediately before the atomic ``_last_optimizer_result`` commit.
    There are two structurally identical chains: the initial solve and the
    optional Auto-Apply reserve rerun.
    """
    start_marker = "schedule = result.schedule"
    starts = [
        m.start()
        for m in re.finditer(
            rf"(?m)^\s*{re.escape(start_marker)}\s*$",
            method_source,
        )
    ]
    assert len(starts) == 2, (
        f"expected exactly 2 solve-local schedule chains (initial + Auto-Apply "
        f"rerun), found {len(starts)} — _run_optimization's structure changed, "
        f"update this test's chain boundaries"
    )
    return [
        method_source[
            start:method_source.index("self._last_optimizer_result = result", start)
        ]
        for start in starts
    ]


def test_override_chain_blocks_are_present_and_ordered():
    """Sanity check the extraction found both remaining overlay chains
    overlays present, in the documented order, before asserting atomicity."""
    method_source = _run_optimization_source()
    chains = _chain_blocks(method_source)
    assert len(chains) == 2

    for chain in chains:
        positions = [chain.index(marker) for marker in _OVERLAY_CALL_MARKERS]
        assert positions == sorted(positions), (
            "override calls are not in the expected spread-import -> "
            "spread-export -> bridge -> offgrid order"
        )


def test_current_schedule_committed_exactly_once_per_chain():
    """HD-2: self._current_schedule must be written exactly once per override
    chain — not after every individual overlay call — so a mid-chain
    exception can never leave it holding a partially-overlaid schedule.
    """
    method_source = _run_optimization_source()
    chains = _chain_blocks(method_source)

    for i, chain in enumerate(chains, start=1):
        assignments = [
            m.start() for m in re.finditer(r"self\._current_schedule\s*=", chain)
        ]
        assert len(assignments) == 1, (
            f"chain {i}: expected exactly one self._current_schedule assignment "
            f"(committed once after the whole override chain completes), found "
            f"{len(assignments)} — a mid-chain exception would leave "
            f"self._current_schedule partially overlaid"
        )

        reconcile = chain.index("self._optimizer.reconcile_result_with_schedule(")
        last_overlay_call = max(chain.index(marker) for marker in _OVERLAY_CALL_MARKERS)
        assert reconcile > last_overlay_call
        assert assignments[0] > reconcile, (
            f"chain {i}: self._current_schedule is assigned before the override "
            f"chain is reconciled"
        )


def test_result_schedule_reconciled_exactly_once_per_chain():
    """The canonical finalizer is the only result/schedule mutation surface."""
    method_source = _run_optimization_source()
    chains = _chain_blocks(method_source)

    for i, chain in enumerate(chains, start=1):
        assert chain.count("self._optimizer.reconcile_result_with_schedule(") == 1
        assert re.search(r"result\.schedule\s*=", chain) is None
        last_overlay_call = max(chain.index(marker) for marker in _OVERLAY_CALL_MARKERS)
        reconcile = chain.index("self._optimizer.reconcile_result_with_schedule(")
        assert reconcile > last_overlay_call


def test_overlay_calls_thread_a_local_variable_not_self_current_schedule():
    """Each overlay call in the chain must read/write a local ``schedule``
    variable, not self._current_schedule directly — otherwise
    self._current_schedule is visible (and mutated) mid-chain to any
    concurrent reader even though the final assignment is deduplicated.
    """
    method_source = _run_optimization_source()
    chains = _chain_blocks(method_source)

    for i, chain in enumerate(chains, start=1):
        for marker in _OVERLAY_CALL_MARKERS:
            call_start = chain.index(marker)
            # The statement's left-hand side is on the same line as the call
            # (``schedule = self._spread_import_schedule(``); walk backward
            # to the start of that line.
            line_start = chain.rfind("\n", 0, call_start) + 1
            lhs = chain[line_start:call_start]
            assert re.search(r"\bschedule\s*=\s*$", lhs), (
                f"chain {i}: overlay call {marker!r} does not assign into a "
                f"local 'schedule' variable (found lhs={lhs!r}) — this reads "
                f"as self._current_schedule being threaded directly through "
                f"the chain again"
            )
            assert "self._current_schedule" not in lhs and "result.schedule" not in lhs
