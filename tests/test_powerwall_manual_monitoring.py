"""The advanced manual controls share the established manual monitoring policy."""
import ast
from pathlib import Path
from types import SimpleNamespace as NS

import pytest


def guards():
    source = Path(__file__).resolve().parents[1] / "custom_components/power_sync/__init__.py"
    names = {"_battery_output_allowed", "_native_limits_allowed", "_backup_preparation_allowed"}
    nodes = [node for node in ast.walk(ast.parse(source.read_text()))
             if isinstance(node, ast.FunctionDef) and node.name in names]

    class InjectImports(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            return ast.copy_location(ast.Pass(), node)

    runtime = {}
    entry = NS(data={}, options={})
    profile = NS(monitoring_only=False)
    ns = dict(entry=entry, _local_powerwall_runtime=runtime, active_battery_system="tesla",
              BATTERY_SYSTEM_TESLA="tesla", CONF_MONITORING_MODE="monitoring_mode", _control_site_id="site",
              local_battery_dispatch_configured=lambda _: True,
              _local_powerwall_generation_current=lambda: True,
              resolve_connection_profile=lambda *_: profile,
              _is_monitoring_mode=lambda: entry.options.get("monitoring_mode", False),
              foreign_site_owner=lambda *_: None, hass=NS(),
              force_charge_state={}, force_discharge_state={}, hold_soc_state={}, self_consumption_state={})
    entry.entry_id = "entry"
    tree = ast.fix_missing_locations(InjectImports().visit(ast.Module(body=nodes, type_ignores=[])))
    exec(compile(tree, str(source), "exec"), ns)
    return ns, entry, runtime, profile


@pytest.mark.parametrize("monitoring", [False, True])
def test_manual_controls_are_available_independently_of_optimizer_monitoring(monitoring):
    ns, entry, runtime, _ = guards()
    entry.options["monitoring_mode"] = monitoring
    assert ns["_battery_output_allowed"]()
    assert ns["_native_limits_allowed"](False)
    assert ns["_backup_preparation_allowed"]()
    assert ns["_battery_output_allowed"](automatic=True) is (not monitoring)


@pytest.mark.parametrize("key", ["_monitoring_handoff_active", "powerwall_local_dispatch_stopping", "powerwall_settings_pending",
    "powerwall_settings_recovering", "tesla_control_method_changing", "native_max_startup_pending",
    "powerwall_local_recovery_pending", "max_backup_end_ts", "max_backup_recovery_unavailable"])
def test_manual_monitoring_exception_preserves_recovery_fences(key):
    ns, entry, runtime, _ = guards()
    entry.options["monitoring_mode"] = True
    runtime[key] = True
    assert not ns["_battery_output_allowed"]()
    assert not ns["_native_limits_allowed"](False)
    assert not ns["_backup_preparation_allowed"]()


def test_manual_monitoring_exception_preserves_active_owner_and_unsupported_profile():
    ns, entry, runtime, profile = guards()
    entry.options["monitoring_mode"] = True
    ns["force_charge_state"]["active"] = True
    assert not ns["_native_limits_allowed"](False)
    assert not ns["_backup_preparation_allowed"]()
    ns["force_charge_state"].clear()
    profile.monitoring_only = True
    assert not ns["_battery_output_allowed"]()
    assert not ns["_native_limits_allowed"](False)
