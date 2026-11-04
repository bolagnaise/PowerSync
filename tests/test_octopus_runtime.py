"""Real optimizer listener/execution methods under mid-slot source revisions."""
from __future__ import annotations

import ast
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS


def optimizer_class():
    path = Path(__file__).parents[1] / "custom_components/power_sync/optimization/coordinator.py"
    tree = ast.parse(path.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "OptimizationCoordinator")
    selected = {"_is_octopus_dynamic_tariff", "_price_update_generation_key", "_on_price_update",
                "_run_octopus_price_updates", "_execute_optimizer_action", "_monitoring_mode_active", "_setup_price_listener"}
    cls.body = [n for n in cls.body if getattr(n, "name", None) in selected]
    cls.bases = []
    for n in cls.body:
        n.decorator_list = []
    env = {"datetime": datetime, "_LOGGER": logging.getLogger(__name__), "dt_util": NS(utcnow=lambda: datetime.now(timezone.utc))}
    exec(compile("from __future__ import annotations\n" + ast.unparse(cls), str(path), "exec"), env)
    return env["OptimizationCoordinator"]


def source(revision="one"):
    cls = type("OctopusPriceCoordinator", (), {})
    obj = cls()
    obj.data = {"revision": revision}
    obj.last_update_success = True
    return obj


def test_intelligent_go_replans_revised_prices_while_solve_is_in_flight():
    async def scenario():
        obj = optimizer_class()()
        obj._enabled = obj._is_dynamic_pricing = True
        obj.price_coordinator = source()
        obj._seconds_until_initial_optimization_allowed = lambda: 0
        obj._get_active_force_state = lambda: {}
        obj.hass = NS(async_create_background_task=lambda task, name: asyncio.create_task(task, name=name))
        entered, release = asyncio.Event(), asyncio.Event()
        solves = []

        async def run(**kwargs):
            solves.append((obj.price_coordinator.data["revision"], kwargs))
            if len(solves) == 1:
                entered.set()
                await release.wait()

        obj._run_optimization = run
        assert obj._is_octopus_dynamic_tariff()
        obj._on_price_update()
        await entered.wait()
        first_task = obj._price_reoptimize_task
        obj.price_coordinator.data["revision"] = "two"
        obj._on_price_update()
        obj._on_price_update()  # Duplicate snapshot must not add a solve.
        assert obj._price_reoptimize_task is first_task
        release.set()
        await first_task
        assert [r for r, _ in solves] == ["one", "two"]
        assert all(k["force"] for _, k in solves)  # Queue behind the existing lock.
        obj._enabled = False
        obj.price_coordinator.data["revision"] = "three"
        obj._on_price_update()
        assert len(solves) == 2

    asyncio.run(scenario())


def test_superseded_or_unavailable_octopus_plan_cannot_issue_a_command():
    async def scenario():
        obj = optimizer_class()()
        obj._enabled = True
        obj.price_coordinator = source("new")
        obj._octopus_schedule_revision = "old"
        # There is deliberately no executor: reaching a command boundary fails.
        await obj._execute_optimizer_action(NS(action="charge"))
        obj._octopus_schedule_revision = "new"
        obj.price_coordinator.last_update_success = False
        await obj._execute_optimizer_action(NS(action="charge"))

    asyncio.run(scenario())


def test_intelligent_flux_is_monitoring_even_without_manual_monitoring_setting():
    obj = optimizer_class()()
    obj.price_coordinator = source()
    obj.price_coordinator.data["octopus"] = {"external_battery_control": True}
    assert obj._monitoring_mode_active() is True


def test_intelligent_go_registers_actual_optimizer_price_listener():
    async def scenario():
        obj = optimizer_class()()
        obj.price_coordinator = source()
        obj.price_coordinator.product_code = "INTELLI-VAR-24-10-29"
        obj.price_coordinator.tariff_code = "E-1R-INTELLI-VAR-24-10-29-C"
        registered = []
        def add_listener(callback):
            registered.append(callback)
            return lambda: registered.remove(callback)
        obj.price_coordinator.async_add_listener = add_listener
        obj._prefers_static_tou_pricing = lambda: False
        obj._price_listener_unsub = None
        obj._octopus_gate_listener_unsub = None
        await obj._setup_price_listener()
        assert obj._is_dynamic_pricing is True
        assert registered == [obj._on_price_update]
    asyncio.run(scenario())


def test_expired_price_blocks_a_matching_cached_plan_at_command_boundary():
    async def scenario():
        obj = optimizer_class()()
        obj._enabled = True
        obj.price_coordinator = source("new")
        obj._octopus_schedule_revision = "new"
        obj.price_coordinator.data["current"] = [{
            "channelType": "general", "valid_from": "2020-01-01T00:00:00Z", "valid_to": "2020-01-01T00:30:00Z",
        }]
        await obj._execute_optimizer_action(NS(action="charge"))
    asyncio.run(scenario())
