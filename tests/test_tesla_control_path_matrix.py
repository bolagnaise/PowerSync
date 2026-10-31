"""Cross-path Tesla contracts and a reviewed production mutation inventory.

These checks use software fakes only.  A passing receipt or target calculation
is not provider acknowledgement, settings readback, or physical battery proof.
The corresponding local-HA checklist is docs/tesla-control-path-matrix.md.
"""
from __future__ import annotations

import ast
import asyncio
from collections import Counter
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS

import pytest
from test_tesla_tariff_rate_limit import upload_harness

COMPONENT = Path(__file__).parents[1] / "custom_components/power_sync"


@pytest.fixture(scope="module")
def controls():
    """Load the actual lightweight modules without importing HA integration setup."""
    prefix = "tesla_path_matrix_probe"
    previous = {name: value for name, value in sys.modules.items()
                if name == prefix or name.startswith(prefix + ".")}
    for name in list(previous):
        sys.modules.pop(name)
    for name, path in ((prefix, COMPONENT), (prefix + ".powerwall_local", COMPONENT / "powerwall_local")):
        package = ModuleType(name)
        package.__path__ = [str(path)]
        sys.modules[name] = package
    try:
        yield NS(
            policy=importlib.import_module(prefix + ".powerwall_local.control_method"),
            site=importlib.import_module(prefix + ".powerwall_control"),
            dispatch=importlib.import_module(prefix + ".powerwall_local.battery_dispatch"),
            http=importlib.import_module(prefix + ".powerwall_control_http"),
        )
    finally:
        for name in list(sys.modules):
            if name == prefix or name.startswith(prefix + "."):
                sys.modules.pop(name)
        sys.modules.update(previous)


def site_config(method="auto", **changes):
    return dict(dict(tesla_control_method=method, powerwall_local_paired=True,
        powerwall_local_ip="gateway.local", optimization_max_charge_w=15000,
        optimization_max_discharge_w=15000, optimization_max_grid_export_w=5000,
        optimization_max_grid_import_w=15000, optimization_backup_reserve=.2,
        optimization_grid_charge_soc_cap=1, optimization_allow_grid_charge=True), **changes)


def telemetry(*, load=2000, solar=0, **changes):
    values = dict(load_w=load, solar_w=solar, battery_w=0, grid_w=load-solar,
        soc=60, backup_reserve_percent=20, grid_charging_enabled=True,
        grid_status="SystemGridConnected", raw={"config": {"site_info": {
            "min_site_meter_power_ac": -5, "max_site_meter_power_ac": 15}}})
    values.update(changes)
    return NS(**values)


@pytest.mark.parametrize("method", ["tariff", "local", "auto"])
@pytest.mark.parametrize("factory,direction,watts", [
    ("BatteryPowerTarget", "charge", -1200),
    ("BatteryPowerTarget", "discharge", 1200),
    ("GridExportTarget", "discharge", 2000),
    ("SiteImportTarget", "charge", 4000),
])
def test_numeric_target_matrix_keeps_quantity_and_local_route(controls, method, factory, direction, watts):
    target = getattr(controls.site, factory)(watts)
    plan = controls.policy.plan_route(site_config(method), direction, telemetry(),
        target_kind=target.kind, local_available=True)
    assert plan.route == "local"
    assert plan.target_kind is target.kind
    assert target.watts == watts


@pytest.mark.parametrize("direction", ["charge", "discharge"])
def test_cloud_only_tariff_max_does_not_borrow_a_gateway_requirement(controls, direction):
    config = site_config("tariff")
    config.update(powerwall_local_paired=False, powerwall_local_ip="")
    plan = controls.policy.plan_route(config, direction, target_kind=controls.site.NativeMaxTarget(direction).kind)
    assert plan.route == "tariff"


@pytest.mark.parametrize("direction,load,expected", [
    ("charge", 2000, "tariff"),
    ("charge", 6000, "local"),
    ("discharge", 4999, "local"),
    ("discharge", 5000, "local"),
    ("discharge", 5001, "tariff"),
])
def test_auto_max_matrix_accounts_for_complete_inverter_flow(controls, direction, load, expected):
    plan = controls.policy.plan_route(site_config(), direction, telemetry(load=load), local_available=True)
    assert plan.route == expected
    assert plan.target_kind is controls.policy.ControlTargetKind.NATIVE_MAX


def test_site_input_battery_input_and_grid_export_are_distinct_physical_targets(controls):
    snapshot = telemetry(load=2000, solar=500)
    config = site_config()
    # The same 4 kW request means different quantities at the two boundaries.
    assert controls.dispatch.battery_watts("charge", 4000, snapshot, config) == -4000
    assert controls.dispatch.battery_watts("charge", 4000, snapshot, config,
                                           target_basis="grid_import") == -2500
    assert controls.dispatch.battery_watts("discharge", 2000, snapshot, config) == 3500
    config["native_ev_unobserved_w"] = 1000
    assert controls.dispatch.battery_watts("charge", 4000, snapshot, config,
                                           target_basis="grid_import") == -1500
    assert config["optimization_max_grid_import_w"] == 15000


@pytest.mark.parametrize("method", ["tariff", "local", "auto"])
@pytest.mark.parametrize("direction", ["charge", "discharge"])
@pytest.mark.parametrize("field,value", [
    ("grid_status", "SystemIslandedActive"), ("soc", None),
    ("soc", float("nan")), ("load_w", float("nan")),
])
def test_numeric_dispatch_matrix_never_turns_unknown_physics_into_permission(
        controls, method, direction, field, value):
    with pytest.raises(ValueError):
        controls.dispatch.battery_watts(direction, 1000,
            telemetry(**{field: value}), site_config(method))


@pytest.mark.parametrize("method", ["tariff", "local", "auto"])
@pytest.mark.parametrize("runtime,changes,reason", [
    ({"native_max_startup_pending": True}, {}, "native_max_startup_pending"),
    ({"powerwall_local_recovery_pending": True}, {}, "native_max_local_recovery_pending"),
    ({"powerwall_settings_pending": {"id": "incident"}}, {}, "native_max_settings_unresolved"),
    ({"powerwall_settings_pending": {"id": "incident", "resume_cloud": True}}, {}, "native_max_local_settings_suspended"),
    ({"pending_tesla_restore": {"pending": True}}, {}, "native_max_restore_pending"),
    ({}, {"generation_current": False}, "native_max_runtime_stale"),
    ({}, {"monitoring_blocked": True}, "native_max_monitoring_mode"),
    ({}, {"license_allowed": False}, "native_max_license_denied"),
])
def test_route_preference_matrix_never_grants_native_admission(controls, method, runtime, changes, reason):
    # Planning a route is separate from the current write authority.
    controls.policy.plan_route(site_config(method), "charge", telemetry(), local_available=True)
    checks = dict(call_present=True, optimizer_call=True, targetless=True,
        generation_current=True, monitoring_blocked=False, demand_charge_protected=False,
        local_configured=True, license_allowed=True, direction="charge")
    checks.update(changes)
    decision = controls.policy.native_control_policy(runtime, **checks)
    assert decision.allowed is False and decision.reason == reason


@pytest.mark.parametrize("method", ["tariff", "local", "auto"])
@pytest.mark.parametrize("direction", ["charge", "discharge"])
@pytest.mark.asyncio
async def test_actual_numeric_entry_handlers_keep_local_writes_and_durable_stop(method, direction):
    # The existing harness extracts the production handler and controller,
    # supplies fake I/O, and records the actual persisted force-mode state.
    harness = importlib.import_module("test_powerwall_battery_dispatch")
    env, local, client, saved = harness.load_routing(tesla_control_method=method)
    try:
        result = await env["_try_local_powerwall_force"](
            direction, harness.call(source="user", power_w=1000), 15, 1000)
        assert result["control_path"] == "powerwall_local_dispatch"
        assert saved["force_mode_state"]["mode"] == direction
        client.dispatch_battery_power.assert_awaited_once()
        await env["_stop_local_powerwall_dispatch"]()
        assert saved["force_mode_state"] is None
        client.resume_battery_operation.assert_awaited_once()
    finally:
        if local.pending:
            await local.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["tariff", "local", "auto"])
async def test_actual_spread_import_entry_preserves_site_target_under_each_preference(method):
    harness = importlib.import_module("test_powerwall_battery_dispatch")
    env, local, client, _saved = harness.load_routing(tesla_control_method=method,
        optimization_spread_import_enabled=True, optimization_max_charge_w=20000)
    try:
        result = await env["_try_local_powerwall_force"]("charge",
            harness.call(source="optimizer", grid_import_target_w=5000), 15, 0)
        assert result["control_path"] == "powerwall_local_dispatch"
        assert result["power_target_basis"] == "grid_import"
        assert result["battery_power_w"] == -4200  # 5 kW site less 800 W net home load.
        assert env["force_charge_state"]["grid_import_target_w"] == 5000
        client.dispatch_battery_power.assert_awaited_once()
    finally:
        if local.pending:
            await local.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["tariff", "local", "auto"])
@pytest.mark.parametrize("vehicles,battery_w", [(0, -8200), (1, -5900), (2, -3600)])
async def test_actual_spread_import_entry_shares_durable_ev_budget_without_changing_target_or_deadline(
        method, vehicles, battery_w):
    harness = importlib.import_module("test_powerwall_battery_dispatch")
    budget_harness = importlib.import_module("test_native_rate_budget")
    native = budget_harness.m
    budget = native.SharedRateBudget(budget_harness.Store(), clock=lambda: budget_harness.NOW)
    journal = native.EntryRateJournal(budget, "ev-entry", "matrix-site")
    await journal.async_save({"sessions": {
        f"VIN{index}": dict(identity=f"provider:car{index}", baseline_amps=10,
            attempted_amps=10, confirmed_amps=10, watts_per_amp=230, uncertain=True)
        for index in range(vehicles)
    }})
    env, local, client, saved = harness.load_routing(tesla_control_method=method,
        tesla_energy_site_id="matrix-site", optimization_spread_import_enabled=True,
        optimization_max_charge_w=10000, optimization_max_grid_import_w=10000)
    env["site_identity"] = native.site_identity  # Source extraction strips this import.
    env["hass"].data[native.RUNTIME_KEY] = budget
    configured_limits = {name: env["entry"].data[name] for name in (
        "optimization_max_charge_w", "optimization_max_grid_import_w")}
    try:
        result = await env["handle_force_charge"](harness.call(source="optimizer",
            grid_import_target_w=9000, power_w=2000, duration=15))
        assert result["control_path"] == "powerwall_local_dispatch"
        assert result["power_target_basis"] == "grid_import"
        assert result["requested_power_w"] == 9000
        assert result["battery_power_w"] == battery_w
        assert client.dispatch_battery_power.call_args.args[0] == battery_w
        assert budget.unobserved_w("matrix-site") == vehicles * 2300
        assert local.grid_import_estimate_w == 9000 - vehicles * 2300
        assert saved["force_mode_state"]["power_w"] == 2000
        assert saved["force_mode_state"]["grid_import_target_w"] == 9000
        deadline, expiry = local._deadline, env["force_charge_state"]["expires_at"]
        await local._cancel_task()

        # A fresh meter now includes one reserved EV and another 1 kW of home
        # load. Its observed demand must replace that EV's outstanding claim.
        observed_ev_w = 2300 if vehicles else 0
        if vehicles:
            budget.observe("VIN0", watts=observed_ev_w, at=budget_harness.NOW,
                           meter_at=budget_harness.NOW)
        load = 2000 + observed_ev_w
        client.get_snapshot.return_value = harness.snapshot(load_w=load, solar_w=200, grid_w=load-200)
        await local._write_with_limit_retry()
        assert budget.unobserved_w("matrix-site") == max(0, vehicles-1) * 2300
        assert client.dispatch_battery_power.call_args.args[0] == battery_w + 1000
        assert client.dispatch_battery_power.await_count == 2
        assert local._deadline == deadline
        assert env["force_charge_state"]["expires_at"] == expiry
        assert env["force_charge_state"]["grid_import_target_w"] == 9000
        assert local._target == 9000 and local._target_basis == "grid_import"
        assert all(env["entry"].data[name] == value for name, value in configured_limits.items())
        assert all(local.config()[name] == value for name, value in configured_limits.items())
    finally:
        if local.pending:
            await local.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["current", "write_fence"])
async def test_actual_tariff_writer_rechecks_authority_after_site_queue_wait(
        controls, upload_harness, monkeypatch, revocation):
    upload, hass, raw, _clock = upload_harness
    raw.responses = [200]
    namespace = upload.__globals__
    # Keep any production-relative controlled-session import inside the same
    # isolated real package as the wrapper and coordinator used below.
    monkeypatch.setitem(namespace, "__package__", controls.site.__package__)
    monkeypatch.setitem(namespace, "async_get_clientsession",
        lambda owner: controls.http.controlled_session(owner, raw))
    queued = asyncio.Event()
    state = NS(current=True)

    def fence():
        if revocation == "write_fence" and not state.current:
            raise RuntimeError("original write authority expired")

    async def before_write():
        queued.set()
        return fence

    status = {}
    site = controls.site.coordinator_for(hass, site_id="site-a")
    async with site.write_context("preceding mutation"):
        pending = asyncio.create_task(upload(hass, "site-a", {"name": "force"}, "fake",
            api_provider="teslemetry", max_retries=1, before_write=before_write,
            is_current=lambda: state.current if revocation == "current" else True,
            accepted_status=status))
        await asyncio.wait_for(queued.wait(), timeout=2)
        assert raw.posts == []
        state.current = False
    assert await asyncio.wait_for(pending, timeout=2) is False
    assert raw.posts == []
    assert status.get("not_submitted") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_generic_energy_request_mutations_share_gate_while_reads_advance(controls, method):
    calls = []
    class Reply:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
    def request(verb, url, **kwargs):
        calls.append(verb)
        return Reply()
    hass = NS(data={})
    session = controls.http.controlled_session(hass, NS(request=request))
    site = controls.site.coordinator_for(hass, site_id=123)
    url = "https://provider.invalid/proxy/api/1/energy_sites/00123/storm_mode"
    async def mutate():
        async with session.request(method, url):
            pass
    async with site.write_context("preceding mutation"):
        for read in ("GET", "HEAD", "OPTIONS"):
            async with session.request(read, url):
                pass
        pending = asyncio.create_task(mutate())
        await asyncio.sleep(0)
        assert calls == ["GET", "HEAD", "OPTIONS"]
        assert not pending.done()
    await asyncio.wait_for(pending, timeout=2)
    assert calls == ["GET", "HEAD", "OPTIONS", method]
    assert len(site.snapshot()["effects"]) == 2  # Reads produce no mutation receipt.


def _functions(path):
    tree = ast.parse((COMPONENT / path).read_text())
    result = {}
    def visit(node, parents=()):
        current = parents + (node.name,) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else parents
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result[".".join(current)] = node
        for child in ast.iter_child_nodes(node):
            visit(child, current)
    visit(tree)
    return tree, result


def _calls(node, name):
    return [child for child in ast.walk(node) if isinstance(child, ast.Call)
            and ((isinstance(child.func, ast.Name) and child.func.id == name)
                 or (isinstance(child.func, ast.Attribute) and child.func.attr == name))]


def _energy_post_inventory():
    result = Counter()
    for path in COMPONENT.rglob("*.py"):
        if path.name == "tesla_local_pb2.py":
            continue
        source = path.read_text()
        if "energy_sites" not in source:
            continue
        tree = ast.parse(source)
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for call in _calls(tree, "post"):
            ancestors, current = [], call
            while current in parents:
                current = parents[current]
                if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    ancestors.append(current)
            owner = next((node for node in ancestors if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
            if owner is not None and "energy_sites" in ast.get_source_segment(source, owner):
                result[(str(path.relative_to(COMPONENT)), ".".join(node.name for node in reversed(ancestors)))] += 1
    return result


# Exact owners and counts make a new or moved energy-site writer require a
# review. POST read queries are explicit exemptions, not assumed mutations.
REVIEWED_ENERGY_POSTS = {
    ("__init__.py", "AEMOSpikeManager._enter_spike_mode_locked"): 1,
    ("__init__.py", "AEMOSpikeManager._exit_spike_mode_locked"): 2,
    ("__init__.py", "SavingSessionTariffManager._enter_session_mode_locked"): 1,
    ("__init__.py", "SavingSessionTariffManager._exit_session_mode_locked"): 2,
    ("__init__.py", "_send_tariff_to_tesla"): 1,
    ("__init__.py", "async_setup_entry._tesla_charge_kick._mode_bounce"): 2,
    ("__init__.py", "async_setup_entry._handle_sync_tou_internal"): 2,
    ("__init__.py", "async_setup_entry._handle_sync_tou_internal._calibration_recovery_check"): 1,
    ("__init__.py", "async_setup_entry._handle_sync_tou_internal._retry_autonomous_restore"): 1,
    ("__init__.py", "async_setup_entry.handle_solar_curtailment_check"): 2,
    ("__init__.py", "async_setup_entry.handle_solar_curtailment_with_websocket_data"): 2,
    ("__init__.py", "async_setup_entry._tesla_force_set_operation_mode.post"): 1,
    ("__init__.py", "async_setup_entry._tesla_force_apply_export_rule.cloud_request"): 1,
    ("__init__.py", "async_setup_entry._tesla_force_set_backup_reserve_cloud"): 1,
    ("__init__.py", "async_setup_entry.handle_set_autonomous"): 1,
    ("__init__.py", "async_setup_entry.handle_set_operation_mode._cloud._post_mode"): 1,
    ("config_flow.py", "PowerSyncOptionsFlow._restore_export_rule"): 1,
    ("tesla_grid_control.py", "async_set_tesla_grid_charging_confirmed"): 2,
    ("powerwall_local/client.py", "PowerwallLocalClient._send_signed_device_command"): 1,
    ("powerwall_local/views.py", "PowerwallCloudProbeView.post"): 1,
}
READ_ONLY_ENERGY_POSTS = {
    ("__init__.py", "BatteryHealthView._try_fleet_api_bms_fetch"): 2,
    ("__init__.py", "BatteryHealthView._try_fleet_api_solar_strings_fetch._read_query"): 2,
    ("powerwall_local/client.py", "PowerwallLocalClient._verify_pairing_cloud"): 1,
    ("powerwall_local/client.py", "PowerwallLocalClient.fetch_device_controller_json"): 1,
}
PROVISIONING_ENERGY_POSTS = {
    ("powerwall_local/pairing.py", "PowerwallPairingManager._poll_until_verified"): 1,
    ("powerwall_local/pairing.py", "PowerwallPairingManager._register_key"): 1,
}


def test_every_production_energy_post_is_explicitly_reviewed():
    assert _energy_post_inventory() == Counter(
        REVIEWED_ENERGY_POSTS | READ_ONLY_ENERGY_POSTS | PROVISIONING_ENERGY_POSTS)


def test_generic_coordinator_energy_mutations_are_explicitly_reviewed():
    _, functions = _functions("coordinator.py")
    delegates = Counter()
    for owner, function in functions.items():
        for call in _calls(function, "_tesla_api_call"):
            if call.args and isinstance(call.args[0], ast.Constant) and call.args[0].value == "POST":
                delegates[owner] += 1
    assert delegates == {
        "TeslaEnergyCoordinator.async_set_storm_watch": 1,
        "TeslaEnergyCoordinator.async_set_off_grid_ev_reserve": 1,
        "TeslaEnergyCoordinator.async_set_vpp_enrollment": 1,
    }
    assert _calls(functions["TeslaEnergyCoordinator._tesla_api_call"], "request")
    _, wrappers = _functions("powerwall_control_http.py")
    assert _calls(wrappers["_ControlledSession.request"], "_mutation")


def test_integration_session_alias_covers_all_background_and_service_posts():
    tree, functions = _functions("__init__.py")
    imports = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
               and any(alias.name == "async_get_clientsession" for alias in node.names)]
    assert imports and all(node.module == "powerwall_control_http" for node in imports)
    _, wrappers = _functions("powerwall_control_http.py")
    assert _calls(wrappers["async_get_clientsession"], "controlled_session")


@pytest.mark.parametrize("path,owner", [
    ("config_flow.py", "PowerSyncOptionsFlow._restore_export_rule"),
    ("powerwall_local/views.py", "PowerwallCloudProbeView.post"),
])
def test_other_cloud_writer_factories_use_the_controlled_session(path, owner):
    _, functions = _functions(path)
    assert _calls(functions[owner], "controlled_session")


def test_every_grid_charging_helper_caller_has_protected_session_construction():
    tree, functions = _functions("coordinator.py")
    callers = [node for name, node in functions.items()
               if _calls(node, "async_set_tesla_grid_charging_confirmed")
               and name.count(".") == 1]
    assert callers
    imports = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
               and any(alias.name == "async_get_clientsession" for alias in node.names)]
    assert imports and all(node.module == "powerwall_control_http" for node in imports)
    assert _calls(functions["TeslaEnergyCoordinator.__init__"], "async_get_clientsession")


def test_ev_settings_changes_use_canonical_entry_scoped_services():
    _, functions = _functions("automations/ev_charging_planner.py")
    setter = functions["AutoScheduleExecutor._set_export_rule"]
    assert _calls(setter, "async_call") and not _calls(setter, "post")
    assert any(isinstance(node, ast.Constant) and node.value == "entry_id" for node in ast.walk(setter))
    assert any(keyword.arg == "return_response" and isinstance(keyword.value, ast.Constant)
               and keyword.value.value is True for call in _calls(setter, "async_call")
               for keyword in call.keywords)
    obsolete = functions.get("AutoScheduleExecutor._set_tesla_backup_reserve")
    assert obsolete is None or not _calls(obsolete, "post")


def test_signed_local_writes_are_classified_and_fenced_at_transport_submission():
    _, functions = _functions("powerwall_local/transport.py")
    post = functions["TEDAPIv1rTransport.post_v1r"]
    assert _calls(post, "_mutation_label") and _calls(post, "run_write")
    assert any(isinstance(node, ast.Name) and node.id == "before_send" for node in ast.walk(post))
    assert any(isinstance(node, ast.Name) and node.id == "before_submit" for node in ast.walk(post))
    _, clients = _functions("powerwall_local/client.py")
    assert any((isinstance(node, ast.Attribute) and node.attr == "_control_session")
               or (isinstance(node, ast.Constant) and node.value == "_control_session")
               for node in ast.walk(clients["PowerwallLocalClient._send_signed_device_command"]))


def test_factory_binds_the_site_gate_and_debug_post_joins_the_same_gate():
    _, functions = _functions("powerwall_local/views.py")
    bind = functions["_bind_site_control"]
    attributes = {node.attr for node in ast.walk(bind) if isinstance(node, ast.Attribute)}
    assert {"_site_control", "_control_identity", "_control_current", "_control_session"} <= attributes
    assert _calls(functions["PowerwallDebugProbeView.post"], "write_context")
