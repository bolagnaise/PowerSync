"""The advanced manual controls share the established manual monitoring policy."""
import ast
from pathlib import Path
from types import SimpleNamespace as NS

import pytest


def guards():
    source = Path(__file__).resolve().parents[1] / "custom_components/power_sync/__init__.py"
    names = {"_battery_output_allowed", "_native_limits_allowed", "_native_limits_block_reason", "_backup_preparation_allowed"}
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


@pytest.mark.parametrize("key,reason", [
    ("_monitoring_handoff_active", "monitoring_handoff_active"),
    ("powerwall_settings_pending", "settings_pending"),
    ("powerwall_local_dispatch_stopping", "dispatch_stopping"),
    ("powerwall_settings_recovering", "settings_recovering"),
    ("tesla_control_method_changing", "control_method_changing"),
    ("native_max_startup_pending", "native_max_startup_pending"),
    ("powerwall_local_recovery_pending", "local_recovery_pending"),
    ("optimizer_force_restart_restore_pending", "optimizer_restore_pending"),
    ("max_backup_end_ts", "backup_preparation_active"),
    ("max_backup_recovery_unavailable", "backup_recovery_unavailable"),
])
def test_native_limit_status_identifies_runtime_fence_and_keeps_it_closed(key, reason):
    ns, _, runtime, _ = guards()
    runtime[key] = True
    assert ns["_native_limits_block_reason"]() == reason
    assert not ns["_native_limits_allowed"](False)


@pytest.mark.parametrize("name", ["force_charge", "force_discharge", "hold_soc", "self_consumption"])
def test_native_limit_status_names_the_active_control(name):
    ns, _, _, _ = guards()
    ns[name + "_state"]["active"] = True
    assert ns["_native_limits_block_reason"]() == name + "_active"
    assert not ns["_native_limits_allowed"](False)


def test_native_limit_status_preserves_own_transaction_recovery_and_monitoring_policy():
    ns, entry, runtime, profile = guards()
    entry.options["monitoring_mode"] = True
    assert ns["_native_limits_block_reason"]() is None
    runtime["powerwall_settings_pending"] = {"kind": "native_site_limits"}
    assert ns["_native_limits_block_reason"]() == "settings_pending"
    assert ns["_native_limits_allowed"](True)
    profile.monitoring_only = True
    assert ns["_native_limits_block_reason"]() == "monitoring_only_profile"
    assert not ns["_native_limits_allowed"](True)


@pytest.mark.parametrize("setting,reason", [
    ("system", "battery_system_unsupported"), ("site", "control_site_unavailable"),
    ("gateway", "local_gateway_unconfigured"), ("generation", "generation_replaced"),
    ("owner", "foreign_site_owner"), ("dispatch", "battery_dispatch_pending"),
    ("native_max", "native_max_charge_pending"),
])
def test_native_limit_status_identifies_configuration_owner_and_pending_leases(setting, reason):
    ns, _, runtime, _ = guards()
    if setting == "system": ns["active_battery_system"] = "other"
    elif setting == "site": ns["_control_site_id"] = None
    elif setting == "gateway": ns["local_battery_dispatch_configured"] = lambda _: False
    elif setting == "generation": ns["_local_powerwall_generation_current"] = lambda: False
    elif setting == "owner": ns["foreign_site_owner"] = lambda *_: "private-owner-id"
    else: runtime["powerwall_battery_dispatch" if setting == "dispatch" else "powerwall_native_max_charge"] = NS(pending=True)
    assert ns["_native_limits_block_reason"]() == reason
    assert not ns["_native_limits_allowed"](False)
