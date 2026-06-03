"""Regression coverage for startup load deferral paths."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"
OPTIMIZATION_COORDINATOR_PATH = (
    ROOT / "custom_components" / "power_sync" / "optimization" / "coordinator.py"
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
    polling_loop = _find_class_method(
        tree,
        "OptimizationCoordinator",
        "_schedule_polling_loop",
    )

    enable_source = ast.get_source_segment(source, enable)
    price_update_source = ast.get_source_segment(source, price_update)
    initial_run_source = ast.get_source_segment(source, initial_run)
    polling_loop_source = ast.get_source_segment(source, polling_loop)

    assert "INITIAL_OPTIMIZATION_DELAY_SECONDS = 90.0" in source
    assert enable_source is not None
    assert price_update_source is not None
    assert initial_run_source is not None
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
    assert "self._seconds_until_initial_optimization_allowed()" in initial_run_source
    assert "await asyncio.sleep(delay)" in initial_run_source
    assert "if not self._enabled:" in initial_run_source
    assert "await self._run_optimization()" in initial_run_source
    assert (
        "startup_delay = self._seconds_until_initial_optimization_allowed()"
        in polling_loop_source
    )
    assert "await asyncio.sleep(startup_delay)" in polling_loop_source
    assert "if self._initial_opt_task is not None:" in polling_loop_source


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

    assert "AEMO_SETTLED_SYNC_DELAY_SECONDS = 5.0" in source
    assert "AEMO_STARTUP_SYNC_DELAY_SECONDS = 90.0" in source
    assert setup_source is not None
    assert allowed_source is not None
    assert handler_source is not None
    assert "aemo_dispatch_setup_started = dt_util.utcnow()" in setup_source
    assert "aemo_startup_sync_pending = False" in setup_source
    assert "CONF_AUTO_SYNC_ENABLED" in allowed_source
    assert "nonlocal aemo_startup_sync_pending" in handler_source
    assert "AEMO_SETTLED_SYNC_DELAY_SECONDS" in handler_source
    assert "AEMO_STARTUP_SYNC_DELAY_SECONDS" in handler_source
    assert "aemo_startup_sync_pending = True" in handler_source
    assert "AEMO-dispatch sync already deferred during startup" in handler_source
    assert "entry.entry_id not in hass.data.get(DOMAIN, {})" in handler_source
    assert handler_source.count("_aemo_dispatch_sync_allowed()") == 2
    assert handler_source.index("entry.entry_id not") < handler_source.index(
        "await handle_sync_rest_api_check"
    )
