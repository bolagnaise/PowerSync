"""GoodWe's first plan should follow telemetry recovery without a clock tick."""

from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "custom_components/power_sync/optimization/coordinator.py"


def _methods():
    tree = ast.parse(SOURCE.read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "OptimizationCoordinator")
    names = {"_run_initial_optimization_after_startup_delay", "_run_optimization", "_wait_for_deferred_enable_restore"}
    methods = [node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    for method in methods:
        method.decorator_list = []
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), ast.ClassDef(name="Startup", bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"asyncio": asyncio, "_LOGGER": logging.getLogger(__name__)}
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace["Startup"]


class Energy:
    def __init__(self, ready=False):
        self.ready = ready
        self.listeners = []

    def async_add_listener(self, callback):
        self.listeners.append(callback)
        return lambda: self.listeners.remove(callback)

    def update(self, ready):
        self.ready = ready
        for callback in list(self.listeners):
            callback()


def _coordinator(brand="goodwe", ready=False):
    obj = _methods()()
    obj.hass = SimpleNamespace(is_running=True)
    obj.battery_system = brand
    obj.energy_coordinator = Energy(ready)
    obj._energy_telemetry_ready = lambda: obj.energy_coordinator.ready
    obj._enabled = True
    obj._initial_opt_task = None
    obj._current_schedule = None
    obj._optimization_lock = asyncio.Lock()
    obj._deferred_restore_task = None
    obj._optimizer = object()
    obj.calls = []

    async def solve(**kwargs):
        obj.calls.append((obj._energy_telemetry_ready(), kwargs))
        if obj._energy_telemetry_ready():
            obj._current_schedule = object()
            return True
        return False

    obj._run_optimization = solve
    return obj


async def _flush():
    for _ in range(4):
        await asyncio.sleep(0)


def test_goodwe_first_plan_runs_on_recovery_without_polling_boundary():
    async def run():
        obj = _coordinator()
        obj._initial_opt_task = asyncio.create_task(obj._run_initial_optimization_after_startup_delay())
        task = obj._initial_opt_task
        try:
            await _flush()
            assert obj.calls == [(False, {})]
            assert not task.done()
            obj.energy_coordinator.update(False)
            await _flush()
            assert len(obj.calls) == 1
            obj.energy_coordinator.update(True)
            obj.energy_coordinator.update(True)
            await _flush()
            assert task.done()
            await task
            assert obj._current_schedule is not None
            assert len(obj.calls) == 2
            assert obj.calls[-1] == (True, {"execution_trigger": "startup_recovery"})
            assert obj._initial_opt_task is None
            assert obj.energy_coordinator.listeners == []
            obj.energy_coordinator.update(True)
            assert len(obj.calls) == 2
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize("brand,ready", [("goodwe", True), ("sungrow", False)])
def test_goodwe_recovery_does_not_change_ready_startup_or_other_brands(brand, ready):
    async def run():
        obj = _coordinator(brand, ready)
        await obj._run_initial_optimization_after_startup_delay()
        assert len(obj.calls) == 1
        assert obj.energy_coordinator.listeners == []
    asyncio.run(run())


@pytest.mark.parametrize("end", ["cancel", "disable", "other_plan"])
def test_goodwe_startup_listener_is_removed_when_no_longer_needed(end):
    async def run():
        obj = _coordinator()
        obj._initial_opt_task = asyncio.create_task(obj._run_initial_optimization_after_startup_delay())
        task = obj._initial_opt_task
        try:
            await _flush()
            assert len(obj.energy_coordinator.listeners) == 1
            if end == "cancel":
                task.cancel()  # disable/unload cancels this existing tracked handle.
            else:
                if end == "disable":
                    obj._enabled = False
                else:
                    obj._current_schedule = object()
                obj.energy_coordinator.update(True)
            await asyncio.gather(task, return_exceptions=True)
            assert len(obj.calls) == 1
            assert obj.energy_coordinator.listeners == []
            assert obj._initial_opt_task is None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_goodwe_recovery_between_first_attempt_and_subscription_is_not_lost():
    async def run():
        obj = _coordinator()
        calls = []

        async def solve(**kwargs):
            calls.append(kwargs)
            obj.energy_coordinator.ready = True

        obj._run_optimization = solve
        await obj._run_initial_optimization_after_startup_delay()
        assert calls == [{}, {"execution_trigger": "startup_recovery"}]
        assert obj.energy_coordinator.listeners == []
    asyncio.run(run())


class SolveReached(BaseException):
    """Stop the real optimizer just before forecast/solver work."""


@pytest.mark.parametrize("competing", ["none", "plan", "inflight", "disabled", "stale"])
def test_goodwe_recovery_obeys_restore_order_and_coalesces_competing_solves(competing):
    async def run():
        obj = _coordinator(ready=True)
        del obj._run_optimization  # Exercise the actual production entry guards.
        restored = asyncio.Event()
        obj._deferred_restore_task = asyncio.create_task(restored.wait())
        restore = obj._deferred_restore_task
        obj._battery_specs_source = "configured"
        obj._config = SimpleNamespace(battery_capacity_wh=36000)
        obj._battery_efficiency_topology_fingerprint = lambda _capacity: "goodwe"
        entries = []

        def reached():
            entries.append(True)
            raise SolveReached

        obj._run_optimization.__func__.__globals__["dt_util"] = SimpleNamespace(now=reached)

        async def restart_restore():
            if competing == "plan":
                obj._current_schedule = object()
            elif competing == "inflight":
                await obj._optimization_lock.acquire()
            elif competing == "disabled":
                obj._enabled = False
            elif competing == "stale":
                obj.energy_coordinator.ready = False
            return False

        obj._wait_for_restart_force_restore = restart_restore
        task = asyncio.create_task(obj._run_optimization(execution_trigger="startup_recovery"))
        try:
            await _flush()
            assert not task.done()
            assert entries == []
            restored.set()
            # A completed restore task permits entry, a pending one never does.
            if competing == "none":
                with pytest.raises(SolveReached):
                    await task
                assert entries == [True]
            else:
                assert await task is False
                assert entries == []
        finally:
            task.cancel()
            restored.set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())
