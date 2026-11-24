"""Publish only the final reconciled plan, including reserve and resource reruns.

Structural checks protect both overlay chains. Behavioral checks suspend the
actual orchestration at its await points and verify concurrent readers retain
the preceding committed plan, including when the rerun fails or is cancelled.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import re
import textwrap
from datetime import datetime, timezone
from types import SimpleNamespace
from pathlib import Path

import pytest


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


def _schedule_preparation_source(method_source: str) -> str:
    """Find the local overlay helper shared by the initial solve and rerun."""
    source = textwrap.dedent(method_source)
    tree = ast.parse(source)
    [helper] = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_prepare_schedule"
    ]
    segment = ast.get_source_segment(source, helper)
    assert segment is not None
    return segment


def test_override_chain_blocks_are_present_and_ordered():
    """Both solves must use the shared overlays before external planning."""
    method_source = _run_optimization_source()
    helper = _schedule_preparation_source(method_source)
    solution = ast.parse(textwrap.dedent(method_source))
    calls = [
        node for node in ast.walk(solution)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "_prepare_schedule"
    ]
    assert [ast.unparse(call) for call in sorted(calls, key=lambda call: call.lineno)] == [
        "_prepare_schedule(result, reference_reserve_floor)",
        "_prepare_schedule(result, applied_reserve_floor)",
    ]
    assert method_source.index(
        "result, _ = _prepare_schedule(result, applied_reserve_floor)"
    ) < method_source.index("result = await self._apply_external_energy_plan(")
    positions = [helper.index(marker) for marker in _OVERLAY_CALL_MARKERS]
    assert positions == sorted(positions)


def test_current_schedule_committed_once_after_every_planning_stage():
    method_source = _run_optimization_source()
    helper = _schedule_preparation_source(method_source)
    assert re.search(r"self\._current_schedule\s*=", helper) is None
    assert re.search(r"self\._last_optimizer_result\s*=", helper) is None

    for field in ("_current_schedule", "_last_optimizer_result", "_last_update_time",
                  "_octopus_schedule_revision", "_last_published_optimizer_parameters"):
        assignments = list(re.finditer(rf"self\.{field}\s*=", method_source))
        assert len(assignments) == 1
        assert assignments[0].start() > method_source.index(
            "result = await self._apply_external_energy_plan("
        )


def test_result_schedule_reconciled_exactly_once_per_chain():
    """The canonical finalizer is the only result/schedule mutation surface."""
    helper = _schedule_preparation_source(_run_optimization_source())
    assert helper.count("self._optimizer.reconcile_result_with_schedule(") == 1
    assert re.search(r"result\.schedule\s*=", helper) is None
    last_overlay_call = max(helper.index(marker) for marker in _OVERLAY_CALL_MARKERS)
    reconcile = helper.index("self._optimizer.reconcile_result_with_schedule(")
    assert reconcile > last_overlay_call


def test_overlay_calls_thread_a_local_variable_not_self_current_schedule():
    """Each overlay call in the chain must read/write a local ``schedule``
    variable, not self._current_schedule directly — otherwise
    self._current_schedule is visible (and mutated) mid-chain to any
    concurrent reader even though the final assignment is deduplicated.
    """
    helper = _schedule_preparation_source(_run_optimization_source())
    for marker in _OVERLAY_CALL_MARKERS:
        call_start = helper.index(marker)
        line_start = helper.rfind("\n", 0, call_start) + 1
        lhs = helper[line_start:call_start]
        assert lhs.strip() == "schedule =", f"{marker} assignment target: {lhs!r}"


def test_auto_apply_bridge_uses_one_frozen_reference_across_both_solves():
    """Ticket #286: the applied solve must not redefine the manual bridge."""
    method_source = _run_optimization_source()
    helper = _schedule_preparation_source(method_source)
    capture = helper.index("self._reference_export_bridge_windows(")
    spread = helper.index("self._spread_export_schedule(")
    assert capture < spread
    assert re.search(r"self\._merge_export_protection_floors\(\s*reserve_floor,", helper)
    assert "export_reserve_floor=post_solve_export_floor" in helper
    assert "authoritative_reserve_floor=post_solve_export_floor" in helper
    first_solve = method_source.index("result, reference_export_windows = _prepare_schedule(")
    capture_recommendation = method_source.index("self._set_forecast_bridge_reserve_recommendation(")
    applied_solve = method_source.index("result, _ = _prepare_schedule(")
    restore_recommendation = method_source.index("final_recommendation.update(reference_reserve_semantics)")
    assert first_solve < capture_recommendation < applied_solve < restore_recommendation
    assert method_source.count("self._set_forecast_bridge_reserve_recommendation(") == 1


def test_spread_export_uses_effective_bonus_prices_in_both_solve_chains():
    """Ticket #319: post-solve spreading must retain provider bonus boundaries."""
    method_source = _run_optimization_source()
    helper = _schedule_preparation_source(method_source)

    derivation = method_source.index(
        "spread_export_prices = self._spread_export_prices_for_run("
    )
    assert derivation < method_source.index("schedule = result.schedule")
    assert helper.count("export_prices=spread_export_prices") == 1


def _publication_pipeline(*, rerun=True):
    """Execute the real post-solve orchestration, with only its inputs faked."""
    method = ast.parse(textwrap.dedent(_run_optimization_source())).body[0]
    body = next(node.body for node in method.body if isinstance(node, ast.Try))
    start = next(i for i, node in enumerate(body)
                 if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "used_reference_override"
                         for target in node.targets))
    end = next(i for i, node in enumerate(body[start:], start)
               if isinstance(node, ast.If)
               and isinstance(node.test, ast.Call)
               and isinstance(node.test.func, ast.Attribute)
               and node.test.func.attr == "isEnabledFor")
    function = ast.parse(
        "async def pipeline(self, result, _run_optimizer_once, resolved_parameters, run): pass"
    ).body[0]
    generation_capture = next(node for node in body if isinstance(node, ast.Assign)
                              and any(isinstance(target, ast.Name) and target.id == 'historical_ev_generation'
                                      for target in node.targets))
    function.body = copy.deepcopy([generation_capture, *body[start:end]])
    namespace = dict.fromkeys((
        "import_prices", "export_prices", "spread_import_blocked", "solar_forecast",
        "load_forecast", "battery_export_allowed", "spread_export_prices",
        "cost_neutral_export_prices", "schedule_timestamps", "native_home_load_w",
        "cost_neutral_import_prices", "battery_charge_blocked", "cost_neutral_slots",
        "priority_export_slots",
    ), [])
    namespace.update(solve_reserve_override=0.0 if rerun else None, soc=0.8, reference_reserve_floor=0.0,
                     manual_control_projection=None, octopus_revision=None,
                     cost_neutral_cap=None, cost_neutral_forecast_import_cost=None,
                     cost_neutral_fixed_cost_allowance=None, cost_neutral_plan=None,
                     cost_neutral_status={},
                     solve_timestamp=None,
                     dt_util=SimpleNamespace(now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
                 str(COORDINATOR_PATH), "exec"), namespace)
    return namespace["pipeline"]


@pytest.mark.parametrize(("rerun", "failure_stage"), [
    (True, None), (True, "reserve"), (True, "external"), (True, "cancel"),
    (False, None), (False, "external"), (True, "disabled"), (False, "disabled"),
    (True, "superseded"),
])
def test_slow_reserve_rerun_never_exposes_an_uncommitted_export(rerun, failure_stage):
    """A refresh during either await must keep the previously committed plan."""
    async def run():
        old_schedule = SimpleNamespace(actions=["self_consumption"])
        old_result = SimpleNamespace(schedule=old_schedule)
        old_parameters = object()
        resolved_parameters = object()
        reference = SimpleNamespace(schedule=SimpleNamespace(actions=["export"]),
                                    future_export_protection_floor_slots=[], lp_stats={},
                                    reserve_recommendation={}, feasible=True)
        final = SimpleNamespace(schedule=SimpleNamespace(actions=["self_consumption", "export"]),
                                future_export_protection_floor_slots=[], lp_stats={},
                                reserve_recommendation={}, feasible=True)
        old_time = datetime(2025, 12, 31, tzinfo=timezone.utc)
        adopted = []
        committed_prices = []
        committed_reserves = []
        superseded = False
        subject = SimpleNamespace(
            _enabled=True,
            _current_schedule=old_schedule, _last_optimizer_result=old_result,
            _last_published_optimizer_parameters=old_parameters,
            _last_update_time=old_time, _octopus_schedule_revision="old",
            _config=SimpleNamespace(backup_reserve=0.2), auto_apply_reserve_enabled=True,
            _optimizer=SimpleNamespace(
                reconcile_result_with_schedule=lambda result, schedule, **kw: result,
                update_config=lambda **kwargs: None,
            ),
            _should_spread_import_schedule=lambda: False,
            _should_spread_export_schedule=lambda: False,
            _reference_export_bridge_windows=lambda *args: [],
            _merge_export_protection_floors=lambda *args: [],
            _bridge_short_export_gaps=lambda schedule, *args, **kw: schedule,
            _should_apply_offgrid_overlay=lambda: False,
            _annotate_manual_control_schedule=lambda schedule, *args: schedule,
            _restore_bridged_export_gap_provenance=lambda *args: None,
            _set_forecast_bridge_reserve_recommendation=lambda *args: None,
            _adopt_solved_ev_series=adopted.append,
            _commit_price_forecast_cache=lambda *args: committed_prices.append(args),
            _stage_auto_reserve_recommendation=lambda result: 0.3 if rerun else None,
            _apply_auto_reserve_recommendation=committed_reserves.append,
            _reserve_ratio=lambda value, default: value,
            _last_zerohero_bonus_prices=[], _last_zerohero_bonus_cap_kwh=None,
            _last_zerocharge_bonus_prices=[], _last_zerocharge_bonus_cap_kwh=None,
        )

        def assert_previous_plan():
            assert subject._current_schedule is old_schedule
            assert subject._last_optimizer_result is old_result
            assert subject._last_published_optimizer_parameters is old_parameters
            assert subject._last_update_time is old_time
            assert subject._octopus_schedule_revision == "old"
            assert adopted == []
            assert committed_prices == []
            assert committed_reserves == []

        async def solve_again(reserve):
            assert rerun and reserve == 0.3
            await asyncio.sleep(0)
            assert_previous_plan()
            if failure_stage == "reserve":
                raise RuntimeError("reserve solve failed")
            if failure_stage == "cancel":
                raise asyncio.CancelledError()
            return final

        async def external(result, **kwargs):
            nonlocal superseded
            await asyncio.sleep(0)
            assert_previous_plan()
            if failure_stage == "external":
                raise RuntimeError("external plan failed")
            if failure_stage == "disabled":
                subject._enabled = False
            if failure_stage == "superseded":
                superseded = True
            return result

        def check_current():
            if superseded:
                raise RuntimeError("mode changed")

        work = SimpleNamespace(check_current=check_current)
        subject._apply_external_energy_plan = external
        publish = _publication_pipeline(rerun=rerun)
        if failure_stage == "disabled":
            assert await publish(subject, reference, solve_again, resolved_parameters, work) is False
            assert_previous_plan()
        elif failure_stage:
            error = asyncio.CancelledError if failure_stage == "cancel" else RuntimeError
            with pytest.raises(error):
                await publish(subject, reference, solve_again, resolved_parameters, work)
            assert_previous_plan()
        else:
            await publish(subject, reference, solve_again, resolved_parameters, work)
            expected = final if rerun else reference
            assert subject._current_schedule is expected.schedule
            assert subject._last_optimizer_result is expected
            assert subject._last_published_optimizer_parameters is resolved_parameters
            assert subject._last_update_time != old_time
            assert subject._octopus_schedule_revision is None
            assert adopted == [expected]
            assert len(committed_prices) == 1
            assert committed_reserves == ([reference] if rerun else [])

    asyncio.run(run())
