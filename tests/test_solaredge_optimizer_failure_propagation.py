from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
COORDINATOR_PATH = (
    ROOT / "custom_components" / "power_sync" / "optimization" / "coordinator.py"
)


def _find_method(tree: ast.AST, class_name: str, method_name: str) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if child.name == method_name:
                        return child
    raise AssertionError(f"{class_name}.{method_name} not found")


def _method_source(source: str, method_name: str) -> str:
    tree = ast.parse(source)
    method = _find_method(tree, "OptimizationCoordinator", method_name)
    method_source = ast.get_source_segment(source, method)
    assert method_source is not None
    return method_source


def test_solaredge_direct_optimizer_controls_skip_busy_mutations():
    source = COORDINATOR_PATH.read_text()
    helper = _method_source(source, "_call_optimizer_energy_control")

    assert 'self.battery_system == "solaredge"' in helper
    assert "return await method(*args, automatic=True)" in helper
    assert "return await method(*args)" in helper


def test_solaredge_idle_records_hold_only_after_confirmed_mode_and_reserve():
    source = COORDINATOR_PATH.read_text()
    idle = _method_source(source, "_set_idle_hold_mode")

    mode_failure = idle.index("backup_mode_result is False")
    reserve_failure = idle.index("reserve_result is False")
    success_marker = idle.index("self._idle_hold_reserve = non_tesla_hold_pct")

    assert mode_failure < reserve_failure < success_marker
    assert idle.count('self.battery_system == "solaredge"') >= 4
    assert 'self._call_optimizer_energy_control(\n                "set_backup_mode"' in idle


def test_solaredge_startup_restore_failure_blocks_the_first_solve():
    source = COORDINATOR_PATH.read_text()
    restore = _method_source(source, "_deferred_enable_restore")

    failed = restore.index("restored is False")
    success_log = restore.index("Optimizer startup: ensured normal operation mode")

    assert failed < success_log
    assert "SolarEdge did not confirm the startup work-mode restore" in restore
    assert 'if self.battery_system == "solaredge":\n                    raise' in restore


def test_solaredge_disable_failure_retains_idle_action_marker():
    source = COORDINATOR_PATH.read_text()
    disable = _method_source(source, "disable")

    assert "idle_work_mode_restore_failed = False" in disable
    assert "idle_work_mode_restore_failed = True" in disable
    assert "if not idle_work_mode_restore_failed:" in disable
    assert "retaining the IDLE action marker" in disable


def test_solaredge_extended_restore_carries_successful_command_generation():
    source = COORDINATOR_PATH.read_text()
    execute = _method_source(source, "_execute_optimizer_action")

    capture = execute.index('getattr(self.energy_coordinator, "generation", None)')
    timer = execute.index("async def _auto_restore_extended")
    service_call = execute.index('"_solaredge_generation": (')

    assert capture < timer < service_call
    assert '"source": "optimizer"' in execute[timer:service_call]
    assert "solaredge_restore_generation" in execute[service_call:]


def test_solaredge_failed_force_commands_do_not_advance_action_or_timer_state():
    source = COORDINATOR_PATH.read_text()
    execute = _method_source(source, "_execute_optimizer_action")

    extension_failure = execute.index(
        '"Optimizer: SolarEdge force-discharge "'
    )
    generation_capture = execute.index(
        'getattr(self.energy_coordinator, "generation", None)'
    )
    main_failure = execute.index(
        '"Optimizer: SolarEdge force-discharge command was "'
    )
    final_marker = execute.rindex("self._last_executed_action = effective_action")

    assert "return" in execute[extension_failure:generation_capture]
    assert "return" in execute[main_failure:final_marker]
    assert (
        'if self.battery_system == "solaredge":\n                                return'
        in execute
    )
