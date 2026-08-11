"""Regression coverage for startup load deferral paths."""

from __future__ import annotations

import ast
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"
OPTIMIZATION_COORDINATOR_PATH = (
    ROOT / "custom_components" / "power_sync" / "optimization" / "coordinator.py"
)
ENERGY_COORDINATOR_PATH = (
    ROOT / "custom_components" / "power_sync" / "coordinator.py"
)


def _find_class_method(
    tree: ast.AST,
    class_name: str,
    method_name: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for child in node.body:
            if (
                isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name == method_name
            ):
                return child
    raise AssertionError(f"{class_name}.{method_name} not found")


def _find_setup_child(
    tree: ast.AST,
    child_name: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or node.name != "async_setup_entry":
            continue
        for child in node.body:
            if (
                isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name == child_name
            ):
                return child
    raise AssertionError(f"async_setup_entry.{child_name} not found")


def _find_nested_setup_child(
    setup: ast.AsyncFunctionDef,
    child_name: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(setup):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == child_name
        ):
            return node
    raise AssertionError(f"async_setup_entry.{child_name} not found")


def _find_module_function(
    tree: ast.AST,
    name: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _walk_excluding_nested_functions(nodes: list[ast.AST]):
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            yield from _walk_excluding_nested_functions([child])


def _is_check_aemo_spike_await(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "check_aemo_spike_for_vpp"
    )


def test_mobile_config_endpoint_registered_before_slow_refreshes():
    """The HA app probe must be available before startup network warmups."""
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    setup = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry"
    )
    helper = _find_module_function(tree, "_register_mobile_detection_views")

    helper_source = ast.get_source_segment(source, helper)
    assert helper_source is not None
    assert "ConfigView(hass)" in helper_source
    assert "ConfigViewLegacy(hass, config_view)" in helper_source

    helper_calls = [
        node
        for node in ast.walk(setup)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_register_mobile_detection_views"
        )
    ]
    first_refresh_calls = [
        node
        for node in ast.walk(setup)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "async_config_entry_first_refresh"
        )
    ]

    assert len(helper_calls) == 1
    assert first_refresh_calls
    assert helper_calls[0].lineno < min(call.lineno for call in first_refresh_calls)
    assert "longer than the app's HA request timeout" in source


def test_initial_optimizer_pass_is_deferred_after_enable():
    source = OPTIMIZATION_COORDINATOR_PATH.read_text()
    tree = ast.parse(source)
    enable = _find_class_method(tree, "OptimizationCoordinator", "enable")
    price_update = _find_class_method(tree, "OptimizationCoordinator", "_on_price_update")
    initial_run = _find_class_method(
        tree,
        "OptimizationCoordinator",
        "_run_initial_optimization_after_startup_delay",
    )
    run_optimization = _find_class_method(
        tree,
        "OptimizationCoordinator",
        "_run_optimization",
    )
    deferred_restore = _find_class_method(
        tree,
        "OptimizationCoordinator",
        "_deferred_enable_restore",
    )
    polling_loop = _find_class_method(
        tree,
        "OptimizationCoordinator",
        "_schedule_polling_loop",
    )

    enable_source = ast.get_source_segment(source, enable)
    price_update_source = ast.get_source_segment(source, price_update)
    initial_run_source = ast.get_source_segment(source, initial_run)
    run_optimization_source = ast.get_source_segment(source, run_optimization)
    deferred_restore_source = ast.get_source_segment(source, deferred_restore)
    polling_loop_source = ast.get_source_segment(source, polling_loop)

    assert "INITIAL_OPTIMIZATION_DELAY_SECONDS = 90.0" in source
    assert enable_source is not None
    assert price_update_source is not None
    assert initial_run_source is not None
    assert run_optimization_source is not None
    assert deferred_restore_source is not None
    assert polling_loop_source is not None
    assert "self._initial_optimization_not_before" in enable_source
    assert (
        "startup_delay = self._seconds_until_initial_optimization_allowed()"
        in price_update_source
    )
    assert "during startup" in price_update_source
    assert (
        "self._last_price_triggered_optimization = dt_util.utcnow()"
        in price_update_source
    )
    assert "self._run_initial_optimization_after_startup_delay()" in enable_source
    assert "powersync_initial_optimization" in enable_source
    # The first solve now waits on HA's real started signal (bounded by the
    # legacy window as a cap), not a flat sleep.
    assert "self.hass.is_running" in initial_run_source
    assert "EVENT_HOMEASSISTANT_STARTED" in initial_run_source
    assert "async_listen_once" in initial_run_source
    assert "INITIAL_OPTIMIZATION_DELAY_SECONDS" in initial_run_source
    assert "if not self._enabled:" in initial_run_source
    assert "await self._run_optimization()" in initial_run_source
    assert (
        "startup_delay = self._seconds_until_initial_optimization_allowed()"
        in polling_loop_source
    )
    assert "await asyncio.sleep(startup_delay)" in polling_loop_source
    assert "not initial_task.done()" in polling_loop_source
    assert "self._initial_opt_task = None" in initial_run_source
    assert "self._energy_telemetry_ready()" in run_optimization_source
    assert "self._energy_telemetry_ready()" in deferred_restore_source


def test_optimizer_solve_waits_for_deferred_startup_restore():
    source = OPTIMIZATION_COORDINATOR_PATH.read_text()
    tree = ast.parse(source)
    run_optimization = _find_class_method(
        tree,
        "OptimizationCoordinator",
        "_run_optimization",
    )
    wait_for_restore = _find_class_method(
        tree,
        "OptimizationCoordinator",
        "_wait_for_deferred_enable_restore",
    )
    run_source = ast.get_source_segment(source, run_optimization)
    wait_source = ast.get_source_segment(source, wait_for_restore)

    assert run_source is not None
    assert wait_source is not None
    assert "if not await self._wait_for_deferred_enable_restore():" in run_source
    assert run_source.index("_wait_for_deferred_enable_restore") < run_source.index(
        "await self._optimization_lock.acquire()"
    )
    assert 'getattr(self, "_deferred_restore_task", None)' in wait_source
    assert "await restore_task" in wait_source
    assert "restore_task is asyncio.current_task()" in wait_source


def test_native_energy_coordinators_propagate_startup_telemetry_readiness():
    source = ENERGY_COORDINATOR_PATH.read_text()
    tree = ast.parse(source)
    coordinator_classes = (
        "FoxESSEntityEnergyCoordinator",
        "GoodWeEnergyCoordinator",
        "SolaxBatteryEnergyCoordinator",
        "SolarEdgeEnergyCoordinator",
        "SajH2EnergyCoordinator",
        "FroniusReservaEnergyCoordinator",
        "NeovoltEnergyCoordinator",
        "AnkerSolixEnergyCoordinator",
        "ESYSunhomeEnergyCoordinator",
    )

    for class_name in coordinator_classes:
        class_node = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == class_name
        )
        assert any(
            isinstance(base, ast.Name)
            and base.id == "NativeBatteryIntegrationReadinessMixin"
            for base in class_node.bases
        ), class_name
        update = _find_class_method(tree, class_name, "_async_update_data")
        update_source = ast.get_source_segment(source, update)
        assert update_source is not None
        assert "telemetry_ready" in update_source, class_name
        assert "if telemetry_ready:" in update_source, class_name


def test_native_readiness_mixin_requires_snapshot_and_live_entities():
    source = ENERGY_COORDINATOR_PATH.read_text()
    tree = ast.parse(source)
    class_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "NativeBatteryIntegrationReadinessMixin"
    )
    module = ast.fix_missing_locations(
        ast.Module(body=[class_node], type_ignores=[])
    )
    warnings: list[str] = []
    namespace = {
        "Any": Any,
        "_LOGGER": SimpleNamespace(
            warning=lambda message, *args: warnings.append(
                message % args if args else message
            )
        ),
    }
    exec(compile(module, str(ENERGY_COORDINATOR_PATH), "exec"), namespace)
    mixin = namespace["NativeBatteryIntegrationReadinessMixin"]()
    live = SimpleNamespace(ready=False)
    mixin._controller = SimpleNamespace(
        telemetry_ready=lambda: live.ready
    )
    mixin.data = {"telemetry_ready": False}

    assert mixin.startup_control_ready() is False
    assert mixin._native_control_allowed("test write") is False

    mixin.data = {"telemetry_ready": True}
    assert mixin.startup_control_ready() is False

    live.ready = True
    assert mixin.startup_control_ready() is True
    assert mixin._native_control_allowed("test write") is True
    assert mixin._native_stale_data()["telemetry_ready"] is False

    mixin._native_integration_enabled = lambda: False
    mixin.data = None
    live.ready = False
    assert mixin.startup_control_ready() is True
    assert mixin._native_control_allowed("direct write") is True
    assert len(warnings) == 1


def test_esy_native_readiness_rejects_non_finite_entity_values():
    source = ENERGY_COORDINATOR_PATH.read_text()
    tree = ast.parse(source)
    state_float = _find_class_method(
        tree,
        "ESYSunhomeEnergyCoordinator",
        "_state_float",
    )
    module = ast.fix_missing_locations(
        ast.Module(body=[state_float], type_ignores=[])
    )
    namespace: dict[str, Any] = {"math": math}
    exec(compile(module, str(ENERGY_COORDINATOR_PATH), "exec"), namespace)

    state = SimpleNamespace(state="nan")
    coordinator = SimpleNamespace(
        _entity_map={"batterySoc": "sensor.esy_soc"},
        hass=SimpleNamespace(
            states=SimpleNamespace(get=lambda _entity_id: state)
        ),
    )

    assert namespace["_state_float"](coordinator, "batterySoc") is None
    state.state = "inf"
    assert namespace["_state_float"](coordinator, "batterySoc") is None
    state.state = "0"
    assert namespace["_state_float"](coordinator, "batterySoc") == 0.0


def test_esy_native_readiness_rediscovers_late_startup_entities():
    source = ENERGY_COORDINATOR_PATH.read_text()
    tree = ast.parse(source)
    readiness = _find_class_method(
        tree,
        "ESYSunhomeEnergyCoordinator",
        "_native_live_telemetry_ready",
    )
    module = ast.fix_missing_locations(
        ast.Module(body=[readiness], type_ignores=[])
    )
    namespace: dict[str, Any] = {}
    exec(compile(module, str(ENERGY_COORDINATOR_PATH), "exec"), namespace)

    coordinator = SimpleNamespace(
        _entity_map={"batterySoc": "sensor.esy_soc"},
    )
    discovery_calls = 0

    def _discover_entities() -> None:
        nonlocal discovery_calls
        discovery_calls += 1
        coordinator._entity_map.update(
            {
                "pvPower": "sensor.esy_pv",
                "gridPower": "sensor.esy_grid",
                "loadPower": "sensor.esy_load",
                "batteryPower": "sensor.esy_battery",
            }
        )

    coordinator._discover_entities = _discover_entities
    coordinator._state_float = (
        lambda key: 0.0 if key in coordinator._entity_map else None
    )

    assert namespace["_native_live_telemetry_ready"](coordinator) is True
    assert discovery_calls == 1


def test_native_startup_restore_waits_instead_of_using_a_fixed_delay():
    optimizer_source = OPTIMIZATION_COORDINATOR_PATH.read_text()
    optimizer_tree = ast.parse(optimizer_source)
    readiness = ast.get_source_segment(
        optimizer_source,
        _find_class_method(
            optimizer_tree,
            "OptimizationCoordinator",
            "_energy_telemetry_ready",
        ),
    )
    deferred = ast.get_source_segment(
        optimizer_source,
        _find_class_method(
            optimizer_tree,
            "OptimizationCoordinator",
            "_deferred_enable_restore",
        ),
    )
    assert readiness is not None
    assert deferred is not None
    assert '"startup_control_ready"' in readiness
    assert "while self._enabled and not self._energy_telemetry_ready()" in deferred
    assert "min(30, 5 * attempt)" in deferred
    assert "self._energy_uses_native_battery_integration()" in deferred

    init_source = INIT_PATH.read_text()
    assert '"battery_energy_coordinator": energy_coord_for_demand' in init_source
    assert "def _uses_native_battery_integration" in init_source


def test_initial_vpp_aemo_spike_check_is_backgrounded():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    setup = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry"
    )
    initial_check = _find_nested_setup_child(setup, "_run_initial_aemo_spike_check")

    setup_source = ast.get_source_segment(source, setup)
    initial_check_source = ast.get_source_segment(source, initial_check)

    assert setup_source is not None
    assert initial_check_source is not None
    assert "await check_aemo_spike_for_vpp(None)" in initial_check_source
    assert "except asyncio.CancelledError" in initial_check_source
    assert "hass.async_create_background_task" in setup_source
    assert "powersync_vpp_aemo_initial_check" in setup_source
    assert "vpp_aemo_initial_task" in setup_source
    direct_setup_awaits = [
        node for node in _walk_excluding_nested_functions(setup.body)
        if _is_check_aemo_spike_await(node)
    ]
    assert direct_setup_awaits == []


def test_aemo_dispatch_sync_debounces_during_startup():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    setup = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry"
    )
    allowed = _find_setup_child(tree, "_aemo_dispatch_sync_allowed")
    handler = _find_setup_child(tree, "_handle_aemo_dispatch_event")

    setup_source = ast.get_source_segment(source, setup)
    allowed_source = ast.get_source_segment(source, allowed)
    handler_source = ast.get_source_segment(source, handler)

    runner = _find_setup_child(tree, "_run_aemo_dispatch_sync")
    runner_source = ast.get_source_segment(source, runner)
    sync_after_started = _find_nested_setup_child(setup, "_sync_after_started")
    sync_after_started_source = ast.get_source_segment(source, sync_after_started)

    assert "AEMO_SETTLED_SYNC_DELAY_SECONDS = 5.0" in source
    # The fixed 90s startup window is gone — the deferral now tracks HA's real
    # startup-complete signal instead of an arbitrary constant.
    assert "AEMO_STARTUP_SYNC_DELAY_SECONDS" not in source
    assert setup_source is not None
    assert allowed_source is not None
    assert handler_source is not None
    assert runner_source is not None
    assert sync_after_started_source is not None
    assert isinstance(sync_after_started, ast.FunctionDef)
    assert any(
        isinstance(decorator, ast.Name) and decorator.id == "callback"
        for decorator in sync_after_started.decorator_list
    )
    assert (
        "_schedule_aemo_dispatch_task(_run_aemo_dispatch_sync)"
        in sync_after_started_source
    )
    assert "aemo_startup_sync_pending = False" in setup_source
    assert "CONF_AUTO_SYNC_ENABLED" in allowed_source
    assert "nonlocal aemo_startup_sync_pending" in handler_source
    # During startup the handler waits on the started event, not a timed sleep.
    assert "hass.is_running" in handler_source
    assert "EVENT_HOMEASSISTANT_STARTED" in handler_source
    assert "async_listen_once" in handler_source
    assert "aemo_startup_sync_pending = True" in handler_source
    assert "AEMO-dispatch sync already deferred until HA start completes" in handler_source
    # The settled-price delay + entry-still-loaded guard live in the runner that
    # both the normal and deferred paths funnel through.
    assert "AEMO_SETTLED_SYNC_DELAY_SECONDS" in runner_source
    assert "entry.entry_id not in hass.data.get(DOMAIN, {})" in runner_source
    assert runner_source.index("entry.entry_id not") < runner_source.index(
        "await handle_sync_rest_api_check"
    )


def test_aemo_dispatch_tasks_are_generation_scoped_and_joined_on_unload():
    """Reloads must fence old dispatch callbacks before entry data is removed."""
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    setup = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry"
    )
    unload = _find_module_function(tree, "async_unload_entry")
    done_callback = _find_nested_setup_child(setup, "_on_aemo_dispatch_task_done")
    sigenergy_sync = _find_setup_child(tree, "_sync_tariff_to_sigenergy")
    foxess_sync = _find_setup_child(tree, "_sync_tariff_to_foxess")
    demand_helper = _find_setup_child(
        tree, "_enforce_demand_grid_charging_protection"
    )
    tou_sync = _find_setup_child(tree, "_handle_sync_tou_internal")
    setup_source = ast.get_source_segment(source, setup)
    unload_source = ast.get_source_segment(source, unload)
    done_callback_source = ast.get_source_segment(source, done_callback)
    sigenergy_sync_source = ast.get_source_segment(source, sigenergy_sync)
    foxess_sync_source = ast.get_source_segment(source, foxess_sync)
    demand_helper_source = ast.get_source_segment(source, demand_helper)
    tou_sync_source = ast.get_source_segment(source, tou_sync)

    assert setup_source is not None
    assert unload_source is not None
    assert done_callback_source is not None
    assert sigenergy_sync_source is not None
    assert foxess_sync_source is not None
    assert demand_helper_source is not None
    assert tou_sync_source is not None
    assert "aemo_dispatch_generation" in setup_source
    assert "aemo_dispatch_tasks" in setup_source
    assert "aemo_dispatch_started_unsub" in setup_source
    assert "_aemo_dispatch_entry_data" in setup_source
    assert "completed_task.cancelled()" in done_callback_source
    assert "completed_task.result()" in done_callback_source
    assert "except asyncio.CancelledError" in done_callback_source
    assert "_LOGGER.exception(\"AEMO dispatch task failed\")" in done_callback_source
    assert "aemo_dispatch_tasks.discard(completed_task)" in done_callback_source
    assert "aemo_dispatch_tasks.add(task)" in setup_source
    assert "_aemo_dispatch_entry_data()" in sigenergy_sync_source
    assert 'entry_data["sigenergy_tariff"]' in sigenergy_sync_source
    assert "_aemo_dispatch_entry_data()" in foxess_sync_source
    assert "Skipping FoxESS schedule write for stale AEMO dispatch" in foxess_sync_source
    assert "current_entry_data = _aemo_dispatch_entry_data()" in demand_helper_source
    assert "entry_data=current_entry_data" in demand_helper_source
    assert '"grid_charging_disabled_for_demand"' in demand_helper_source
    assert demand_helper_source.index("await ts_coordinator.set_grid_charging_enabled") < demand_helper_source.rindex(
        "current_entry_data = _aemo_dispatch_entry_data()"
    )
    assert "_aemo_dispatch_entry_data() is not _toggle_entry_data" in tou_sync_source
    assert '_toggle_entry_data.get(' in tou_sync_source
    assert '"last_force_toggle_time"' in tou_sync_source
    assert '"retoggle_attempted", False' in tou_sync_source
    assert "task.cancel()" in unload_source
    assert "await asyncio.gather" in unload_source
    assert unload_source.index('"aemo_dispatch_stopping"') < unload_source.index(
        '"aemo_dispatch_unsub"'
    )
    assert unload_source.index("task.cancel()") < unload_source.index(
        "await asyncio.gather"
    )
