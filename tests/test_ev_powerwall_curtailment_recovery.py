"""EV export-only writes and durable, per-vehicle Powerwall restoration."""
from __future__ import annotations

import ast
import asyncio
import copy
from collections.abc import Mapping
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from conftest import load_source_node

ROOT = Path(__file__).parents[1] / "custom_components/power_sync"
PLANNER = ROOT / "automations/ev_charging_planner.py"
HTTP = NS(ClientTimeout=lambda total: total, ClientError=ConnectionError)
METHODS = (
    "_set_export_rule", "_set_tesla_backup_reserve", "_get_current_export_rule", "_curtailment_entry_data", "_curtailment_runtime_current", "_curtailment_site_id", "_curtailment_recovery_allowed", "_curtailment_export_unowned",
    "_load_curtailment_recovery", "_persist_curtailment_recovery",
    "_disable_curtailment_for_ev", "_restore_curtailment",
    "_retry_curtailment_restoration", "evaluate",
)


def load(name, path, ns, *, strip_imports=False):
    node = load_source_node(path, name)
    if strip_imports:
        class StripImports(ast.NodeTransformer):
            def visit_ImportFrom(self, node):
                return ast.copy_location(ast.Pass(), node)
        node = StripImports().visit(node)
    unit = ast.Module(body=[node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(unit), str(path), "exec",
                 flags=__import__("__future__").annotations.compiler_flag), ns)
    return ns[name]


def planner_class():
    ns = dict(asyncio=asyncio, time=time, datetime=datetime, timezone=timezone,
              dataclass=dataclass, Mapping=Mapping, DOMAIN="power_sync", _LOGGER=logging.getLogger(__name__),
              aiohttp=HTTP, get_session_manager=lambda: None,
              REGISTRY_KEY="power_sync_tesla_control")
    load("canonical_site_id", ROOT / "powerwall_control.py", ns)
    load("foreign_site_owner", ROOT / "powerwall_control.py", ns)
    load("_ev_curtailment_battery_owner_present", PLANNER, ns)
    load("AutoScheduleState", PLANNER, ns)
    for name in METHODS:
        load(name, PLANNER, ns, strip_imports=True)
    return type("Executor", (), {name: ns[name] for name in METHODS}), ns


class Store:
    def __init__(self, saved=None):
        self._data = copy.deepcopy(saved or {})
        self.saved = copy.deepcopy(self._data)
        self.fail = False

    async def async_save(self):
        if self.fail:
            raise OSError("storage unavailable")
        self.saved = copy.deepcopy(self._data)


def executor(store=None):
    cls, ns = planner_class()
    obj = cls()
    obj.config_entry = NS(entry_id="entry", data={"tesla_energy_site_id": "site"}, options={})
    obj._state = {}
    obj._settings = {}
    obj._sync_inactive_smart_schedule_preserve_intent = lambda: None
    obj._save_cached_soc_if_needed = AsyncMock()
    obj._is_sigenergy_system = lambda: False
    runtime = {"automation_store": store if store is not None else Store()}
    obj.hass = NS(data={"power_sync": {"entry": runtime}},
                  services=NS(async_call=AsyncMock()))
    return obj, runtime, ns


async def wait_for_task_event(event, task, *, timeout=5.0):
    """Bound a staged test and surface an early product/harness task failure."""
    waiter = asyncio.create_task(event.wait())
    try:
        await asyncio.wait({waiter, task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            task.result()  # Preserve the original exception and its traceback.
        if not event.is_set():
            raise AssertionError("Task did not reach the expected test handshake")
    except BaseException:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


def state(obj, vin):
    cls = obj._load_curtailment_recovery.__globals__["AutoScheduleState"]
    return obj._state.setdefault(vin, cls(vehicle_id=vin))


class Reply:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self.payload


def canonical_service(obj, *, receipt, rule="pv_only", requested_rule="pv_only", timeout=False):
    """Use the actual canonical HTTP writer and acknowledgement contract."""
    ns = dict(asyncio=NS(CancelledError=asyncio.CancelledError,
                         TimeoutError=asyncio.TimeoutError, sleep=AsyncMock()),
              time=time, aiohttp=HTTP, hass=obj.hass, entry=obj.config_entry,
              DOMAIN="power_sync", _LOGGER=logging.getLogger(__name__),
              _command_generation=[0], _tesla_charge_kick_generation=[0],
              _local_powerwall_generation_current=lambda: True,
              _record_tesla_settings_submission=AsyncMock(),
              CONF_FLEET_API_BASE_URL="fleet_api_base_url",
              get_tesla_api_base_url=lambda *args: "https://tesla.test")
    permission_ns = {}
    exec((ROOT / "tesla_export_permissions.py").read_text(), permission_ns)
    ns["tesla_export_rule_from_site_info"] = permission_ns["tesla_export_rule_from_site_info"]
    sent = []

    def post(url, **kwargs):
        sent.append((url, kwargs))
        if timeout:
            raise asyncio.TimeoutError()
        return Reply(receipt)

    session = NS(post=post, get=lambda *a, **kw: Reply({
        "response": {"components": {"customer_preferred_export_rule": rule}}
    }))
    ns["async_get_clientsession"] = lambda h: session

    async def dispatch(hass, entry, *, cloud_call, **kwargs):
        return await cloud_call()

    ns["dispatch_powerwall_setting"] = dispatch
    load("_tesla_grid_export_write_result", ROOT / "__init__.py", ns)
    apply = load("_tesla_force_apply_export_rule", ROOT / "__init__.py", ns, strip_imports=True)

    async def service(domain, name, data, *, blocking, return_response):
        assert (domain, name, blocking, return_response) == ("power_sync", "set_grid_export", True, True)
        assert data == {"rule": requested_rule, "entry_id": "entry", "_ev_curtailment": True}
        accepted = await apply([("site", "token", "teslemetry")], data["rule"], reason="EV export")
        if not accepted:
            raise RuntimeError("Tesla did not accept export setting")
        return {"success": True, "settings_confirmed": True, "physical_effect_confirmed": False}

    obj.hass.services.async_call = AsyncMock(side_effect=service)
    obj._get_current_export_rule = AsyncMock(return_value=rule)
    return sent


@pytest.mark.asyncio
@pytest.mark.parametrize("rule", ["never", "pv_only", "battery_ok"])
@pytest.mark.parametrize("receipt", [
    {"response": {"result": True}}, {"response": ""},
    {"response": {"code": 201, "message": "Updated"}},
])
async def test_ev_export_routes_to_canonical_export_only_writer(receipt, rule):
    obj, _, _ = executor()
    sent = canonical_service(obj, receipt=receipt, rule=rule, requested_rule=rule)
    assert await obj._set_export_rule(rule) is True
    assert len(sent) == 1
    assert sent[0][0].endswith("/energy_sites/site/grid_import_export")
    assert sent[0][1]["json"] == {"customer_preferred_export_rule": rule}
    obj._get_current_export_rule.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt,timeout", [({"response": {"result": False}}, False),
                                           ({"response": ""}, True)])
async def test_ev_export_propagates_explicit_failure_and_timeout(receipt, timeout):
    obj, _, _ = executor()
    canonical_service(obj, receipt=receipt, timeout=timeout)
    assert await obj._set_export_rule("pv_only") is False
    obj._get_current_export_rule.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("observed", [None, "battery_ok", "never"])
async def test_ev_export_requires_independent_matching_readback(observed):
    obj, _, _ = executor()
    canonical_service(obj, receipt={"response": {"result": True}})
    obj._get_current_export_rule.return_value = observed
    assert await obj._set_export_rule("pv_only") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("components,top_level,expected", [
    ({"disallow_charge_from_grid_with_solar_installed": True}, {}, None),
    ({"disallow_charge_from_grid_with_solar_installed": False}, {}, None),
    ({"customer_preferred_export_rule": "never"}, {}, "never"),
    ({"customer_preferred_export_rule": None}, {"customer_preferred_export_rule": "pv_only"}, "pv_only"),
    ({"customer_preferred_export_rule": "never"}, {"customer_preferred_export_rule": "battery_ok"}, None),
])
async def test_original_rule_uses_explicit_export_evidence(components, top_level, expected, wrapped):
    obj, _, ns = executor()
    permission_ns = {}
    exec((ROOT / "tesla_export_permissions.py").read_text(), permission_ns)
    ns.update(CONF_FLEET_API_BASE_URL="fleet_api_base_url", CONF_TESLA_ENERGY_SITE_ID="site",
              get_tesla_api_token=lambda *a: ("token", "teslemetry"),
              get_tesla_api_base_url=lambda *a: "https://tesla.test",
              tesla_export_rule_from_site_info=permission_ns["tesla_export_rule_from_site_info"],
              async_get_clientsession=lambda h: NS(get=lambda *a, **kw: Reply(
                  {"response": {"components": components, **top_level}} if wrapped
                  else {"components": components, **top_level})))
    obj.config_entry.data["site"] = "site-id"
    assert await obj._get_current_export_rule() == expected


@pytest.mark.asyncio
async def test_failed_restore_keeps_original_owner_and_retries_after_backoff():
    obj, runtime, _ = executor()
    vin = state(obj, "VIN-A")
    obj._get_current_export_rule = AsyncMock(return_value="never")
    obj._set_export_rule = AsyncMock(side_effect=[True, False, True])
    assert await obj._disable_curtailment_for_ev(vin)
    saved_lease = copy.deepcopy(runtime["automation_store"].saved["ev_curtailment_recovery"]["entry"])
    await obj._restore_curtailment(vin)
    assert runtime["ev_curtailment_override"] is True
    assert vin.curtailment_override_active and vin.original_export_rule == "never"
    outstanding = runtime["automation_store"].saved["ev_curtailment_recovery"]["entry"]
    assert outstanding["source"] == "smart_schedule"
    assert outstanding["leases"]["VIN-A"]["acquired_at"] == saved_lease["leases"]["VIN-A"]["acquired_at"]
    await obj._retry_curtailment_restoration()
    assert obj._set_export_rule.await_count == 2  # No immediate hammering.
    obj._curtailment_restore_after = 0
    await obj._retry_curtailment_restoration()
    assert obj._set_export_rule.await_args.args == ("never",)
    assert not vin.curtailment_override_active and vin.original_export_rule is None
    assert "ev_curtailment_override" not in runtime
    assert "entry" not in runtime["automation_store"].saved["ev_curtailment_recovery"]


@pytest.mark.asyncio
async def test_restart_recovers_original_and_restores_without_vehicle_configuration():
    obj, runtime, _ = executor()
    obj._get_current_export_rule = AsyncMock(return_value="never")
    obj._set_export_rule = AsyncMock(return_value=True)
    assert await obj._disable_curtailment_for_ev(state(obj, "VIN-A"))
    saved = copy.deepcopy(runtime["automation_store"].saved)
    recovered, after, _ = executor(Store(saved))
    recovered._set_export_rule = AsyncMock(return_value=True)
    record = recovered._load_curtailment_recovery()
    assert record["leases"]["VIN-A"]["release_pending"] is True
    assert after["ev_curtailment_override"] is True
    assert recovered._state["VIN-A"].original_export_rule == "never"
    await recovered.evaluate({})
    recovered._set_export_rule.assert_awaited_once_with("never")
    assert "entry" not in after["automation_store"].saved["ev_curtailment_recovery"]


@pytest.mark.asyncio
async def test_two_vehicle_leases_release_only_the_last_owner():
    obj, runtime, _ = executor()
    a, b = state(obj, "VIN-A"), state(obj, "VIN-B")
    obj._get_current_export_rule = AsyncMock(return_value="never")
    obj._set_export_rule = AsyncMock(return_value=True)
    assert await obj._disable_curtailment_for_ev(a)
    assert await obj._disable_curtailment_for_ev(b)
    obj._get_current_export_rule.assert_awaited_once()
    before_release = obj._set_export_rule.await_count
    await obj._restore_curtailment(a)
    assert obj._set_export_rule.await_count == before_release
    assert not a.curtailment_override_active and b.curtailment_override_active
    assert runtime["ev_curtailment_override"] is True
    assert set(runtime["automation_store"].saved["ev_curtailment_recovery"]["entry"]["leases"]) == {"VIN-B"}
    await obj._restore_curtailment(b)
    assert obj._set_export_rule.await_args.args == ("never",)
    assert not b.curtailment_override_active
    assert "ev_curtailment_override" not in runtime


@pytest.mark.asyncio
async def test_restarted_vehicle_reacquires_while_other_recovered_lease_releases():
    obj, runtime, _ = executor()
    obj._get_current_export_rule = AsyncMock(return_value="never")
    obj._set_export_rule = AsyncMock(return_value=True)
    await obj._disable_curtailment_for_ev(state(obj, "VIN-A"))
    await obj._disable_curtailment_for_ev(state(obj, "VIN-B"))
    recovered, after, _ = executor(Store(runtime["automation_store"].saved))
    recovered._load_curtailment_recovery()
    recovered._set_export_rule = AsyncMock(return_value=True)
    assert await recovered._disable_curtailment_for_ev(recovered._state["VIN-B"])
    await recovered._retry_curtailment_restoration()
    assert recovered._set_export_rule.await_args_list[0].args == ("pv_only",)
    assert recovered._set_export_rule.await_count == 1
    assert after["ev_curtailment_override"] is True
    assert set(after["automation_store"].saved["ev_curtailment_recovery"]["entry"]["leases"]) == {"VIN-B"}


@pytest.mark.asyncio
async def test_persistence_precedes_request_and_storage_failure_blocks_acquisition():
    obj, runtime, _ = executor()
    store = runtime["automation_store"]
    obj._get_current_export_rule = AsyncMock(return_value="never")

    async def write(rule):
        saved = store.saved["ev_curtailment_recovery"]["entry"]
        assert saved["original_export_rule"] == "never"
        assert saved["leases"]["VIN-A"]["source"] == "smart_schedule"
        return True

    obj._set_export_rule = AsyncMock(side_effect=write)
    store.fail = True
    assert not await obj._disable_curtailment_for_ev(state(obj, "VIN-A"))
    obj._set_export_rule.assert_not_awaited()
    store.fail = False
    assert await obj._disable_curtailment_for_ev(obj._state["VIN-A"])


@pytest.mark.asyncio
async def test_cancelled_acquisition_keeps_a_durable_pending_restoration():
    obj, runtime, _ = executor()
    obj._get_current_export_rule = AsyncMock(return_value="never")
    obj._set_export_rule = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await obj._disable_curtailment_for_ev(state(obj, "VIN-A"))
    record = runtime["automation_store"].saved["ev_curtailment_recovery"]["entry"]
    assert record["leases"]["VIN-A"]["release_pending"] is True
    assert runtime["ev_curtailment_override"] is True


@pytest.mark.asyncio
async def test_two_acquisitions_serialize_original_capture_before_outbound_write():
    obj, runtime, _ = executor()
    a, b = state(obj, "VIN-A"), state(obj, "VIN-B")
    obj._get_current_export_rule = AsyncMock(return_value="never")
    submitted, finish = asyncio.Event(), asyncio.Event()

    async def write(rule):
        if not submitted.is_set():
            submitted.set()
            await finish.wait()
        return True

    obj._set_export_rule = AsyncMock(side_effect=write)
    first = asyncio.create_task(obj._disable_curtailment_for_ev(a))
    await wait_for_task_event(submitted, first)
    second = asyncio.create_task(obj._disable_curtailment_for_ev(b))
    await asyncio.sleep(0)
    assert obj._get_current_export_rule.await_count == 1
    assert obj._set_export_rule.await_count == 1
    finish.set()
    assert all(await asyncio.wait_for(asyncio.gather(first, second), timeout=5.0))
    record = runtime["automation_store"].saved["ev_curtailment_recovery"]["entry"]
    assert set(record["leases"]) == {"VIN-A", "VIN-B"}
    assert record["original_export_rule"] == "never"


@pytest.mark.asyncio
async def test_restart_evaluates_both_vehicles_before_restoring_disabled_first_profile():
    obj, runtime, _ = executor()
    obj._get_current_export_rule = AsyncMock(return_value="never")
    obj._set_export_rule = AsyncMock(return_value=True)
    await obj._disable_curtailment_for_ev(state(obj, "VIN-A"))
    await obj._disable_curtailment_for_ev(state(obj, "VIN-B"))
    recovered, after, _ = executor(Store(runtime["automation_store"].saved))
    recovered._settings = {"VIN-A": NS(enabled=False), "VIN-B": NS(enabled=True)}
    recovered._is_effective_shared_loadpoint_profile = lambda vin: True
    recovered._clear_start_failure = lambda vin: None
    recovered._clear_active_charging_preserve_intent = lambda *args: None
    recovered._set_export_rule = AsyncMock(return_value=True)

    async def evaluate_vehicle(vin, *args):
        assert vin == "VIN-B"
        assert await recovered._disable_curtailment_for_ev(recovered._state[vin])

    recovered._evaluate_vehicle = evaluate_vehicle
    await recovered.evaluate({})
    recovered._set_export_rule.assert_awaited_once_with("pv_only")
    assert not recovered._state["VIN-A"].curtailment_override_active
    assert recovered._state["VIN-B"].curtailment_override_active
    assert after["ev_curtailment_override"] is True
    assert set(after["automation_store"].saved["ev_curtailment_recovery"]["entry"]["leases"]) == {"VIN-B"}


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, False, True, {}, {"success": False}, {"success": 1}])
async def test_ev_export_requires_explicit_service_success_before_readback(response):
    obj, _, _ = executor()
    obj.hass.services.async_call.return_value = response
    obj._get_current_export_rule = AsyncMock(return_value="pv_only")
    assert await obj._set_export_rule("pv_only") is False
    obj._get_current_export_rule.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("response,expected", [
    ({"success": True}, True), ({"success": False}, False),
    ({"success": 1}, False), ({}, False), (None, False),
])
async def test_ev_reserve_uses_entry_targeted_verified_canonical_service(response, expected):
    obj, _, _ = executor()
    obj.hass.services.async_call.return_value = response
    assert await obj._set_tesla_backup_reserve(42) is expected
    obj.hass.services.async_call.assert_awaited_once_with(
        "power_sync", "set_backup_reserve",
        {"percent": 42, "entry_id": "entry", "source": "automation_preserve_charge"},
        blocking=True, return_response=True,
    )


@pytest.mark.asyncio
async def test_ev_reserve_propagates_canonical_timeout_as_failure():
    obj, _, _ = executor()
    obj.hass.services.async_call.side_effect = asyncio.TimeoutError()
    assert await obj._set_tesla_backup_reserve(42) is False


@pytest.mark.asyncio
async def test_reload_during_acquisition_save_never_submits_old_executor_intent():
    obj, runtime, _ = executor()
    obj._get_current_export_rule = AsyncMock(return_value="never")
    obj._set_export_rule = AsyncMock(return_value=True)
    started, release = asyncio.Event(), asyncio.Event()
    store = runtime["automation_store"]
    original_save = store.async_save

    async def save():
        started.set()
        await release.wait()
        await original_save()

    store.async_save = save
    task = asyncio.create_task(obj._disable_curtailment_for_ev(state(obj, "VIN-A")))
    await wait_for_task_event(started, task)
    replacement = {"automation_store": store, "auto_schedule_executor": object()}
    obj.hass.data["power_sync"]["entry"] = replacement
    release.set()
    assert await asyncio.wait_for(task, timeout=5.0) is False
    obj._set_export_rule.assert_not_awaited()
    assert "ev_curtailment_override" not in replacement


@pytest.mark.asyncio
async def test_failed_old_save_cannot_roll_back_reloaded_executor_lease():
    obj, runtime, _ = executor()
    obj._get_current_export_rule = AsyncMock(return_value="never")
    obj._set_export_rule = AsyncMock(return_value=True)
    a = state(obj, "VIN-A")
    assert await obj._disable_curtailment_for_ev(a)
    store = runtime["automation_store"]
    started, release = asyncio.Event(), asyncio.Event()

    async def failed_save():
        started.set()
        await release.wait()
        raise OSError("old storage operation failed")

    store.async_save = failed_save
    task = asyncio.create_task(obj._restore_curtailment(a))
    await wait_for_task_event(started, task)
    replacement = {"automation_store": store, "auto_schedule_executor": object()}
    obj.hass.data["power_sync"]["entry"] = replacement
    newer = {"original_export_rule": "never", "leases": {"VIN-B": {"source": "new_executor"}}}
    store._data["ev_curtailment_recovery"]["entry"] = newer
    release.set()
    await asyncio.wait_for(task, timeout=5.0)
    assert store._data["ev_curtailment_recovery"]["entry"] is newer
    obj._set_export_rule.assert_awaited_once_with("pv_only")
    assert "ev_curtailment_override" not in replacement


@pytest.mark.asyncio
async def test_replaced_executor_never_calls_newly_registered_service():
    obj, runtime, _ = executor()
    obj._curtailment_entry_data()
    obj.hass.data["power_sync"]["entry"] = {"auto_schedule_executor": object()}
    assert await obj._set_export_rule("never") is False
    assert await obj._set_tesla_backup_reserve(42) is False
    obj.hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_reloaded_executor_cannot_publish_old_service_success():
    obj, runtime, _ = executor()

    async def service(*args, **kwargs):
        obj.hass.data["power_sync"]["entry"] = {"auto_schedule_executor": object()}
        return {"success": True}

    obj.hass.services.async_call.side_effect = service
    obj._get_current_export_rule = AsyncMock(return_value="never")
    assert await obj._set_export_rule("never") is False
    obj._get_current_export_rule.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("journal_site,new_site,reason", [
    ("site", "other-site", "site_identity_changed"),
    (None, "site", "site_identity_missing"),
    ("site", None, "site_identity_missing"),
])
async def test_reload_never_restores_or_reacquires_unknown_or_different_physical_site(journal_site, new_site, reason):
    obj, runtime, _ = executor()
    obj._get_current_export_rule = AsyncMock(return_value="never")
    obj._set_export_rule = AsyncMock(return_value=True)
    assert await obj._disable_curtailment_for_ev(state(obj, "VIN-A"))
    saved = copy.deepcopy(runtime["automation_store"].saved)
    record = saved["ev_curtailment_recovery"]["entry"]
    if journal_site is None:
        record.pop("site_id")
    else:
        record["site_id"] = journal_site
    recovered, after, _ = executor(Store(saved))
    recovered.config_entry.data["tesla_energy_site_id"] = new_site
    recovered._set_export_rule = AsyncMock(return_value=True)
    recovered._get_current_export_rule = AsyncMock(return_value="never")
    await recovered.evaluate({})
    a = recovered._state["VIN-A"]
    assert a.curtailment_override_active and a.original_export_rule == "never"
    assert a.curtailment_restore_blocked == reason
    assert after["ev_curtailment_recovery_blocked"]["reason"] == reason
    assert "ev_curtailment_override" not in after
    assert not await recovered._disable_curtailment_for_ev(state(recovered, "VIN-B"))
    await recovered._restore_curtailment(a)
    recovered._set_export_rule.assert_not_awaited()
    recovered._get_current_export_rule.assert_not_awaited()
    assert after["automation_store"].saved == saved


@pytest.mark.asyncio
async def test_physical_site_identity_is_canonical_in_durable_journal():
    obj, runtime, _ = executor()
    obj.config_entry.data["tesla_energy_site_id"] = " 00123 "
    obj._get_current_export_rule = AsyncMock(return_value="never")
    obj._set_export_rule = AsyncMock(return_value=True)
    assert await obj._disable_curtailment_for_ev(state(obj, "VIN-A"))
    assert runtime["automation_store"].saved["ev_curtailment_recovery"]["entry"]["site_id"] == "123"
    recovered, after, _ = executor(Store(runtime["automation_store"].saved))
    recovered.config_entry.data["tesla_energy_site_id"] = 123
    recovered._set_export_rule = AsyncMock(return_value=True)
    await recovered.evaluate({})
    recovered._set_export_rule.assert_awaited_once_with("never")
    assert "ev_curtailment_recovery_blocked" not in after


@pytest.mark.asyncio
async def test_site_reconfiguration_while_journal_saves_blocks_original_request():
    obj, runtime, _ = executor()
    obj._get_current_export_rule = AsyncMock(return_value="never")
    obj._set_export_rule = AsyncMock(return_value=True)
    store = runtime["automation_store"]
    save = store.async_save

    async def save_and_reconfigure():
        await save()
        obj.config_entry.data["tesla_energy_site_id"] = "other-site"

    store.async_save = save_and_reconfigure
    a = state(obj, "VIN-A")
    assert not await obj._disable_curtailment_for_ev(a)
    obj._set_export_rule.assert_not_awaited()
    assert a.curtailment_restore_blocked == "site_identity_changed"
    assert runtime["ev_curtailment_recovery_blocked"]["site_id"] == "site"
    assert "ev_curtailment_override" not in runtime
    assert store.saved["ev_curtailment_recovery"]["entry"]["site_id"] == "site"


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [
    {"force_charge_state": {"active": True}}, {"force_discharge_state": {"active": True}},
    {"hold_soc_state": {"active": True}}, {"self_consumption_state": {"active": True}},
    {"powerwall_battery_dispatch": NS(pending=True)}, {"powerwall_native_max_charge": NS(pending=True)},
    {"max_backup_end_ts": 123}, {"optimizer_force_restart_restore_pending": True},
    {"pending_tesla_restore": {"site_id": "site"}}, {"powerwall_settings_pending": {"site_id": "site"}},
])
async def test_ev_acquisition_never_captures_a_battery_owners_temporary_export_rule(owner):
    obj, runtime, _ = executor()
    runtime.update(owner)
    obj._get_current_export_rule = AsyncMock(return_value="never")
    obj._set_export_rule = AsyncMock(return_value=True)
    a = state(obj, "VIN-A")
    assert not await obj._disable_curtailment_for_ev(a)
    obj._get_current_export_rule.assert_not_awaited()
    obj._set_export_rule.assert_not_awaited()
    assert a.original_export_rule is None and not a.curtailment_override_active
    assert "ev_curtailment_recovery" not in runtime["automation_store"].saved


@pytest.mark.asyncio
async def test_battery_owner_arriving_during_baseline_read_prevents_ev_acquisition():
    obj, runtime, _ = executor()

    async def baseline():
        runtime["force_charge_state"] = {"active": True}
        return "never"

    obj._get_current_export_rule = baseline
    obj._set_export_rule = AsyncMock(return_value=True)
    a = state(obj, "VIN-A")
    assert not await obj._disable_curtailment_for_ev(a)
    obj._set_export_rule.assert_not_awaited()
    assert a.original_export_rule is None
    assert "ev_curtailment_recovery" not in runtime["automation_store"].saved


@pytest.mark.asyncio
async def test_foreign_entry_battery_owner_blocks_ev_before_baseline_read():
    obj, runtime, _ = executor()
    obj.hass.data["power_sync"]["other-entry"] = {
        "entry": NS(data={"tesla_energy_site_id": "site"}, options={}),
        "force_charge_state": {"active": True},
    }
    obj._get_current_export_rule = AsyncMock(return_value="never")
    a = state(obj, "VIN-A")
    assert not await obj._disable_curtailment_for_ev(a)
    obj._get_current_export_rule.assert_not_awaited()
    assert "ev_curtailment_recovery" not in runtime["automation_store"].saved


@pytest.mark.asyncio
async def test_staged_handshake_reports_early_task_exception_instead_of_waiting():
    async def failed_before_handshake():
        raise NameError("REGISTRY_KEY fixture binding missing")

    task = asyncio.create_task(failed_before_handshake())
    with pytest.raises(NameError, match="REGISTRY_KEY fixture binding missing"):
        await wait_for_task_event(asyncio.Event(), task)
    assert task.done()


@pytest.mark.asyncio
async def test_staged_handshake_timeout_cancels_a_task_that_never_signals():
    async def never_signals():
        await asyncio.Event().wait()

    task = asyncio.create_task(never_signals())
    with pytest.raises(AssertionError, match="expected test handshake"):
        await wait_for_task_event(asyncio.Event(), task, timeout=0.01)
    assert task.cancelled()
