"""Execute the actual local settings transaction with controlled gateway I/O."""
from __future__ import annotations

import ast
import asyncio
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import aiohttp

ROOT = Path(__file__).parents[1] / "custom_components/power_sync"


def load_function(path, name, namespace, *, strip_imports=False):
    node = next(n for n in ast.walk(ast.parse(path.read_text()))
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name)
    if strip_imports:
        class StripImports(ast.NodeTransformer):
            def visit_ImportFrom(self, node):
                return ast.copy_location(ast.Pass(), node)
        node = StripImports().visit(node)
    if any(isinstance(item, ast.Name) and item.id == "_track_tesla_settings_control" for item in getattr(node, "decorator_list", [])):
        if "_track_tesla_settings_control" not in namespace:
            load_function(ROOT / "__init__.py", "_track_tesla_settings_control", namespace)
    node = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    exec(compile(node, str(path), "exec", flags=__import__("__future__").annotations.compiler_flag), namespace)
    return namespace[name]


def transaction():
    ns = dict(asyncio=asyncio, time=time)
    return load_function(ROOT / "powerwall_local/dispatch.py", "write_config_confirmed", ns)


@pytest.mark.asyncio
async def test_local_success_requires_independent_exact_readback():
    transport = SimpleNamespace(write_config=AsyncMock(return_value=True),
                                read_config=AsyncMock(return_value={"site_info": {"flag": True}}))
    result = await transaction()(transport, "din", {"site_info.flag": True},
                                 is_current=lambda: True, deadline=time.monotonic() + 5)
    assert result is True
    transport.write_config.assert_awaited_once()
    transport.read_config.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, 1, "true", {}, [True]])
async def test_local_boolean_readback_does_not_coerce_unknown_values(value):
    transport = SimpleNamespace(write_config=AsyncMock(return_value=True),
                                read_config=AsyncMock(return_value={"site_info": {"flag": value}}))
    assert not await transaction()(transport, "din", {"site_info.flag": True},
                                   is_current=lambda: True, deadline=time.monotonic() + .01)
    transport.write_config.assert_awaited_once()


@pytest.mark.asyncio
async def test_submission_guard_runs_after_rmw_read():
    current = True
    writes = []

    async def write(din, updates, *, before_write, **kwargs):
        nonlocal current
        current = False  # A newer command arrives during the config read.
        before_write()
        writes.append(updates)
        return True

    transport = SimpleNamespace(write_config=write, read_config=AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await transaction()(transport, "din", {"default_real_mode": "autonomous"},
                            is_current=lambda: current, deadline=time.monotonic() + 5)
    assert not writes
    transport.read_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_supersession_during_readback_cannot_confirm():
    current = True

    async def read(din):
        nonlocal current
        current = False
        return {"default_real_mode": "autonomous"}

    transport = SimpleNamespace(write_config=AsyncMock(return_value=True), read_config=read)
    with pytest.raises(asyncio.CancelledError):
        await transaction()(transport, "din", {"default_real_mode": "autonomous"},
                            is_current=lambda: current, deadline=time.monotonic() + 5)


@pytest.mark.asyncio
async def test_expired_operation_never_submits():
    transport = SimpleNamespace(write_config=AsyncMock(), read_config=AsyncMock())
    assert not await transaction()(transport, "din", {"default_real_mode": "autonomous"},
                                   is_current=lambda: True, deadline=time.monotonic() - 1)
    transport.write_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_acknowledgement_is_preserved_when_confirmation_times_out():
    accepted = []
    transport = SimpleNamespace(write_config=AsyncMock(return_value=True),
                                read_config=AsyncMock(return_value={}))
    assert not await transaction()(transport, "din", {"default_real_mode": "autonomous"},
                                   is_current=lambda: True, deadline=time.monotonic() + .01,
                                   acknowledged=lambda: accepted.append(True))
    assert accepted == [True]
    transport.write_config.assert_awaited_once()


@pytest.mark.parametrize("value", [{"unexpected": True}, [True], 2, -1, float("nan")])
def test_cloud_grid_parser_rejects_arbitrary_truthiness(value):
    ns = {}
    load_function(ROOT / "tesla_grid_control.py", "_optional_bool", ns)
    parse = load_function(ROOT / "tesla_grid_control.py", "tesla_grid_charging_enabled_from_site_info", ns)
    assert parse({"disallow_charge_from_grid_with_solar_installed": value}) is None


def settings_context(transport=None, *, paired=True):
    runtime = {}
    hass = SimpleNamespace(data={"power_sync": {"entry": runtime}})
    entry = SimpleNamespace(entry_id="entry", options={"tesla_control_method": "tariff"},
                            data={"tesla_energy_site_id": "site", "powerwall_local_din": "din",
                                  "powerwall_local_paired": paired})
    ns = dict(asyncio=asyncio, time=time, DOMAIN="power_sync", _LOGGER=logging.getLogger(__name__),
              get_local_transport=lambda h, e: transport if e.data["powerwall_local_paired"] else None,
              hass=hass, entry=entry, _local_powerwall_generation_current=lambda: True,
              active_battery_system="tesla", BATTERY_SYSTEM_TESLA="tesla", HomeAssistantError=RuntimeError)
    for name in ("PowerwallLocalError", "PowerwallUnreachableError", "PowerwallSignatureError", "PowerwallAuthError", "PowerwallWriteRejected"):
        load_function(ROOT / "powerwall_local/exceptions.py", name, ns)
    ns["_LOCAL_FALLBACK_EXC"] = (ns["PowerwallLocalError"], asyncio.TimeoutError)
    load_function(ROOT / "powerwall_local/dispatch.py", "_SettingsPolicyDenied", ns)
    for name in ("write_config_confirmed", "_record_setting_observation", "dispatch_powerwall_setting"):
        load_function(ROOT / "powerwall_local/dispatch.py", name, ns)
    return ns, runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("paired", [True, False])
@pytest.mark.parametrize("site", ["site", "other-site"])
async def test_pairing_and_primary_identity_control_local_eligibility(paired, site):
    transport = SimpleNamespace(write_config=AsyncMock(return_value=True),
                                read_config=AsyncMock(return_value={"default_real_mode": "autonomous"}))
    ns, _ = settings_context(transport, paired=paired)
    cloud = AsyncMock(return_value=True)
    assert await ns["dispatch_powerwall_setting"](
        ns["hass"], ns["entry"], site_id=site, updates={"default_real_mode": "autonomous"},
        cloud_call=cloud, is_current=lambda: True, deadline=time.monotonic() + 5, label="mode")
    assert transport.write_config.await_count == int(paired and site == "site")
    assert cloud.await_count == int(not (paired and site == "site"))


@pytest.mark.asyncio
async def test_unreachable_local_settings_uses_cloud_without_telemetry_admission():
    ns, runtime = settings_context()
    transport = SimpleNamespace(write_config=AsyncMock(side_effect=ns["PowerwallUnreachableError"]("offline")))
    ns["get_local_transport"] = lambda h, e: transport
    cloud = AsyncMock(return_value=True)
    assert await ns["dispatch_powerwall_setting"](
        ns["hass"], ns["entry"], site_id="site", updates={"default_real_mode": "autonomous"},
        cloud_call=cloud, is_current=lambda: True, deadline=time.monotonic() + 5,
        operation_id=(1, 1), label="mode")
    cloud.assert_awaited_once()
    # Another prerequisite of the same operation does not probe the failed LAN again.
    assert await ns["dispatch_powerwall_setting"](
        ns["hass"], ns["entry"], site_id="site", updates={"default_real_mode": "autonomous"},
        cloud_call=cloud, is_current=lambda: True, deadline=time.monotonic() + 5,
        operation_id=(1, 1), label="mode")
    transport.write_config.assert_awaited_once()


@pytest.mark.asyncio
async def test_superseded_local_transaction_never_starts_cloud_fallback():
    current = True
    async def write(din, updates, *, before_write, **kwargs):
        nonlocal current
        current = False
        before_write()
    ns, _ = settings_context(SimpleNamespace(write_config=write))
    cloud = AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        await ns["dispatch_powerwall_setting"](
            ns["hass"], ns["entry"], site_id="site", updates={"default_real_mode": "autonomous"},
            cloud_call=cloud, is_current=lambda: current, deadline=time.monotonic() + 5, label="mode")
    cloud.assert_not_awaited()


@pytest.mark.asyncio
async def test_cloud_fallback_invalidates_only_setting_evidence():
    ns, runtime = settings_context()
    snapshot = SimpleNamespace(grid_charging_enabled=True, soc=54, observed_monotonic=123)
    runtime["powerwall_local"] = {"coordinator": SimpleNamespace(data=snapshot)}
    assert await ns["dispatch_powerwall_setting"](
        ns["hass"], ns["entry"], site_id="site",
        updates={"site_info.disallow_charge_from_grid_with_solar_installed": True},
        cloud_call=AsyncMock(return_value=True), is_current=lambda: True,
        deadline=time.monotonic() + 5, label="grid")
    assert snapshot.grid_charging_enabled is None
    assert snapshot.soc == 54 and snapshot.observed_monotonic == 123


@pytest.mark.asyncio
async def test_entry_storage_serializes_competing_field_merges():
    saved = {"force_mode_state": {"owner": "old"}}
    started, release = asyncio.Event(), asyncio.Event()
    reads = 0
    async def load():
        nonlocal reads
        reads += 1
        value = dict(saved)
        if reads == 1:
            started.set()
            await release.wait()
        return value
    async def save(value):
        saved.clear()
        saved.update(value)
    store = SimpleNamespace(async_load=load, async_save=save)
    update = load_function(ROOT / "__init__.py", "_update_powerwall_store", dict(asyncio=asyncio))
    first = asyncio.create_task(update(store, {"cached_export_rule": "pv_only"}))
    await started.wait()
    second = asyncio.create_task(update(store, {"force_mode_state": {"owner": "new"}}))
    release.set()
    await asyncio.gather(first, second)
    assert saved == {"cached_export_rule": "pv_only", "force_mode_state": {"owner": "new"}}


@pytest.mark.asyncio
async def test_stale_restore_cannot_clear_newer_pending_state_during_load():
    current = True
    saved = {"pending_tesla_restore": {"owner": "new"}}
    async def load():
        nonlocal current
        current = False
        return saved
    store = SimpleNamespace(async_load=load, async_save=AsyncMock())
    update = load_function(ROOT / "__init__.py", "_update_powerwall_store", dict(asyncio=asyncio))
    assert not await update(store, {"pending_tesla_restore": None}, is_current=lambda: current)
    store.async_save.assert_not_awaited()
    assert saved["pending_tesla_restore"] == {"owner": "new"}


@pytest.mark.asyncio
@pytest.mark.parametrize("setting,value,config", [
    ("operation_mode", "autonomous", {"default_real_mode": "autonomous"}),
    ("grid_charging", False, {"site_info": {"disallow_charge_from_grid_with_solar_installed": True}}),
])
async def test_actual_tariff_prerequisite_helper_uses_paired_local_readback(setting, value, config):
    transport = SimpleNamespace(write_config=AsyncMock(return_value=True),
                                read_config=AsyncMock(return_value=config))
    ns, _ = settings_context(transport)
    cloud = AsyncMock(side_effect=AssertionError("confirmed local setting must not write cloud"))
    ns.update(_command_generation=[0], _tesla_charge_kick_generation=[0], _tesla_operation_generation=[0],
              _record_tesla_settings_submission=AsyncMock(), async_get_clientsession=lambda h: object(),
              _tesla_force_set_operation_mode=cloud, async_set_tesla_grid_charging_confirmed=cloud,
              CONF_FLEET_API_BASE_URL="base", GRID_CONFIRMATION_POLL_OFFSETS=(0.5, 1.5),
              get_tesla_api_base_url=lambda *args: "https://tesla.test")
    fn = load_function(ROOT / "__init__.py", "_tesla_force_apply_" + setting, ns, strip_imports=True)
    result = await fn([("site", "token", "fleet")], value, reason="set grid charging" if setting == "grid_charging" else "force charge")
    assert result["confirmed_sites"] == ["site"]
    assert not result["failed_sites"] and not result["accepted_sites"]
    cloud.assert_not_awaited()


@pytest.mark.asyncio
async def test_cloud_mode_guard_wraps_the_actuator_instead_of_a_dummy_callback():
    session = SimpleNamespace(post=AsyncMock())
    current = True
    async def guard(writer):
        nonlocal current
        current = False
        return await writer()
    ns = dict(asyncio=asyncio, aiohttp=aiohttp, _LOGGER=logging.getLogger(__name__))
    load_function(ROOT / "__init__.py", "_run_optional_write_guard", ns)
    fn = load_function(ROOT / "__init__.py", "_tesla_force_set_operation_mode", ns, strip_imports=True)
    with pytest.raises(asyncio.CancelledError):
        await fn(session, "https://tesla.test", "site", {}, "autonomous", reason="test",
                 guard_write=guard, is_current=lambda: current)
    session.post.assert_not_called()


@pytest.mark.asyncio
async def test_transport_rejects_a_read_reply_as_update_acknowledgement():
    import json
    from test_powerwall_v1r_teslemetry_parity import _transport_without_key, transport_pb2
    read = transport_pb2.MessageEnvelope()
    read.filestore.read_file_response.file.blob = json.dumps({"default_real_mode": "backup"}).encode()
    read.filestore.read_file_response.hash = b"original-hash"
    transport = _transport_without_key()
    transport.post_v1r = AsyncMock(return_value=SimpleNamespace(ok=True, inner_bytes=read.SerializeToString(), fault_name=None))
    assert await transport.write_config("din", {"default_real_mode": "autonomous"}) is False
    assert transport.post_v1r.await_count == 2




@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["idle", "restored", "pending", "superseded"])
async def test_manual_setting_waits_for_owned_restore_and_retains_its_request(outcome):
    ns, runtime = settings_context()
    charge = {"active": outcome != "idle"}
    kicks = [0]
    def supersede(*args, **kwargs):
        kicks[0] += 1
        return kicks[0]
    async def restore(call):
        assert call.data["_confirm_restore"] is True
        if outcome == "restored":
            charge["active"] = False
        if outcome == "superseded":
            kicks[0] += 1
    restore = AsyncMock(side_effect=restore)
    ns.update(_supersede_tesla_charge_kick=supersede, _command_generation=[0],
              _tesla_charge_kick_generation=kicks, _tesla_operation_generation=[0],
              force_charge_state=charge, force_discharge_state={"active": False},
              hold_soc_state={"active": False}, _local_powerwall_generation_current=lambda: True,
              _stop_local_powerwall_dispatch=AsyncMock(return_value=False),
              handle_restore_normal=restore, SERVICE_RESTORE_NORMAL="restore_normal",
              ServiceCall=lambda domain, service, data, **kwargs: SimpleNamespace(data=data),
              HomeAssistantError=RuntimeError)
    fn = load_function(ROOT / "__init__.py", "_prepare_tesla_setting_change", ns)
    if outcome == "pending":
        with pytest.raises(RuntimeError, match="restoration remains pending"):
            await fn(SimpleNamespace())
    elif outcome == "superseded":
        with pytest.raises(asyncio.CancelledError):
            await fn(SimpleNamespace())
    else:
        assert await fn(SimpleNamespace()) == (0, 1, 0)
    assert restore.await_count == int(outcome != "idle")


def staged_transport(write_result=True, *, submitted=None, release=None, error=None):
    async def write(din, updates, *, before_write, before_submit):
        await before_submit(int(time.time()) + 1)
        before_write()
        if submitted:
            submitted.set()
        if release:
            await release.wait()
        if error:
            raise error
        return write_result
    return SimpleNamespace(write_config=write, read_config=AsyncMock(
        return_value={"default_real_mode": "autonomous"}))


@pytest.mark.asyncio
async def test_late_signed_ack_blocks_contrary_owner_until_actual_completion():
    submitted, release = asyncio.Event(), asyncio.Event()
    ns, runtime = settings_context(staged_transport(submitted=submitted, release=release))
    durable = {}
    async def persist(record, *, previous=None):
        assert durable.get("pending") == previous
        durable["pending"] = record
    dispatch = ns["dispatch_powerwall_setting"]
    first_cloud = AsyncMock(return_value=True)
    first = asyncio.create_task(dispatch(ns["hass"], ns["entry"], site_id="site",
        updates={"default_real_mode": "autonomous"}, cloud_call=first_cloud,
        is_current=lambda: True, before_submit=persist, deadline=time.monotonic()+.03, label="first"))
    await submitted.wait()
    assert not await first  # Whole request expires; actual RPC is still tracked.
    assert durable["pending"] and runtime["powerwall_settings_submissions"]
    contrary = AsyncMock(return_value=True)
    assert not await dispatch(ns["hass"], ns["entry"], site_id="site",
        updates={"default_real_mode": "backup"}, cloud_call=contrary,
        is_current=lambda: True, before_submit=persist, deadline=time.monotonic()+.01, label="second")
    contrary.assert_not_awaited()
    release.set()
    await asyncio.gather(*runtime["powerwall_settings_submissions"])
    assert durable["pending"] is None and runtime["powerwall_settings_pending"] is None
    assert await dispatch(ns["hass"], ns["entry"], site_id="other",
        updates={"default_real_mode": "backup"}, cloud_call=contrary,
        is_current=lambda: True, before_submit=persist, deadline=time.monotonic()+1, label="third")


@pytest.mark.asyncio
async def test_lost_signed_response_retains_obligation_across_reload_and_expiry():
    ns, runtime = settings_context()
    ns["get_local_transport"] = lambda *a: staged_transport(
        error=ns["PowerwallUnreachableError"]("response lost"))
    saved = {}
    async def persist(record, *, previous=None):
        assert saved.get("pending") == previous
        saved["pending"] = record
    cloud = AsyncMock(return_value=True)
    assert await ns["dispatch_powerwall_setting"](ns["hass"], ns["entry"], site_id="site",
        updates={"default_real_mode": "autonomous"}, cloud_call=cloud,
        before_submit=persist, is_current=lambda: True, deadline=time.monotonic()+1, label="lost")
    assert saved["pending"] is runtime["powerwall_settings_pending"]
    # Even a matching cloud readback and an expired signature do not prove a
    # queued FileStore update has finished. A new setup must retain that fence.
    ns2, runtime2 = settings_context(paired=False)
    runtime2["powerwall_settings_pending"] = {**saved["pending"], "expires_at": 0}
    contrary = AsyncMock(return_value=True)
    assert not await ns2["dispatch_powerwall_setting"](ns2["hass"], ns2["entry"], site_id="site",
        updates={"default_real_mode": "backup"}, cloud_call=contrary,
        is_current=lambda: True, deadline=time.monotonic()+1, label="reload")
    contrary.assert_not_awaited()


@pytest.mark.asyncio
async def test_permission_loss_at_actual_send_is_failure_and_clears_unsent_record():
    allowed = True
    ns, runtime = settings_context()
    async def write(din, updates, *, before_write, before_submit):
        nonlocal allowed
        await before_submit(int(time.time())+1)
        allowed = False
        before_write()
        raise AssertionError("must not submit after policy changes")
    ns["get_local_transport"] = lambda *a: SimpleNamespace(write_config=write)
    cloud = AsyncMock()
    assert not await ns["dispatch_powerwall_setting"](ns["hass"], ns["entry"], site_id="site",
        updates={"default_real_mode": "autonomous"}, cloud_call=cloud,
        is_current=lambda: True, is_allowed=lambda: allowed,
        deadline=time.monotonic()+1, label="policy")
    assert runtime["powerwall_settings_pending"] is None
    cloud.assert_not_awaited()


@pytest.mark.asyncio
async def test_queue_wait_is_inside_deadline_and_queued_owner_is_tracked():
    ns, runtime = settings_context(paired=False)
    lock = runtime.setdefault("powerwall_settings_lock", asyncio.Lock())
    await lock.acquire()
    cloud = AsyncMock()
    task = asyncio.create_task(ns["dispatch_powerwall_setting"](ns["hass"], ns["entry"], site_id="site",
        updates={"default_real_mode": "backup"}, cloud_call=cloud,
        is_current=lambda: True, deadline=time.monotonic()+.03, label="queued"))
    await asyncio.sleep(0)
    assert task in runtime["powerwall_settings_tasks"]
    assert not await asyncio.wait_for(task, .2)
    assert not runtime["powerwall_settings_tasks"]
    cloud.assert_not_awaited()
    lock.release()


@pytest.mark.asyncio
async def test_queued_owner_cancellation_does_not_acquire_or_write_later():
    ns, runtime = settings_context(paired=False)
    lock = runtime.setdefault("powerwall_settings_lock", asyncio.Lock())
    await lock.acquire()
    cloud = AsyncMock()
    task = asyncio.create_task(ns["dispatch_powerwall_setting"](ns["hass"], ns["entry"], site_id="site",
        updates={"default_real_mode": "backup"}, cloud_call=cloud,
        is_current=lambda: True, deadline=time.monotonic()+1, label="queued"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    lock.release()
    assert not runtime["powerwall_settings_tasks"]
    cloud.assert_not_awaited()


@pytest.mark.asyncio
async def test_replacement_restore_persists_basis_before_first_actuator():
    # Execute the actual shared Store helper with the restore handler's two
    # save phases. A superseding restore must wait for the terminal save and
    # reinstall its basis before it can mutate hardware.
    update = load_function(ROOT / "__init__.py", "_update_powerwall_store", dict(asyncio=asyncio))
    started, release = asyncio.Event(), asyncio.Event()
    saved = {"force_mode_state": {"root": "backup"}}
    generation = [1]
    async def save(value):
        if value.get("force_mode_state") is None:
            started.set()
            await release.wait()
        saved.clear()
        saved.update(value)
    store = SimpleNamespace(async_load=AsyncMock(side_effect=lambda: dict(saved)), async_save=save)
    old = asyncio.create_task(update(store, {"force_mode_state": None, "pending_tesla_restore": None},
        is_current=lambda: generation[0] == 1))
    await started.wait()
    generation[0] = 2
    actuator = AsyncMock()
    async def replacement():
        assert await update(store, {"force_mode_state": {"root": "backup"}},
                            is_current=lambda: generation[0] == 2)
        await actuator()
    newer = asyncio.create_task(replacement())
    await asyncio.sleep(0)
    actuator.assert_not_awaited()
    release.set()
    assert not await old
    await newer
    assert saved["force_mode_state"] == {"root": "backup"}
    actuator.assert_awaited_once()
    source = (ROOT / "__init__.py").read_text()
    restore = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.AsyncFunctionDef)
                   and n.name == "handle_restore_normal")
    body = ast.get_source_segment(source, restore)
    assert body.index("if not await persist_force_mode_state():") < body.index("handoff_result = await _tesla_force_apply_operation_mode")


@pytest.mark.asyncio
async def test_coordinator_reconciles_setting_after_its_final_await():
    ns, runtime = settings_context()
    snap = SimpleNamespace(operation_mode="backup", observed_monotonic=time.monotonic()-10, soc=42)
    started, release = asyncio.Event(), asyncio.Event()
    async def alert(snapshot):
        started.set()
        await release.wait()
    coordinator = SimpleNamespace(hass=ns["hass"], _entry_id="entry",
        _client=SimpleNamespace(local_access_enabled=True, get_snapshot=AsyncMock(return_value=snap)),
        _schedule_v1r_diagnostics_if_due=lambda: None, _discard_unverified_backup_reserve=lambda s: None,
        _sync_calibration_alert=alert, _consecutive_failures=0)
    ns.update(POWERWALL_LOCAL_SNAPSHOT_TIMEOUT=1, _CLOUD_FALLBACK_PENDING_KEY="pending_cloud",
              TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS=30)
    fn = load_function(ROOT / "powerwall_local/coordinator.py", "_async_update_data", ns)
    poll = asyncio.create_task(fn(coordinator))
    await started.wait()
    ns["_record_setting_observation"](runtime, {"default_real_mode": "autonomous"}, "local")
    release.set()
    assert (await poll).operation_mode == "autonomous" and snap.soc == 42


@pytest.mark.parametrize("source,age,expected", [("local", 0, False), ("cloud", 0, None), ("unknown", 0, None), ("local", 31, None)])
def test_settings_freshness_does_not_depend_on_dcq(source, age, expected):
    ns, runtime = settings_context()
    runtime["powerwall_local"] = {"coordinator": SimpleNamespace(reachable=False, last_success_monotonic=0)}
    runtime["powerwall_setting_observations"] = {"grid_charging_enabled": {
        "source": source, "at": time.monotonic()-age, "value": False}}
    getter = load_function(ROOT / "powerwall_host.py", "confirmed_local_powerwall_setting", ns)
    assert getter(ns["hass"], ns["entry"], "grid_charging_enabled", max_age=30) is expected


class HttpReply:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    async def json(self):
        return self.payload


@pytest.mark.asyncio
@pytest.mark.parametrize("write_payload,read_payload,expected", [
    ({"response": {"result": False, "reason": "denied"}}, {}, False),
    ({"response": {"result": True}}, {"response": {"components": {"customer_preferred_export_rule": "pv_only"}}}, True),
    ({"response": {"result": True}}, {"response": ["pv_only"]}, False),
    ({"response": {"result": True}}, {"response": {"components": ["pv_only"]}}, False),
])
async def test_actual_export_helper_composes_http_result_and_readback(write_payload, read_payload, expected):
    ns, _ = settings_context(paired=False)
    session = SimpleNamespace(post=lambda *a, **kw: HttpReply(write_payload),
                              get=lambda *a, **kw: HttpReply(read_payload))
    ns.update(_command_generation=[0], _tesla_charge_kick_generation=[0], aiohttp=aiohttp,
              _record_tesla_settings_submission=AsyncMock(), async_get_clientsession=lambda h: session,
              CONF_FLEET_API_BASE_URL="base", get_tesla_api_base_url=lambda *a: "https://tesla.test")
    load_function(ROOT / "__init__.py", "_tesla_grid_export_write_result", ns)
    fn = load_function(ROOT / "__init__.py", "_tesla_force_apply_export_rule", ns, strip_imports=True)
    assert await fn([("site", "token", "fleet")], "pv_only", reason="restore") is expected


@pytest.mark.asyncio
async def test_export_restore_policy_loss_returns_failed_instead_of_cancelling_owner():
    ns, runtime = settings_context(paired=False)
    runtime["network_envelope_manager"] = SimpleNamespace(snapshot=SimpleNamespace(mode="active"))
    session = SimpleNamespace(post=AsyncMock())
    ns.update(_command_generation=[0], _tesla_charge_kick_generation=[0], aiohttp=aiohttp,
              _record_tesla_settings_submission=AsyncMock(), async_get_clientsession=lambda h: session,
              CONF_FLEET_API_BASE_URL="base", get_tesla_api_base_url=lambda *a: "https://tesla.test")
    fn = load_function(ROOT / "__init__.py", "_tesla_force_apply_export_rule", ns, strip_imports=True)
    assert not await fn([("site", "token", "fleet")], "battery_ok", reason="restore")
    session.post.assert_not_called()


@pytest.mark.parametrize("filename,classname,field,value,property_name,options", [
    ("switch.py", "GridChargingSwitch", "grid_charging_enabled", False, "is_on", None),
    ("select.py", "TeslaOperationModeSelect", "operation_mode", "backup", "current_option", ("backup", "autonomous", "self_consumption")),
    ("select.py", "TeslaGridExportRuleSelect", "grid_export_rule", "pv_only", "current_option", ("never", "pv_only", "battery_ok")),
])
def test_actual_entity_getters_prefer_independent_readback_to_conflicting_cloud(filename, classname, field, value, property_name, options):
    ns, runtime = settings_context()
    runtime["powerwall_setting_observations"] = {field: {
        "source": "local", "at": time.monotonic(), "value": value}}
    load_function(ROOT / "powerwall_host.py", "confirmed_local_powerwall_setting", ns)
    ns.update(TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS=30)
    tree = ast.parse((ROOT / filename).read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == classname)
    getter = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == property_name)
    getter.decorator_list = []
    exec(compile(ast.fix_missing_locations(ast.Module(body=[getter], type_ignores=[])), filename, "exec"), ns)
    entity = SimpleNamespace(hass=ns["hass"], _entry=ns["entry"], _OPTIONS=options,
        _tesla_coord=lambda: SimpleNamespace(_site_info_cache={"default_real_mode": "autonomous",
            "components": {"disallow_charge_from_grid_with_solar_installed": False,
                           "customer_preferred_export_rule": "battery_ok"}}))
    assert ns[property_name](entity) == value


def restore_context(*, mode="backup", direction="discharge", envelope="off"):
    """Run the complete restore closure with only external actuators faked."""
    from datetime import datetime, timedelta, timezone
    from unittest.mock import Mock
    ns, runtime = settings_context(paired=False)
    tree = ast.parse((ROOT / "__init__.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id.startswith(("CONF_", "BATTERY_SYSTEM_", "SERVICE_")):
            ns[node.id] = node.id
    baseline = dict(expires_at=None, saved_tariff={"name": "root"}, saved_operation_mode=mode,
                    saved_backup_reserve=None, saved_grid_charging_enabled=None)
    charge = dict(baseline, active=direction == "charge")
    discharge = dict(baseline, active=direction == "discharge", saved_export_rule="pv_only")
    if direction == "charge":
        discharge.update(saved_tariff=None, saved_operation_mode=None, saved_export_rule=None)
    else:
        charge.update(saved_tariff=None, saved_operation_mode=None)
    runtime.update(tesla_coordinator=SimpleNamespace(data={}),
                   optimization_coordinator=SimpleNamespace(_enabled=True),
                   network_envelope_manager=SimpleNamespace(snapshot=SimpleNamespace(mode=envelope)))
    ns["entry"].options[ns["CONF_ELECTRICITY_PROVIDER"]] = "other"
    saved = {}
    store = SimpleNamespace(async_load=AsyncMock(side_effect=lambda: dict(saved)),
                            async_save=AsyncMock(side_effect=lambda value: saved.update(value)))
    runtime["store"] = store
    confirmed = {"confirmed_sites": ["site"], "failed_sites": []}
    ns.update(store=store, force_charge_state=charge, force_discharge_state=discharge,
        hold_soc_state={"active": False}, self_consumption_state={"active": False},
        tesla_grid_charging_preferences={}, _command_generation=[0], _tesla_reserve_generation=[0],
        _tesla_charge_kick_generation=[0], _tesla_operation_generation=[0],
        active_battery_system=ns["BATTERY_SYSTEM_TESLA"],
        _control_call_source=lambda call: "force_cleanup", _stop_local_powerwall_dispatch=AsyncMock(return_value=False),
        _sigenergy_restore_native_control=lambda call: False, _monitoring_mode_should_block_control=lambda call: False,
        _is_monitoring_mode=lambda: False, _get_neovolt_entry_ids=lambda *args: [],
        _uses_native_battery_integration=lambda coordinator: False,
        _get_tesla_site_configs=lambda *args: [("site", "token", "fleet")],
        _select_restorable_tesla_tariff=lambda *values: next((value for value in values if value), None),
        _configured_restorable_tesla_tariff=lambda: None, _cached_restorable_tesla_tariff=lambda: None,
        get_tesla_api_base_url=lambda *args: "https://tesla.test", async_get_clientsession=lambda hass: object(),
        send_tariff_to_tesla=AsyncMock(return_value=True),
        _tesla_force_apply_operation_mode=AsyncMock(return_value=confirmed),
        _tesla_force_apply_grid_charging=AsyncMock(return_value=confirmed),
        _tesla_force_apply_export_rule=AsyncMock(return_value=True),
        _demand_grid_charging_protection_active=lambda: False, _remember_tesla_grid_charging_preference=lambda *args: None,
        _last_manual_projection_signature=[None], _manual_projection_signature=lambda state: "active" if state else "idle",
        _request_manual_control_replan=Mock(), async_dispatcher_send=Mock(), _coerce_force_power_w=lambda value: value,
        dt_util=SimpleNamespace(utcnow=lambda: datetime.now(timezone.utc)), datetime=datetime, timedelta=timedelta,
        async_track_point_in_utc_time=Mock(return_value=lambda: None),
        _optional_bool=lambda value: value if type(value) is bool else None, _send_expo_push=AsyncMock(),
        HomeAssistantError=RuntimeError, ServiceCall=lambda domain, service, data, **kwargs: SimpleNamespace(data=data, **kwargs))
    for name in ("_update_powerwall_store", "_cancel_all_force_timers", "persist_force_mode_state",
                 "persist_pending_tesla_restore", "_tesla_force_result_all_confirmed", "handle_restore_normal",
                 "update_cached_export_rule", "_supersede_tesla_charge_kick", "_prepare_tesla_setting_change"):
        load_function(ROOT / "__init__.py", name, ns, strip_imports=True)
    ns["hass"].services = SimpleNamespace(async_call=AsyncMock())
    call = SimpleNamespace(context=SimpleNamespace(user_id=None, parent_id=None), data={"_confirm_restore": True})
    return ns, runtime, saved, call


@pytest.mark.asyncio
async def test_complete_restore_retains_owner_and_retry_when_export_policy_denies():
    ns, runtime, saved, call = restore_context(envelope="active")
    ns.update(aiohttp=aiohttp, _record_tesla_settings_submission=AsyncMock())
    load_function(ROOT / "__init__.py", "_tesla_force_apply_export_rule", ns, strip_imports=True)
    result = await ns["handle_restore_normal"](call)
    assert result["success"] is False
    assert ns["force_discharge_state"]["active"] is True
    assert saved["force_mode_state"]["saved_operation_mode"] == "backup"
    assert saved["pending_tesla_restore"] == runtime["pending_tesla_restore"]
    ns["async_track_point_in_utc_time"].assert_called_once()
    # A retry belongs to this generation and cannot restore over a new owner.
    callback = ns["async_track_point_in_utc_time"].call_args.args[1]
    ns["_command_generation"][0] += 1
    await callback(None)
    ns["hass"].services.async_call.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["backup", "autonomous"])
async def test_real_manual_handoff_restores_captured_mode_before_new_setting(mode):
    ns, runtime, saved, call = restore_context(mode=mode, direction="charge")
    generation = await ns["_prepare_tesla_setting_change"](call)
    requested_modes = [item.args[1] for item in ns["_tesla_force_apply_operation_mode"].await_args_list]
    assert requested_modes == ["self_consumption", mode]
    assert generation == (1, 1, 0)
    assert saved["force_mode_state"] is None and runtime["pending_tesla_restore"] is None
    assert not ns["force_charge_state"]["active"]
    assert ns["_last_manual_projection_signature"] == ["idle"]
    assert ns["_request_manual_control_replan"].call_count == 2


@pytest.mark.asyncio
async def test_complete_restore_retires_export_baseline_and_publishes_cache():
    ns, runtime, saved, call = restore_context()
    assert (await ns["handle_restore_normal"](call))["success"] is True
    assert saved["cached_export_rule"] == runtime["cached_export_rule"] == "pv_only"
    assert ns["force_discharge_state"]["saved_export_rule"] is None
    assert saved["force_mode_state"] is None and saved["pending_tesla_restore"] is None
    ns["async_dispatcher_send"].assert_any_call(ns["hass"], "power_sync_curtailment_updated_entry")


@pytest.mark.asyncio
async def test_complete_replacement_restore_saves_root_before_actuation_during_old_terminal_save():
    ns, runtime, saved, call = restore_context()
    started, release = asyncio.Event(), asyncio.Event()
    async def save(value):
        if value.get("force_mode_state") is None:
            started.set()
            await release.wait()
        saved.clear()
        saved.update(value)
    ns["store"].async_save.side_effect = save
    old = asyncio.create_task(ns["handle_restore_normal"](call))
    await started.wait()
    assert ns["_tesla_force_apply_operation_mode"].await_count == 2
    async def new_mode(*args, **kwargs):
        assert saved["force_mode_state"]["saved_operation_mode"] == "backup"
        # Simulate process loss at the replacement's first actuator boundary.
        raise asyncio.CancelledError
    ns["_tesla_force_apply_operation_mode"].side_effect = new_mode
    new = asyncio.create_task(ns["handle_restore_normal"](call))
    await asyncio.sleep(0)
    assert ns["_command_generation"][0] == 2
    assert ns["_tesla_force_apply_operation_mode"].await_count == 2
    release.set()
    assert await old is None
    with pytest.raises(asyncio.CancelledError):
        await new
    assert ns["force_discharge_state"]["active"] is True
    assert saved["force_mode_state"]["saved_export_rule"] == "pv_only"


@pytest.mark.asyncio
async def test_secondary_site_cloud_result_cannot_replace_primary_gateway_evidence():
    ns, runtime = settings_context()
    primary = {"source": "local", "at": time.monotonic(), "value": "backup"}
    runtime["powerwall_setting_observations"] = {"operation_mode": primary}
    assert await ns["dispatch_powerwall_setting"](ns["hass"], ns["entry"], site_id="secondary",
        updates={"default_real_mode": "autonomous"}, cloud_call=AsyncMock(return_value=True),
        is_current=lambda: True, deadline=time.monotonic()+1, label="secondary")
    assert runtime["powerwall_setting_observations"]["operation_mode"] is primary


@pytest.mark.asyncio
async def test_successful_discharge_tail_persists_accepted_phase_before_restart():
    from datetime import datetime, timedelta, timezone
    ns, runtime, saved, call = restore_context()
    source = (ROOT / "__init__.py").read_text()
    handler = next(node for node in ast.walk(ast.parse(source))
                   if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_force_discharge")
    # Execute the actual successful commit tail, including the real persistence
    # helper. Hardware preparation has already succeeded at this boundary.
    tail = None
    for node in ast.walk(handler):
        if isinstance(node, ast.If):
            for index, statement in enumerate(node.body):
                if isinstance(statement, ast.Assign) and ast.unparse(statement) == "force_discharge_state['command_status'] = 'accepted'":
                    tail = node.body[index:]
    assert tail is not None
    wrapper = ast.AsyncFunctionDef(name="commit", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
                                  body=tail, decorator_list=[])
    ns.update(duration=30, _restore_gen=0, call=call)
    ns["force_discharge_state"].update(command_status="awaiting_grid_confirmation",
        expires_at=datetime.now(timezone.utc)+timedelta(minutes=30), cancel_expiry_timer=None)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])), "discharge_commit", "exec"), ns)
    assert (await ns["commit"]())["success"] is True
    assert saved["force_mode_state"]["command_status"] == "accepted"
    assert datetime.fromisoformat(saved["force_mode_state"]["expires_at"]) > datetime.now(timezone.utc)
    requests = await hydrate_payload(ns, saved["force_mode_state"])
    assert requests == [ns["SERVICE_FORCE_DISCHARGE"]]


@pytest.mark.asyncio
async def test_new_dcq_with_missing_config_cannot_discard_recent_setting_readback():
    ns, runtime = settings_context()
    ns["_record_setting_observation"](runtime, {"default_real_mode": "autonomous"}, "local")
    snap = SimpleNamespace(operation_mode=None, observed_monotonic=time.monotonic(), soc=42)
    coordinator = SimpleNamespace(hass=ns["hass"], _entry_id="entry",
        _client=SimpleNamespace(local_access_enabled=True, get_snapshot=AsyncMock(return_value=snap)),
        _schedule_v1r_diagnostics_if_due=lambda: None, _discard_unverified_backup_reserve=lambda s: None,
        _sync_calibration_alert=AsyncMock(), _consecutive_failures=0)
    ns.update(POWERWALL_LOCAL_SNAPSHOT_TIMEOUT=1, _CLOUD_FALLBACK_PENDING_KEY="pending_cloud",
              TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS=30)
    fn = load_function(ROOT / "powerwall_local/coordinator.py", "_async_update_data", ns)
    assert (await fn(coordinator)).operation_mode is None
    getter = load_function(ROOT / "powerwall_host.py", "confirmed_local_powerwall_setting", ns)
    assert getter(ns["hass"], ns["entry"], "operation_mode", max_age=30) == "autonomous"
    # A subsequent actual contrary config observation retires the overlay.
    snap.operation_mode = "backup"
    snap.observed_monotonic = time.monotonic()
    assert (await fn(coordinator)).operation_mode == "backup"
    assert "operation_mode" not in runtime["powerwall_setting_observations"]


def wire_transport(config, *, fault=None):
    """Real signing and response parsing, with only the HTTP boundary faked."""
    import json
    from test_powerwall_v1r_teslemetry_parity import transport_mod, transport_pb2, _private_key_pem
    signed = transport_mod.signed_message_pb2
    read = transport_pb2.MessageEnvelope()
    read.filestore.read_file_response.file.blob = json.dumps(config).encode()
    read.filestore.read_file_response.hash = b"original"
    read_outer = signed.RoutableMessage(protobuf_message_as_bytes=read.SerializeToString())
    rejected = signed.RoutableMessage()
    if fault:
        rejected.signed_message_status.message_fault = getattr(signed, fault)
    responses = [read_outer.SerializeToString(), rejected.SerializeToString()]
    class Session:
        def __init__(self):
            self.posts = []
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        def post(self, url, **kwargs):
            self.posts.append(kwargs["data"])
            response = HttpReply(None)
            response.read = AsyncMock(return_value=responses[len(self.posts)-1])
            return response
    session = Session()
    transport = transport_mod.TEDAPIv1rTransport("gateway.test", _private_key_pem(), din="din")
    transport._session = AsyncMock(return_value=session)
    return transport, session, transport_mod


def use_wire_exception_types(ns, transport_module):
    import importlib
    exceptions = importlib.import_module(transport_module.__package__ + ".exceptions")
    for name in ("PowerwallLocalError", "PowerwallUnreachableError", "PowerwallSignatureError",
                 "PowerwallAuthError", "PowerwallWriteRejected"):
        ns[name] = getattr(exceptions, name)
    ns["_LOCAL_FALLBACK_EXC"] = (exceptions.PowerwallLocalError, asyncio.TimeoutError)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault,retained", [
    ("MESSAGEFAULT_ERROR_BAD_PARAMETER", False),
    ("MESSAGEFAULT_ERROR_TIMEOUT", True),
    ("MESSAGEFAULT_ERROR_INTERNAL", True),
])
async def test_real_transport_negative_receipt_distinguishes_rejection_from_unknown(fault, retained):
    transport, session, mod = wire_transport({"default_real_mode": "backup"}, fault=fault)
    ns, runtime, saved, call = restore_context()
    use_wire_exception_types(ns, mod)
    ns["get_local_transport"] = lambda *args: transport
    load_function(ROOT / "__init__.py", "_record_tesla_settings_submission", ns)
    cloud = AsyncMock(return_value=True)
    assert await ns["dispatch_powerwall_setting"](ns["hass"], ns["entry"], site_id="site",
        updates={"default_real_mode": "autonomous"}, cloud_call=cloud,
        before_submit=ns["_record_tesla_settings_submission"], is_current=lambda: True,
        deadline=time.monotonic()+1, label="negative receipt")
    assert len(session.posts) == 2
    assert bool(saved.get("tesla_settings_pending")) is retained
    # Simulate reload using only the durable record; a definite rejection must
    # not leave a false recovery obligation blocking cloud-only control.
    ns2, runtime2 = settings_context(paired=False)
    runtime2["powerwall_settings_pending"] = saved.get("tesla_settings_pending")
    contrary = AsyncMock(return_value=True)
    assert await ns2["dispatch_powerwall_setting"](ns2["hass"], ns2["entry"], site_id="site",
        updates={"default_real_mode": "backup"}, cloud_call=contrary,
        is_current=lambda: True, deadline=time.monotonic()+1, label="after reload") is not retained
    assert contrary.await_count == int(not retained)


@pytest.mark.asyncio
async def test_actual_expiry_rejection_before_http_does_not_retain_submission(monkeypatch):
    transport, session, mod = wire_transport({"default_real_mode": "backup"})
    ns, runtime, saved, call = restore_context()
    use_wire_exception_types(ns, mod)
    ns["get_local_transport"] = lambda *args: transport
    load_function(ROOT / "__init__.py", "_record_tesla_settings_submission", ns)
    clock = [100.0]
    monkeypatch.setattr(mod.time, "time", lambda: clock[0])
    async def persist(record, *, previous=None):
        await ns["_record_tesla_settings_submission"](record, previous=previous)
        if record:
            clock[0] = 113.0  # Signed at 100 with a 12-second TTL.
    assert await ns["dispatch_powerwall_setting"](ns["hass"], ns["entry"], site_id="site",
        updates={"default_real_mode": "autonomous"}, cloud_call=AsyncMock(return_value=True),
        before_submit=persist, is_current=lambda: True, deadline=time.monotonic()+1, label="expired")
    assert len(session.posts) == 1  # Only the RMW read, no update HTTP POST.
    assert saved["tesla_settings_pending"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("config", [[True], {"site_info": [True]}])
async def test_malformed_local_config_preserves_cloud_baseline_and_fallback(config):
    transport, session, mod = wire_transport(config)
    ns, runtime = settings_context(transport)
    use_wire_exception_types(ns, mod)
    cloud = AsyncMock(return_value=True)
    assert await ns["dispatch_powerwall_setting"](ns["hass"], ns["entry"], site_id="site",
        updates={"site_info.disallow_charge_from_grid_with_solar_installed": True}, cloud_call=cloud,
        is_current=lambda: True, deadline=time.monotonic()+1, label="malformed config")
    assert len(session.posts) == 1
    assert not runtime.get("powerwall_settings_pending")
    state = {"saved_operation_mode": "backup", "saved_grid_charging_enabled": False,
             "saved_export_rule": "pv_only", "saved_states": {}}
    ns.update(force_discharge_state=state, _command_generation=[0], _tesla_charge_kick_generation=[0],
              CONF_TESLA_ENERGY_SITE_ID="tesla_energy_site_id")
    ns["get_local_transport"] = lambda *args: SimpleNamespace(read_config=AsyncMock(return_value=config))
    capture = load_function(ROOT / "__init__.py", "_capture_tesla_setting_baselines", ns, strip_imports=True)
    await capture(state, [("site", "token", "fleet")], is_current=lambda: True)
    assert state["saved_states"]["site"] == {"saved_operation_mode": "backup",
        "saved_grid_charging_enabled": False, "saved_export_rule": "pv_only"}


@pytest.mark.asyncio
async def test_unload_drains_actual_restore_terminal_save_before_replacement():
    ns, runtime, saved, call = restore_context()
    started, release = asyncio.Event(), asyncio.Event()
    async def save(value):
        if value.get("force_mode_state") is None:
            started.set()
            await release.wait()
        saved.clear()
        saved.update(value)
    ns["store"].async_save.side_effect = save
    old = asyncio.create_task(ns["handle_restore_normal"](call))
    await started.wait()
    assert old in runtime["powerwall_settings_control_tasks"]
    ns["async_dispatcher_send"].reset_mock()
    ns["_request_manual_control_replan"].reset_mock()
    tree = ast.parse((ROOT / "__init__.py").read_text())
    unload = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_unload_entry")
    # The relevant actual unload statements: fence the setup, then drain full
    # operation lifetimes. Other shutdown branches are idle at this boundary.
    fence = next(node for node in unload.body if isinstance(node, ast.Assign)
                 and "powerwall_local_dispatch_stopping" in ast.unparse(node))
    capture = next(node for node in ast.walk(unload) if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "controls" for target in node.targets))
    drain = next(node for node in ast.walk(unload) if isinstance(node, ast.If)
                 and isinstance(node.test, ast.Name) and node.test.id == "controls")
    wrapper = ast.AsyncFunctionDef(name="unload_controls", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
                                  body=[fence, capture, drain], decorator_list=[])
    ns.update(entry_data=runtime, current_task=asyncio.current_task())
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])), "unload_controls", "exec"), ns)
    replacement = {"pending_tesla_restore": {"owner": "new"}}
    async def reload():
        await ns["unload_controls"]()
        ns["hass"].data["power_sync"]["entry"] = replacement
        saved.update(force_mode_state={"owner": "new"}, pending_tesla_restore={"owner": "new"})
    reload_task = asyncio.create_task(reload())
    await asyncio.sleep(0)
    assert not reload_task.done() and ns["hass"].data["power_sync"]["entry"] is runtime
    release.set()
    assert await old is None
    await reload_task
    assert saved["force_mode_state"] == {"owner": "new"}
    assert replacement["pending_tesla_restore"] == {"owner": "new"}
    ns["async_dispatcher_send"].assert_not_called()
    ns["_request_manual_control_replan"].assert_not_called()


@pytest.mark.asyncio
async def test_actual_manual_export_cannot_publish_or_persist_override_after_supersession():
    ns, runtime, saved, call = restore_context()
    ns["force_charge_state"]["active"] = ns["force_discharge_state"]["active"] = False
    runtime["tesla_coordinator"] = None
    ns["entry"].options[ns["CONF_BATTERY_CURTAILMENT_ENABLED"]] = True
    tasks = []
    ns["hass"].async_create_task = lambda coroutine: tasks.append(asyncio.create_task(coroutine))
    ns["refresh_powerwall_local_after_settings_write"] = AsyncMock()
    started, release, newer_confirmed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def save(value):
        if value.get("cached_export_rule") == "never":
            started.set()
            await release.wait()
        saved.clear()
        saved.update(value)
    ns["store"].async_save.side_effect = save
    async def export(sites, rule, **kwargs):
        if rule == "battery_ok":
            newer_confirmed.set()
        return True
    ns["_tesla_force_apply_export_rule"].side_effect = export
    handler = load_function(ROOT / "__init__.py", "handle_set_grid_export", ns, strip_imports=True)
    old = asyncio.create_task(handler(SimpleNamespace(data={"rule": "never"})))
    await started.wait()
    new = asyncio.create_task(handler(SimpleNamespace(data={"rule": "battery_ok"})))
    await newer_confirmed.wait()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await old
    await new
    await asyncio.gather(*tasks)
    assert saved["cached_export_rule"] == runtime["cached_export_rule"] == "battery_ok"
    assert saved["manual_export_rule"] == runtime["manual_export_rule"] == "battery_ok"
    assert not any(item.args[0].get("manual_export_rule") == "never" for item in ns["store"].async_save.await_args_list)
    ns["async_dispatcher_send"].assert_called_once_with(ns["hass"], "power_sync_curtailment_updated_entry")
    ns["refresh_powerwall_local_after_settings_write"].assert_awaited_once()


@pytest.mark.asyncio
async def test_actual_manual_cloud_bounce_cannot_contradict_unresolved_local_write():
    import json
    ns, runtime, saved, call = restore_context()
    ns["force_charge_state"]["active"] = ns["force_discharge_state"]["active"] = False
    ns["entry"].data["powerwall_local_paired"] = True
    ns["CONF_TESLA_ENERGY_SITE_ID"] = "tesla_energy_site_id"
    ns["get_local_transport"] = lambda *args: staged_transport(error=ns["PowerwallUnreachableError"]("lost reply"))
    posted = []
    class Session:
        def post(self, url, **kwargs):
            posted.append(kwargs["json"]["default_real_mode"])
            reply = HttpReply({"response": {"result": True}})
            reply.text = AsyncMock(return_value=json.dumps(reply.payload))
            return reply
        def get(self, *args, **kwargs):
            return HttpReply({"response": {"default_real_mode": "backup"}})
    fake_asyncio = SimpleNamespace(**vars(asyncio))
    fake_asyncio.sleep = AsyncMock()
    ns.update(asyncio=fake_asyncio, aiohttp=aiohttp, async_get_clientsession=lambda hass: Session(),
              _record_tesla_settings_submission=AsyncMock(), _notify_api_error=AsyncMock())
    tasks = []
    ns["hass"].async_create_task = lambda coroutine: tasks.append(asyncio.create_task(coroutine))
    handler = load_function(ROOT / "__init__.py", "handle_set_operation_mode", ns, strip_imports=True)
    with pytest.raises(RuntimeError, match="Could not verify Tesla operation mode"):
        await handler(SimpleNamespace(data={"mode": "autonomous"}))
    await asyncio.gather(*tasks)
    assert posted == ["autonomous"]
    assert runtime["powerwall_settings_pending"]["updates"] == {"default_real_mode": "autonomous"}


async def hydrate_payload(ns, payload):
    from datetime import timezone
    import math
    requests = []
    async def service(domain, name, data, **kwargs):
        requests.append(name)
        if name == ns["SERVICE_RESTORE_NORMAL"]:
            ns["force_charge_state"]["active"] = False
            ns["force_discharge_state"]["active"] = False
    ns.update(persisted_force_state=payload, persisted_pending_restore=None, math=math,
              active_battery_system="tesla", BATTERY_SYSTEM_TESLA="tesla")
    ns["dt_util"].UTC = timezone.utc
    ns["hass"].services.async_call.side_effect = service
    fn = load_function(ROOT / "__init__.py", "restore_force_mode_from_persistence", ns, strip_imports=True)
    await fn()
    return requests


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,phase", [("charge", "awaiting_settings_confirmation"),
                                       ("discharge", "awaiting_grid_confirmation")])
async def test_actual_startup_cleans_interrupted_preparation_instead_of_replaying(mode, phase):
    from datetime import datetime, timedelta, timezone
    ns, runtime, saved, call = restore_context(direction=mode)
    payload = {"mode": mode, "command_status": phase, "expires_at": (datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat(),
               "source": "user", "saved_operation_mode": "backup", "saved_tariff": {"name": "root"}}
    requests = await hydrate_payload(ns, payload)
    assert requests == [ns["SERVICE_RESTORE_NORMAL"]]


@pytest.mark.asyncio
async def test_charge_persistence_keeps_distinct_existing_per_site_roots():
    from datetime import datetime, timedelta, timezone
    ns, runtime, saved, call = restore_context(direction="charge")
    roots = {"site": {"saved_operation_mode": "backup", "saved_tariff": {"name": "first"}},
             "secondary": {"saved_operation_mode": "self_consumption", "saved_tariff": {"name": "second"}}}
    ns["force_charge_state"].update(saved_states=roots, command_status="accepted",
        expires_at=datetime.now(timezone.utc)+timedelta(minutes=30))
    assert await ns["persist_force_mode_state"]()
    assert saved["force_mode_state"]["saved_states"] == roots
    await hydrate_payload(ns, saved["force_mode_state"])
    assert ns["force_charge_state"]["saved_states"] == roots


@pytest.mark.asyncio
@pytest.mark.parametrize("older_poll", [False, True])
@pytest.mark.parametrize("filename,classname,field,updates,expected,cloud_value,property_name,options", [
    ("switch.py", "GridChargingSwitch", "grid_charging_enabled", {"site_info.disallow_charge_from_grid_with_solar_installed": True}, False, True, "is_on", None),
    ("select.py", "TeslaOperationModeSelect", "operation_mode", {"default_real_mode": "backup"}, "backup", "autonomous", "current_option", ("backup", "autonomous", "self_consumption")),
    ("select.py", "TeslaGridExportRuleSelect", "grid_export_rule", {"site_info.customer_preferred_export_rule": "pv_only"}, "pv_only", "battery_ok", "current_option", ("never", "pv_only", "battery_ok")),
])
async def test_actual_getters_cannot_recover_expired_settings_from_newer_dcq(
        monkeypatch, older_poll, filename, classname, field, updates, expected, cloud_value, property_name, options):
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    ns, runtime = settings_context()
    ns["_record_setting_observation"](runtime, updates, "local")
    clock[0] = 1020.0
    acquired = 999.0 if older_poll else 1020.0
    snap = SimpleNamespace(operation_mode=None, grid_charging_enabled=None, grid_export_rule=None,
                           observed_monotonic=acquired, soc=42)
    load_function(ROOT / "powerwall_local/coordinator.py", "last_success_monotonic", ns)
    ProbeCoordinator = type("ProbeCoordinator", (SimpleNamespace,), {
        "last_success_monotonic": ns["last_success_monotonic"]})
    coordinator = ProbeCoordinator(hass=ns["hass"], _entry_id="entry", data=snap,
        _client=SimpleNamespace(local_access_enabled=True, get_snapshot=AsyncMock(return_value=snap)),
        _schedule_v1r_diagnostics_if_due=lambda: None, _discard_unverified_backup_reserve=lambda s: None,
        _sync_calibration_alert=AsyncMock(), _consecutive_failures=0)
    runtime["powerwall_local"] = {"coordinator": coordinator}
    ns.update(POWERWALL_LOCAL_SNAPSHOT_TIMEOUT=1, _CLOUD_FALLBACK_PENDING_KEY="cloud_pending",
              TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS=30, CONF_BATTERY_CURTAILMENT_ENABLED="curtail")
    load_function(ROOT / "powerwall_host.py", "confirmed_local_powerwall_setting", ns)
    load_function(ROOT / "tesla_grid_control.py", "_optional_bool", ns)
    load_function(ROOT / "tesla_grid_control.py", "tesla_grid_charging_enabled_from_site_info", ns)
    update = load_function(ROOT / "powerwall_local/coordinator.py", "_async_update_data", ns)
    coordinator.data = await update(coordinator)
    cls = next(node for node in ast.parse((ROOT / filename).read_text()).body
               if isinstance(node, ast.ClassDef) and node.name == classname)
    getter = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == property_name)
    getter.decorator_list = []
    exec(compile(ast.fix_missing_locations(ast.Module(body=[getter], type_ignores=[])), filename, "exec"), ns)
    entity = SimpleNamespace(hass=ns["hass"], _entry=ns["entry"], _OPTIONS=options, _attr_is_on=None,
        _tesla_coord=lambda: SimpleNamespace(_site_info_cache={"default_real_mode": "autonomous",
            "components": {"disallow_charge_from_grid_with_solar_installed": False,
                           "customer_preferred_export_rule": "battery_ok"}}))
    assert ns[property_name](entity) == expected
    assert snap.soc == 42 and snap.observed_monotonic == acquired
    assert getattr(snap, field) == (expected if older_poll else None)
    # DCQ is still fresh at 31s, but the independent setting is not.
    clock[0] = 1031.0
    assert clock[0] - coordinator._last_success_monotonic == 11
    assert ns["confirmed_local_powerwall_setting"](ns["hass"], ns["entry"], field, max_age=30) is None
    assert ns[property_name](entity) == cloud_value
    # A valid later local config observation can become authoritative normally.
    clock[0] = 1032.0
    snap.observed_monotonic = clock[0]
    setattr(snap, field, cloud_value)
    coordinator.data = await update(coordinator)
    assert field not in runtime["powerwall_setting_observations"]
    assert ns["confirmed_local_powerwall_setting"](ns["hass"], ns["entry"], field, max_age=30) == cloud_value
    assert ns[property_name](entity) == cloud_value
