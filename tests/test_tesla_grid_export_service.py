"""Regression tests for Tesla grid-export application responses."""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import load_power_sync_function
from test_service_calls import CurrentServiceCall


INIT_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "power_sync"
    / "__init__.py"
)


@pytest.mark.parametrize('export_fails', [False, True])
def test_export_selector_finishes_before_next_automation_action(export_fails):
    """A sequential HA automation must not race export with grid charging."""
    tree = ast.parse(INIT_PATH.with_name('select.py').read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == 'TeslaGridExportRuleSelect')
    method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef)
                  and n.name == 'async_select_option')
    namespace = {'DOMAIN': 'power_sync'}
    exec(compile(ast.Module(body=[method], type_ignores=[]), '<export selector>', 'exec'), namespace)

    async def scenario():
        completed, queued = [], []

        async def apply_export():
            await asyncio.sleep(0)
            if export_fails:
                raise RuntimeError('export did not verify')
            completed.append('export')

        async def service(domain, name, data, *, blocking):
            assert (domain, name, data['rule']) == ('power_sync', 'set_grid_export', 'never')
            if blocking:
                await apply_export()
            else:
                queued.append(apply_export())

        selector = SimpleNamespace(hass=SimpleNamespace(services=SimpleNamespace(async_call=service)))
        try:
            if export_fails:
                with pytest.raises(RuntimeError, match='export did not verify'):
                    await namespace['async_select_option'](selector, 'never')
            else:
                await namespace['async_select_option'](selector, 'never')
                assert completed == ['export'], 'The following grid action must wait for export'
                completed.append('grid charging')
                assert completed == ['export', 'grid charging']
        finally:
            for operation in queued:
                operation.close()

    asyncio.run(scenario())


def _load_response_helper():
    source = INIT_PATH.read_text()
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_tesla_grid_export_write_result":
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            namespace: dict[str, Any] = {"Any": Any}
            exec("from __future__ import annotations\n" + segment, namespace)
            return namespace[node.name]
    raise AssertionError("_tesla_grid_export_write_result not found")


def _load_grid_export_handler(namespace: dict[str, Any]):
    source = INIT_PATH.read_text()
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry":
            for child in node.body:
                if isinstance(child, ast.AsyncFunctionDef) and child.name == "handle_set_grid_export":
                    segment = ast.get_source_segment(source, child)
                    assert segment is not None
                    segment = segment.replace(
                        "            from .const import CONF_POWERWALL_LOCAL_DIN\n",
                        "",
                    ).replace(
                        "            from .powerwall_local.dispatch import dispatch_powerwall_write\n",
                        "",
                    )
                    exec(
                        "from __future__ import annotations\n"
                        + textwrap.dedent(segment),
                        namespace,
                    )
                    return namespace[child.name]
    raise AssertionError("handle_set_grid_export not found")


class _Response:
    def __init__(self, payload: Any):
        self.payload = payload

    async def json(self) -> Any:
        return self.payload


def test_http_200_explicit_rejection_is_not_successful():
    helper = _load_response_helper()

    accepted, reason = asyncio.run(
        helper(_Response({"response": {"result": False, "reason": "not_allowed"}}))
    )

    assert accepted is False
    assert reason == "not_allowed"


def test_http_200_explicit_acceptance_is_successful():
    helper = _load_response_helper()

    accepted, reason = asyncio.run(
        helper(_Response({"response": {"result": True}}))
    )

    assert accepted is True
    assert reason is None


def test_http_200_missing_result_fails_closed():
    helper = _load_response_helper()

    accepted, reason = asyncio.run(helper(_Response({"response": {}})))

    assert accepted is False
    assert reason == "Tesla rejected the grid export rule"


def test_rejected_http_200_does_not_promote_requested_rule_to_snapshot():
    class _Logger:
        def __getattr__(self, _name):
            return lambda *_args, **_kwargs: None

    class _Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def json(self):
            return {"response": {"result": False, "reason": "not_allowed"}}

    class _Session:
        def post(self, *_args, **_kwargs):
            return _Response()

    snapshot = SimpleNamespace(grid_export_rule="battery_ok")
    entry_data = {
        "powerwall_local": {"coordinator": SimpleNamespace(data=snapshot)},
    }
    hass = SimpleNamespace(
        data={"power_sync": {"entry1": entry_data}},
        async_create_task=lambda _coro: None,
    )
    entry = SimpleNamespace(
        entry_id="entry1",
        data={
            "powerwall_local_din": "din",
            "tesla_energy_site_id": "site-1",
        },
        options={},
    )
    namespace: dict[str, Any] = {
        "Any": Any,
        "DOMAIN": "power_sync",
        "CONF_BATTERY_CURTAILMENT_ENABLED": "battery_curtailment_enabled",
        "CONF_FLEET_API_BASE_URL": "fleet_api_base_url",
        "CONF_POWERWALL_LOCAL_DIN": "powerwall_local_din",
        "CONF_TESLA_ENERGY_SITE_ID": "tesla_energy_site_id",
        "HomeAssistantError": RuntimeError,
        "_LOGGER": _Logger(),
        "_monitoring_mode_should_block_control": lambda _call: False,
        "_supersede_tesla_charge_kick": lambda *args: None,
        "_prepare_tesla_setting_change": __import__('unittest.mock', fromlist=['AsyncMock']).AsyncMock(return_value=(0, 0, 0)),
        "_command_generation": [0], "_tesla_charge_kick_generation": [0],
        "_tesla_force_apply_export_rule": __import__('unittest.mock', fromlist=['AsyncMock']).AsyncMock(return_value=False),
        "_get_tesla_site_configs": lambda _hass, _entry: [("site-1", "token", "fleet")],
        "async_get_clientsession": lambda _hass: _Session(),
        "get_tesla_api_base_url": lambda *_args: "https://tesla.example",
        "aiohttp": SimpleNamespace(ClientTimeout=lambda total: total),
        "hass": hass,
        "entry": entry,
        "_tesla_grid_export_write_result": _load_response_helper(),
    }

    async def _dispatch(_hass, _entry, *, cloud_call, **_kwargs):
        return await cloud_call()

    async def _update_cached(_rule):
        raise AssertionError("rejected write must not update the cached rule")

    async def _refresh(_label):
        raise AssertionError("rejected write must not schedule a refresh")

    namespace["dispatch_powerwall_write"] = _dispatch
    namespace["update_cached_export_rule"] = _update_cached
    namespace["refresh_powerwall_local_after_settings_write"] = _refresh
    handler = _load_grid_export_handler(namespace)

    result = asyncio.run(handler(SimpleNamespace(data={"rule": "never"})))
    assert result["success"] is False
    assert "did not accept" in result["error"]

    assert snapshot.grid_export_rule == "battery_ok"
    assert "cached_export_rule" not in entry_data


def _registered_export_services(service_name="set_grid_export", *, real_prepare=False):
    """Execute real service registration, routing and per-entry handler bodies."""
    import logging
    from collections.abc import Mapping
    from unittest.mock import AsyncMock
    from conftest import load_source_node

    handlers, tasks, contexts = {}, [], {}

    def register(domain, name, handler, **kwargs):
        if name == "set_grid_export":
            assert kwargs.get("supports_response") == "optional"
        handlers[(domain, name)] = handler

    def create_task(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    hass = SimpleNamespace(data={"power_sync": {}},
        services=SimpleNamespace(async_register=register), async_create_task=create_task)
    routing = {}
    exec(INIT_PATH.with_name("licensing").joinpath("entry_routing.py").read_text(), routing)
    setup = load_source_node(INIT_PATH, "async_setup_entry")
    registration = next(node for node in setup.body
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name) and node.value.func.id == "register_handlers"
        and isinstance(node.value.args[-1], ast.Dict))
    service_registration = next(node for node in setup.body
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute) and node.value.func.attr == "async_register"
        and len(node.value.args) > 1 and isinstance(node.value.args[1], ast.Name)
        and node.value.args[1].id == "SERVICE_SET_GRID_EXPORT")
    auto_registration = next(node for node in setup.body
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute) and node.value.func.attr == "async_register"
        and len(node.value.args) > 1 and isinstance(node.value.args[1], ast.Constant)
        and node.value.args[1].value == "set_grid_export_auto")

    class StripImports(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            return ast.copy_location(ast.Pass(), node)

    def load(name, path, namespace):
        body = load_source_node(path, name)
        body.decorator_list = []
        body = StripImports().visit(body)
        exec(compile(ast.fix_missing_locations(ast.Module(body=[body], type_ignores=[])),
                     str(path), "exec", flags=__import__("__future__").annotations.compiler_flag), namespace)
        return namespace[name]

    for entry_id in ("entry1", "entry2"):
        runtime = {"store": object()}
        hass.data["power_sync"][entry_id] = runtime
        entry = SimpleNamespace(entry_id=entry_id, data={"tesla_energy_site_id": entry_id},
                                options={"battery_curtailment_enabled": True})
        ns = dict(asyncio=asyncio, hass=hass, entry=entry, DOMAIN="power_sync",
            SERVICE_SET_GRID_EXPORT="set_grid_export", SupportsResponse=SimpleNamespace(OPTIONAL="optional"), Any=Any,
            CONF_TESLA_ENERGY_SITE_ID="tesla_energy_site_id", Mapping=Mapping,
            REGISTRY_KEY="power_sync_tesla_control",
            CONF_BATTERY_CURTAILMENT_ENABLED="battery_curtailment_enabled",
            HomeAssistantError=RuntimeError, _LOGGER=logging.getLogger(__name__),
            _monitoring_mode_should_block_control=lambda call: False,
            _prepare_tesla_setting_change=AsyncMock(return_value=(0, 0, 0)),
            _command_generation=[0], _tesla_charge_kick_generation=[0], _tesla_operation_generation=[0],
            _local_powerwall_generation_current=lambda: True,
            _tesla_force_apply_export_rule=AsyncMock(return_value=True),
            _get_tesla_site_configs=lambda h, e: [(e.entry_id, "token", "fleet")],
            update_cached_export_rule=AsyncMock(return_value=True),
            _update_powerwall_store=AsyncMock(return_value=True),
            refresh_powerwall_local_after_settings_write=AsyncMock(),
            register_handlers=routing["register_handlers"], routed_handler=routing["routed_handler"],
            amber_smartshift_coordinator=None, native_handlers={})
        load("canonical_site_id", INIT_PATH.with_name("powerwall_control.py"), ns)
        load("foreign_site_owner", INIT_PATH.with_name("powerwall_control.py"), ns)
        load("_ev_curtailment_battery_owner_present", INIT_PATH.with_name("automations") / "ev_charging_planner.py", ns)
        if real_prepare:
            for key in ("force_charge_state", "force_discharge_state", "hold_soc_state", "self_consumption_state"):
                ns[key] = runtime[key] = {"active": False}
            ns.update(_stop_local_powerwall_dispatch=AsyncMock(), handle_restore_normal=AsyncMock(),
                      SERVICE_RESTORE_NORMAL="restore_normal",
                      ServiceCall=CurrentServiceCall)
            ns['make_service_call'] = load_power_sync_function('make_service_call',
                {'signature': inspect.signature, 'ServiceCall': CurrentServiceCall},
                path=INIT_PATH.with_name('service_calls.py'))
            load("_supersede_tesla_charge_kick", INIT_PATH, ns)
            prepare = load("_prepare_tesla_setting_change", INIT_PATH, ns)
            ns["_prepare_tesla_setting_change"] = AsyncMock(wraps=prepare)
        load("handle_set_grid_export", INIT_PATH, ns)
        load("handle_set_grid_export_auto", INIT_PATH, ns)
        # Unrelated registered controls are inert; retain the production map.
        for node in ast.walk(registration):
            if isinstance(node, ast.Name) and node.id.startswith("handle_") and node.id not in ns:
                ns[node.id] = AsyncMock()
        unit = ast.Module(body=[registration, service_registration, auto_registration], type_ignores=[])
        exec(compile(ast.fix_missing_locations(unit), str(INIT_PATH), "exec"), ns)
        contexts[entry_id] = ns
    return hass, handlers[("power_sync", service_name)], contexts, tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", ["entry1", "entry2", None])
@pytest.mark.parametrize("ev_intent", [False, True])
async def test_registered_export_service_routes_exact_entry_and_preserves_ev_ownership(requested, ev_intent):
    hass, service, contexts, tasks = _registered_export_services()
    data = {"rule": "pv_only"}
    if requested is not None:
        data["entry_id"] = requested
    if ev_intent:
        data["_ev_curtailment"] = True
    result = await service(SimpleNamespace(data=data))
    assert result == {"success": True, "settings_confirmed": True, "physical_effect_confirmed": False}
    selected = requested or "entry2"  # HA keeps the most recent registration.
    other = "entry1" if selected == "entry2" else "entry2"
    contexts[selected]["_tesla_force_apply_export_rule"].assert_awaited_once()
    contexts[other]["_tesla_force_apply_export_rule"].assert_not_awaited()
    assert contexts[selected]["_tesla_force_apply_export_rule"].await_args.kwargs["require_readback"] is True
    assert contexts[selected]["_tesla_force_apply_export_rule"].await_args.args == (
        [(selected, "token", "fleet")], "pv_only")
    runtime = hass.data["power_sync"][selected]
    if ev_intent:
        contexts[selected]["_prepare_tesla_setting_change"].assert_not_awaited()
        assert "manual_export_override" not in runtime
        assert "manual_export_rule" not in runtime
        contexts[selected]["_update_powerwall_store"].assert_not_awaited()
    else:
        assert runtime["manual_export_override"] is True
        assert runtime["manual_export_rule"] == "pv_only"
        assert contexts[selected]["_update_powerwall_store"].await_args.args[1] == {
            "manual_export_override": True, "manual_export_rule": "pv_only"}
    contexts[selected]["update_cached_export_rule"].assert_awaited_once()
    contexts[other]["update_cached_export_rule"].assert_not_awaited()
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_registered_ev_export_preserves_an_existing_manual_override():
    hass, service, contexts, tasks = _registered_export_services()
    runtime = hass.data["power_sync"]["entry1"]
    runtime.update(manual_export_override=True, manual_export_rule="battery_ok")
    await service(SimpleNamespace(data={"rule": "never", "entry_id": "entry1", "_ev_curtailment": True}))
    assert runtime["manual_export_override"] is True
    assert runtime["manual_export_rule"] == "battery_ok"
    contexts["entry1"]["_update_powerwall_store"].assert_not_awaited()
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_registered_export_rejects_unavailable_entry_without_other_site_write():
    _, service, contexts, _ = _registered_export_services()
    with pytest.raises(ValueError, match="control_entry_not_available"):
        await service(SimpleNamespace(data={"rule": "never", "entry_id": "missing", "_ev_curtailment": True}))
    for ns in contexts.values():
        ns["_tesla_force_apply_export_rule"].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [
    {"force_charge_state": {"active": True}}, {"force_discharge_state": {"active": True}},
    {"hold_soc_state": {"active": True}}, {"self_consumption_state": {"active": True}},
    {"powerwall_battery_dispatch": SimpleNamespace(pending=True)},
    {"powerwall_native_max_charge": SimpleNamespace(pending=True)},
    {"max_backup_end_ts": 123}, {"optimizer_force_restart_restore_pending": True},
    {"pending_tesla_restore": {"site_id": "entry1"}},
])
async def test_ev_export_composition_refuses_battery_owner_without_preparation_or_cleanup(owner):
    hass, service, contexts, _ = _registered_export_services(real_prepare=True)
    runtime = hass.data["power_sync"]["entry1"]
    runtime.update(owner)
    ns = contexts["entry1"]
    for key in ("force_charge_state", "force_discharge_state", "hold_soc_state", "self_consumption_state"):
        if key in owner:
            ns[key] = runtime[key]
    result = await _ha_export_response(service, {"rule": "pv_only", "entry_id": "entry1", "_ev_curtailment": True})
    assert result["success"] is False
    assert "waiting for battery control restoration" in result["error"]
    ns["_prepare_tesla_setting_change"].assert_not_awaited()
    ns["_stop_local_powerwall_dispatch"].assert_not_awaited()
    ns["handle_restore_normal"].assert_not_awaited()
    ns["_tesla_force_apply_export_rule"].assert_not_awaited()
    assert ns["_tesla_charge_kick_generation"] == [0]
    assert ns["_command_generation"] == [0]


@pytest.mark.asyncio
async def test_unowned_ev_export_uses_only_verified_export_helper_and_never_prepares():
    _, service, contexts, tasks = _registered_export_services(real_prepare=True)
    ns = contexts["entry1"]
    result = await service(SimpleNamespace(data={"rule": "pv_only", "entry_id": "entry1", "_ev_curtailment": True}))
    assert result["success"] is True and result["settings_confirmed"] is True
    ns["_prepare_tesla_setting_change"].assert_not_awaited()
    ns["_stop_local_powerwall_dispatch"].assert_not_awaited()
    ns["handle_restore_normal"].assert_not_awaited()
    ns["_tesla_force_apply_export_rule"].assert_awaited_once()
    assert ns["_tesla_charge_kick_generation"] == [0]
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["force", "native", "operation", "runtime"])
async def test_ev_export_final_helper_retains_original_owner_fences(change):
    hass, service, contexts, _ = _registered_export_services(real_prepare=True)
    ns = contexts["entry1"]
    runtime = hass.data["power_sync"]["entry1"]

    async def apply(*args, is_current, **kwargs):
        assert is_current()
        if change == "force":
            runtime["force_charge_state"]["active"] = True
        elif change == "native":
            runtime["powerwall_native_max_charge"] = SimpleNamespace(pending=True)
        elif change == "operation":
            ns["_tesla_operation_generation"][0] += 1
        else:
            hass.data["power_sync"]["entry1"] = {}
        assert not is_current()
        return False

    ns["_tesla_force_apply_export_rule"].side_effect = apply
    result = await _ha_export_response(service, {"rule": "pv_only", "entry_id": "entry1", "_ev_curtailment": True})
    assert result["success"] is False and "did not accept" in result["error"]
    ns["_prepare_tesla_setting_change"].assert_not_awaited()
    ns["handle_restore_normal"].assert_not_awaited()
    ns["update_cached_export_rule"].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", ["entry1", "entry2", None])
async def test_export_auto_routes_exact_entry_without_releasing_ev_lease(requested):
    hass, service, contexts, _ = _registered_export_services("set_grid_export_auto")
    leases = {}
    for entry_id in contexts:
        runtime = hass.data["power_sync"][entry_id]
        leases[entry_id] = {"site_id": entry_id, "leases": {"VIN": {"source": "smart_schedule"}}}
        runtime.update(manual_export_override=True, manual_export_rule="never",
                       ev_curtailment_override=True, ev_curtailment_recovery=leases[entry_id])
    data = {} if requested is None else {"entry_id": requested}
    await service(SimpleNamespace(data=data))
    selected = requested or "entry2"
    other = "entry2" if selected == "entry1" else "entry1"
    assert hass.data["power_sync"][selected]["manual_export_override"] is False
    assert hass.data["power_sync"][selected]["manual_export_rule"] is None
    assert hass.data["power_sync"][other]["manual_export_override"] is True
    contexts[selected]["_update_powerwall_store"].assert_awaited_once()
    assert contexts[selected]["_update_powerwall_store"].await_args.args[1] == {
        "manual_export_override": False, "manual_export_rule": None}
    contexts[other]["_update_powerwall_store"].assert_not_awaited()
    for entry_id, ns in contexts.items():
        runtime = hass.data["power_sync"][entry_id]
        assert runtime["ev_curtailment_override"] is True
        assert runtime["ev_curtailment_recovery"] is leases[entry_id]
        ns["_prepare_tesla_setting_change"].assert_not_awaited()
        ns["_tesla_force_apply_export_rule"].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("brand", ["alphaess_coordinator", "goodwe_coordinator"])
async def test_ev_export_intent_cannot_route_to_an_unrelated_inverter_controller(brand):
    from unittest.mock import AsyncMock
    hass, service, contexts, tasks = _registered_export_services(real_prepare=True)
    controller = SimpleNamespace(curtail=AsyncMock(), restore=AsyncMock())
    hass.data["power_sync"]["entry1"][brand] = SimpleNamespace(_controller=controller)
    result = await service(SimpleNamespace(data={"rule": "pv_only", "entry_id": "entry1", "_ev_curtailment": True}))
    assert result["success"] is True
    controller.curtail.assert_not_awaited()
    controller.restore.assert_not_awaited()
    contexts["entry1"]["_tesla_force_apply_export_rule"].assert_awaited_once()
    await asyncio.gather(*tasks)


async def _ha_export_response(service, data):
    """Mirror HA's required dictionary result for return_response=True."""
    response = await service(SimpleNamespace(data=data))
    if not isinstance(response, dict):
        raise TypeError(f"expected dictionary, got {type(response).__name__}")
    return response


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["monitoring", "invalid_rule", "missing_rule", "network", "missing_runtime", "missing_config", "missing_config_ev"])
async def test_export_no_action_returns_explicit_failure_response_without_write(refusal):
    hass, service, contexts, tasks = _registered_export_services()
    ns = contexts["entry1"]
    data = {"rule": "pv_only", "entry_id": "entry1", "source": "optimizer"}
    if refusal == "monitoring":
        ns["_monitoring_mode_should_block_control"] = lambda call: True
    elif refusal == "invalid_rule":
        data["rule"] = "invalid"
    elif refusal == "missing_rule":
        data.pop("rule")
    elif refusal == "network":
        hass.data["power_sync"]["entry1"]["network_envelope_manager"] = SimpleNamespace(
            snapshot=SimpleNamespace(mode="enforce"))
    elif refusal == "missing_runtime":
        hass.data["power_sync"].pop("entry1")
        service = ns["handle_set_grid_export"]
    else:
        ns["_get_tesla_site_configs"] = lambda *args: []
        if refusal == "missing_config_ev":
            data["_ev_curtailment"] = True
            ns["entry"].data.pop("tesla_energy_site_id")
    result = await _ha_export_response(service, data)
    assert result["success"] is False and result["error"]
    ns["_tesla_force_apply_export_rule"].assert_not_awaited()
    ns["update_cached_export_rule"].assert_not_awaited()
    ns["refresh_powerwall_local_after_settings_write"].assert_not_awaited()
    if refusal != "missing_config":
        ns["_prepare_tesla_setting_change"].assert_not_awaited()
    assert tasks == []


@pytest.mark.asyncio
@pytest.mark.parametrize("brand", ["alphaess", "goodwe"])
@pytest.mark.parametrize("rule", ["never", "pv_only", "battery_ok"])
@pytest.mark.parametrize("accepted", [False, True])
async def test_non_tesla_export_returns_only_proven_controller_acknowledgement(brand, rule, accepted):
    from unittest.mock import AsyncMock
    hass, service, contexts, tasks = _registered_export_services()
    ns = contexts["entry1"]
    controller = SimpleNamespace(curtail=AsyncMock(return_value=accepted), restore=AsyncMock(return_value=accepted))
    runtime = hass.data["power_sync"]["entry1"]
    runtime[brand + "_coordinator"] = SimpleNamespace(_controller=controller)
    result = await _ha_export_response(service, {"rule": rule, "entry_id": "entry1"})
    assert result["success"] is accepted and result["accepted"] is accepted
    assert result["settings_confirmed"] is False and result["physical_effect_confirmed"] is False
    assert (result["error"] is None) is accepted
    if rule == "never":
        controller.curtail.assert_awaited_once()
        controller.restore.assert_not_awaited()
    else:
        controller.restore.assert_awaited_once()
        controller.curtail.assert_not_awaited()
    if accepted:
        assert runtime[brand + "_curtailment_state"] == ("curtailed" if rule == "never" else "normal")
    else:
        assert brand + "_curtailment_state" not in runtime
    ns["_prepare_tesla_setting_change"].assert_not_awaited()
    ns["_tesla_force_apply_export_rule"].assert_not_awaited()
    assert tasks == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("transport failed"), asyncio.CancelledError("request superseded")])
async def test_export_response_contract_does_not_swallow_transport_errors_or_cancellation(failure):
    _, service, contexts, tasks = _registered_export_services()
    ns = contexts["entry1"]
    ns["_tesla_force_apply_export_rule"].side_effect = failure
    with pytest.raises(type(failure), match=str(failure)):
        await _ha_export_response(service, {"rule": "never", "entry_id": "entry1"})
    ns["update_cached_export_rule"].assert_not_awaited()
    assert tasks == []
