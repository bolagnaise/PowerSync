"""Execute the actual local settings transaction with controlled gateway I/O."""
from __future__ import annotations

import ast
import asyncio
import logging
import copy
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import aiohttp

ROOT = Path(__file__).parents[1] / "custom_components/power_sync"


def load_settings_startup(namespace):
    """Run the actual storage prelude and full runtime publication from setup."""
    import builtins

    setup = next(node for node in ast.parse((ROOT / "__init__.py").read_text()).body
                 if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry")
    start = next(i for i, node in enumerate(setup.body)
                 if isinstance(node, ast.Assign) and any(
                     isinstance(target, ast.Name) and target.id == "settings_state"
                     for target in node.targets))
    stop = next(i for i, node in enumerate(setup.body[start:], start)
                if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "stored_grid_charging_preferences"
                    for target in node.targets))
    publish_start = next(i for i, node in enumerate(setup.body)
                         if isinstance(node, ast.Assign) and any(
                             isinstance(target, ast.Name) and target.id == "existing_entry_data"
                             for target in node.targets))
    publish_end = next(i for i, node in enumerate(setup.body[publish_start:], publish_start)
                       if isinstance(node, ast.Assign) and any(
                           ast.unparse(target) == "hass.data[DOMAIN][entry.entry_id]"
                           for target in node.targets))
    wrapper = ast.AsyncFunctionDef(
        name="settings_startup", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[],
                                                  kw_defaults=[], defaults=[]),
        body=setup.body[start:stop] + setup.body[publish_start:publish_end + 1],
        decorator_list=[])
    # Unrelated coordinator fields are inert. Keep the real dictionary assembly
    # so a later replacement cannot silently discard restored recovery state.
    for node in ast.walk(wrapper):
        if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                and node.id not in namespace and not hasattr(builtins, node.id)):
            namespace[node.id] = node.id if node.id.startswith("CONF_") else None
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])),
                 "settings_startup", "exec"), namespace)
    return namespace["settings_startup"]


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_data", [{}, {"power_sync": {"other": {"sentinel": True}}},
                                         {"power_sync": {"entry": {"tesla_capabilities": {"probe": True}}}}])
@pytest.mark.parametrize("pending", [None, {"id": "uncertain", "site_id": "site"},
                                     {"id": "resumed", "site_id": "site", "resume_cloud": True}])
async def test_settings_startup_publishes_recovery_state_without_preexisting_runtime(initial_data, pending):
    hass = SimpleNamespace(data=copy.deepcopy(initial_data))
    entry = SimpleNamespace(entry_id="entry", data={}, options={})
    saved = {"tesla_settings_pending": pending}
    store = SimpleNamespace(async_load=AsyncMock(return_value=saved))
    submissions = set()
    hass.data["power_sync_settings"] = {"entry": {"store": store, "submissions": submissions}}
    ns = dict(hass=hass, entry=entry, asyncio=asyncio, DOMAIN="power_sync",
              STORAGE_VERSION=1, STORAGE_KEY="settings", Store=Mock(return_value=store),
              _sync_powerwall_settings_issue=Mock())
    await load_settings_startup(ns)()
    runtime = hass.data["power_sync"]["entry"]
    assert runtime["store"] is store
    assert runtime["powerwall_settings_submissions"] is submissions
    assert runtime["powerwall_settings_pending"] is pending
    ns["_sync_powerwall_settings_issue"].assert_called_once_with(hass, "entry", pending)
    if pending and pending.get("resume_cloud"):
        assert isinstance(runtime["powerwall_settings_lock"], asyncio.Lock)
        assert runtime["powerwall_settings_cloud_handoff"] == "resumed"
    else:
        assert runtime.get("powerwall_settings_cloud_handoff") is None
    if "other" in initial_data.get("power_sync", {}):
        assert hass.data["power_sync"]["other"] == {"sentinel": True}
    if "entry" in initial_data.get("power_sync", {}):
        assert runtime["tesla_capabilities"] == {"probe": True}


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
    export_namespace = {}
    exec((ROOT / "tesla_export_permissions.py").read_text(), export_namespace)
    runtime = {}
    hass = SimpleNamespace(data={"power_sync": {"entry": runtime}})
    entry = SimpleNamespace(entry_id="entry", options={"tesla_control_method": "tariff"},
                            data={"tesla_energy_site_id": "site", "powerwall_local_din": "din",
                                  "powerwall_local_paired": paired})
    ns = dict(asyncio=asyncio, time=time, DOMAIN="power_sync", _LOGGER=logging.getLogger(__name__),
              tesla_export_rule_from_site_info=export_namespace["tesla_export_rule_from_site_info"],
              read_export_rule=export_namespace["read_export_rule"],
              _sync_powerwall_settings_issue=Mock(), CONF_TESLA_ENERGY_SITE_ID="tesla_energy_site_id",
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


def export_confirmation_context(monkeypatch, acknowledgement, readback, *, status=200, context=None):
    """Replay the real export dispatcher and cloud confirmation with no device."""
    ns, runtime = context or settings_context()
    session = SimpleNamespace(
        post=Mock(return_value=HttpReply(acknowledgement, status)),
        get=Mock(return_value=HttpReply({"response": readback})),
    )
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    ns.update(
        _command_generation=[0], _tesla_charge_kick_generation=[0],
        _record_tesla_settings_submission=AsyncMock(),
        async_get_clientsession=lambda h: session,
        get_tesla_api_base_url=lambda *args: "https://tesla.test",
        CONF_FLEET_API_BASE_URL="base", aiohttp=aiohttp,
    )
    load_function(ROOT / "__init__.py", "_tesla_grid_export_write_result", ns)
    fn = load_function(ROOT / "__init__.py", "_tesla_force_apply_export_rule", ns,
                       strip_imports=True)
    return fn, ns, runtime, session


@pytest.mark.asyncio
@pytest.mark.parametrize("acknowledgement", [
    {"response": {"result": True}},
    {"response": {"code": 204, "message": "Updated"}},
    {"response": {"code": 201, "message": "Updated"}},
    {"response": '{"Message":"Updated","Code":201}\n'},
])
@pytest.mark.parametrize("restoring", [False, True])
async def test_energy_export_acknowledgement_requires_exact_readback(
    monkeypatch, acknowledgement, restoring,
):
    fn, _, runtime, session = export_confirmation_context(
        monkeypatch, acknowledgement,
        {"components": {"customer_preferred_export_rule": "battery_ok"}},
    )
    assert await fn([("site", "token", "fleet")], "battery_ok",
                    reason="tariff export", restoring=restoring)
    assert session.post.call_count == 1
    assert session.get.call_count == 1
    assert runtime["powerwall_setting_observations"]["grid_export_rule"]["source"] == "cloud"


@pytest.mark.asyncio
@pytest.mark.parametrize("readback", [
    {}, {"components": {"customer_preferred_export_rule": None}},
    {"components": {"customer_preferred_export_rule": "never"}},
    {"components": {"customer_preferred_export_rule": "battery_ok"},
     "customer_preferred_export_rule": "pv_only"},
])
async def test_energy_export_update_without_matching_permission_stays_unconfirmed(
    monkeypatch, readback,
):
    fn, _, runtime, session = export_confirmation_context(
        monkeypatch, {"response": {"code": 204, "message": "Updated"}}, readback,
    )
    assert not await fn([("site", "token", "fleet")], "battery_ok", reason="tariff export")
    assert session.get.call_count == 3
    assert runtime["powerwall_setting_observations"]["grid_export_rule"]["source"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("acknowledgement", [
    {"response": {"result": False, "code": 204, "message": "Updated"}},
    {"response": {"code": 403, "message": "Updated"}},
    {"response": {"code": 204, "message": "Rejected"}},
    {"response": {"code": "204", "message": "Updated"}},
    {"response": {}}, {"response": ""}, {"response": "not-json"},
])
async def test_export_rejection_or_unknown_ack_cannot_use_matching_snapshot(
    monkeypatch, acknowledgement,
):
    fn, _, _, session = export_confirmation_context(
        monkeypatch, acknowledgement,
        {"components": {"customer_preferred_export_rule": "battery_ok"}},
    )
    assert not await fn([("site", "token", "fleet")], "battery_ok", reason="tariff export")
    session.get.assert_not_called()


@pytest.mark.asyncio
async def test_energy_export_receipt_cannot_confirm_a_superseded_request(monkeypatch):
    fn, ns, runtime, session = export_confirmation_context(
        monkeypatch, {"response": {"code": 204, "message": "Updated"}}, {},
    )
    def superseding_read(*args, **kwargs):
        ns["_command_generation"][0] += 1
        return HttpReply({"response": {"customer_preferred_export_rule": "battery_ok"}})
    session.get.side_effect = superseding_read
    with pytest.raises(asyncio.CancelledError):
        await fn([("site", "token", "fleet")], "battery_ok", reason="tariff export")
    assert runtime["powerwall_setting_observations"]["grid_export_rule"]["source"] == "unknown"


@pytest.mark.asyncio
async def test_energy_export_receipt_on_failed_http_status_cannot_confirm(monkeypatch):
    fn, _, _, session = export_confirmation_context(
        monkeypatch, {"response": {"code": 204, "message": "Updated"}},
        {"customer_preferred_export_rule": "battery_ok"}, status=403,
    )
    assert not await fn([("site", "token", "fleet")], "battery_ok", reason="tariff export")
    session.get.assert_not_called()


@pytest.mark.asyncio
async def test_energy_export_receipt_requires_each_gateway_to_confirm(monkeypatch):
    fn, _, _, session = export_confirmation_context(
        monkeypatch, {"response": {"code": 204, "message": "Updated"}}, {},
    )
    session.get.side_effect = [
        HttpReply({"response": {"customer_preferred_export_rule": "battery_ok"}}),
        *[HttpReply({"response": {"customer_preferred_export_rule": "never"}}) for _ in range(3)],
    ]
    assert not await fn([("site", "token", "fleet"), ("other", "token", "fleet")],
                        "battery_ok", reason="tariff export")
    assert session.post.call_count == 2
    assert session.get.call_count == 4


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


def recovery_context(saved=None, *, paired=True):
    ns, runtime = settings_context(staged_transport(), paired=paired)
    saved = saved if saved is not None else {
        "tesla_settings_pending": {"id": "incident", "site_id": "site", "expires_at": 0,
                                   "updates": {"default_real_mode": "autonomous"}},
        "force_mode_state": {"mode": "charge", "saved_operation_mode": "backup"},
        "pending_tesla_restore": {"reason": "original restore"},
    }
    runtime["powerwall_settings_pending"] = copy.deepcopy(saved.get("tesla_settings_pending"))
    if runtime["powerwall_settings_pending"] and runtime["powerwall_settings_pending"].get("resume_cloud") is True:
        runtime["powerwall_settings_cloud_handoff"] = runtime["powerwall_settings_pending"]["id"]
    async def save(value):
        saved.clear()
        saved.update(copy.deepcopy(value))
    store = SimpleNamespace(async_load=AsyncMock(side_effect=lambda: copy.deepcopy(saved)),
                            async_save=AsyncMock(side_effect=save))
    add_store_reader(ns, store)
    ns.update(store=store, _command_generation=[0], copy=copy,
              _cancel_all_force_timers=Mock(), _sync_powerwall_settings_issue=Mock())
    ns["hass"].config_entries = SimpleNamespace(async_get_entry=lambda _: ns["entry"])
    load_function(ROOT / "__init__.py", "_update_powerwall_store", ns)
    load_function(ROOT / "__init__.py", "_record_tesla_settings_submission", ns)
    load_function(ROOT / "__init__.py", "handle_resume_powerwall_settings", ns)
    return ns, runtime, saved


def add_store_reader(ns, store):
    """A new Store reads persisted data, without the writer's pending buffer."""
    store.hass, store.version, store.minor_version, store.key = ns["hass"], 1, 1, "test-store"
    ns["Store"] = lambda *args, **kwargs: SimpleNamespace(async_load=store.async_load)


def recovery_call(**changes):
    return SimpleNamespace(data={"entry_id": "entry", "site_id": "site",
                                 "incident_id": "incident", "acknowledge": True, **changes})


@pytest.mark.asyncio
async def test_explicit_recovery_admits_cloud_despite_hung_receipt_and_old_mutex():
    ns, runtime, saved = recovery_context()
    old_lock = runtime["powerwall_settings_lock"] = asyncio.Lock()
    await old_lock.acquire()
    receipt = asyncio.create_task(asyncio.Event().wait())
    runtime["powerwall_settings_submissions"] = {receipt}
    try:
        result = await asyncio.wait_for(ns["handle_resume_powerwall_settings"](recovery_call()), .2)
        assert result["success"] and not receipt.done()
        assert saved["tesla_settings_pending"]["resume_cloud"] is True
        assert saved["tesla_settings_pending"]["restore_basis"]["force_mode_state"]["saved_operation_mode"] == "backup"
        assert saved["force_mode_state"]["saved_operation_mode"] == "backup"
        cloud = AsyncMock(return_value=True)
        ns["get_local_transport"] = Mock(side_effect=AssertionError("local must stay suspended"))
        assert await ns["dispatch_powerwall_setting"](ns["hass"], ns["entry"], site_id="site",
            updates={"default_real_mode": "backup"}, cloud_call=cloud,
            is_current=lambda: True, deadline=time.monotonic()+.1, label="restore")
        cloud.assert_awaited_once()
        assert not receipt.done()
        assert not saved["tesla_settings_pending"].get("terminal")
    finally:
        receipt.cancel()
        await asyncio.gather(receipt, return_exceptions=True)
        old_lock.release()


@pytest.mark.asyncio
async def test_cancelled_release_then_late_receipt_retry_completes_mutex_handoff_once():
    ns, runtime, saved = recovery_context({"force_mode_state": {"saved_operation_mode": "backup"}})
    submitted, receipt = asyncio.Event(), asyncio.Event()
    ns["get_local_transport"] = lambda *_: staged_transport(submitted=submitted, release=receipt)
    old = asyncio.create_task(ns["dispatch_powerwall_setting"](ns["hass"], ns["entry"], site_id="site",
        updates={"default_real_mode": "autonomous"}, cloud_call=AsyncMock(return_value=False),
        before_submit=ns["_record_tesla_settings_submission"], is_current=lambda: True,
        deadline=time.monotonic()+.03, label="old"))
    await submitted.wait()
    assert not await old
    incident_id = saved["tesla_settings_pending"]["id"]
    old_lock = runtime["powerwall_settings_lock"]
    await old_lock.acquire()
    started, finish_save = asyncio.Event(), asyncio.Event()
    save = ns["store"].async_save.side_effect
    async def paused_save(value):
        if value.get("tesla_settings_pending", {}).get("resume_cloud") and not value["tesla_settings_pending"].get("terminal"):
            started.set()
            await finish_save.wait()
        await save(value)
    ns["store"].async_save.side_effect = paused_save
    release = asyncio.create_task(ns["handle_resume_powerwall_settings"](recovery_call(incident_id=incident_id)))
    await started.wait()
    release.cancel()
    with pytest.raises(asyncio.CancelledError):
        await release
    finish_save.set()
    await asyncio.gather(*ns["store"]._power_sync_update_tasks)
    resumed_at = saved["tesla_settings_pending"]["resumed_at"]
    receipt.set()
    await asyncio.gather(*runtime["powerwall_settings_submissions"])
    assert runtime["powerwall_settings_pending"]["resume_cloud"] is True
    try:
        cloud = AsyncMock(return_value=True)
        with pytest.raises(asyncio.CancelledError):
            await ns["dispatch_powerwall_setting"](ns["hass"], ns["entry"], site_id="site",
                updates={"default_real_mode": "backup"}, cloud_call=cloud,
                is_current=lambda: True, deadline=time.monotonic()+.1, label="handoff missing")
        cloud.assert_not_awaited()
        await ns["handle_resume_powerwall_settings"](recovery_call(incident_id=incident_id))
        assert runtime["powerwall_settings_lock"] is not old_lock
        assert old_lock.locked()
        assert saved["tesla_settings_pending"]["resumed_at"] == resumed_at
        assert saved["tesla_settings_pending"]["terminal"] is True
        assert saved["tesla_settings_pending"]["restore_basis"]["force_mode_state"]["saved_operation_mode"] == "backup"
        assert await ns["dispatch_powerwall_setting"](ns["hass"], ns["entry"], site_id="site",
            updates={"default_real_mode": "backup"}, cloud_call=AsyncMock(return_value=True),
            is_current=lambda: True, deadline=time.monotonic()+.1, label="after handoff")
        installed = runtime["powerwall_settings_lock"]
        generation = ns["_command_generation"][0]
        await installed.acquire()
        await ns["handle_resume_powerwall_settings"](recovery_call(incident_id=incident_id))
        assert runtime["powerwall_settings_lock"] is installed and installed.locked()
        assert ns["_command_generation"][0] == generation
        installed.release()
    finally:
        old_lock.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"acknowledge": False}, {"acknowledge": 1},
    {"incident_id": "stale"}, {"entry_id": "wrong"}, {"site_id": "other"}])
async def test_recovery_requires_explicit_ack_and_exact_incident_entry_site(changes):
    ns, runtime, saved = recovery_context()
    before = copy.deepcopy(saved)
    with pytest.raises(RuntimeError):
        await ns["handle_resume_powerwall_settings"](recovery_call(**changes))
    assert saved == before
    assert not runtime["powerwall_settings_pending"].get("resume_cloud")
    ns["_cancel_all_force_timers"].assert_not_called()


@pytest.mark.asyncio
async def test_recovery_persistence_failure_never_opens_admission():
    ns, runtime, saved = recovery_context()
    ns["store"].async_save.side_effect = OSError("disk full")
    with pytest.raises(OSError):
        await ns["handle_resume_powerwall_settings"](recovery_call())
    assert not saved["tesla_settings_pending"].get("resume_cloud")
    assert not runtime["powerwall_settings_pending"].get("resume_cloud")


@pytest.mark.asyncio
async def test_store_save_swallowed_failure_cannot_release_or_submit_local_write():
    ns, runtime, saved = recovery_context()
    ns["store"].async_save.side_effect = None  # HA can log a write error and return normally.
    with pytest.raises(RuntimeError):
        await ns["handle_resume_powerwall_settings"](recovery_call())
    assert not runtime["powerwall_settings_pending"].get("resume_cloud")
    assert not saved["tesla_settings_pending"].get("resume_cloud")
    saved.clear()
    runtime["powerwall_settings_pending"] = None
    sent = []
    async def write(din, updates, *, before_submit, before_write):
        await before_submit(int(time.time())+12)
        before_write()
        sent.append(updates)
    ns["get_local_transport"] = lambda *_: SimpleNamespace(write_config=write)
    with pytest.raises(RuntimeError):
        await ns["dispatch_powerwall_setting"](ns["hass"], ns["entry"], site_id="site",
            updates={"default_real_mode": "autonomous"}, cloud_call=AsyncMock(return_value=True),
            before_submit=ns["_record_tesla_settings_submission"], is_current=lambda: True,
            deadline=time.monotonic()+1, label="failed durable stage")
    assert not sent


@pytest.mark.asyncio
async def test_cancelled_foreground_cannot_let_recovery_overtake_its_storage_worker():
    ns, runtime, saved = recovery_context()
    saved["tesla_settings_pending"]["restore_basis"] = {"force_mode_state": copy.deepcopy(saved["force_mode_state"])}
    runtime["powerwall_settings_pending"] = copy.deepcopy(saved["tesla_settings_pending"])
    started, finish_worker = asyncio.Event(), asyncio.Event()
    save = ns["store"].async_save.side_effect
    first = True
    async def worker_write(value):
        nonlocal first
        if first:
            first = False
            started.set()
            await finish_worker.wait()
        await save(value)
    ns["store"].async_save.side_effect = worker_write
    old = asyncio.create_task(ns["_update_powerwall_store"](ns["store"], {"unrelated": "retained"}))
    runtime["powerwall_settings_control_tasks"] = {old}
    await started.wait()
    release = asyncio.create_task(ns["handle_resume_powerwall_settings"](recovery_call()))
    await asyncio.sleep(.01)
    assert not release.done()
    assert ns["store"].async_save.await_count == 1
    with pytest.raises(asyncio.CancelledError):
        await old
    finish_worker.set()
    await release
    assert saved["unrelated"] == "retained"
    assert saved["tesla_settings_pending"]["resume_cloud"] is True
    assert saved["tesla_settings_pending"]["restore_basis"]["force_mode_state"]["saved_operation_mode"] == "backup"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["disk_error", "shutdown_deferred", None])
async def test_critical_store_verification_reads_disk_instead_of_pending_buffer(tmp_path, failure):
    ns, runtime, saved = recovery_context()
    path = tmp_path / "entry.json"
    json = __import__("json")
    path.write_text(json.dumps(saved))
    class BufferedStore:
        """Model HA storage.py's swallowed writer errors and stopping buffer."""
        def __init__(self, hass, version, key, *, minor_version=1):
            self.hass, self.version, self.key, self.minor_version = hass, version, key, minor_version
            self.pending = None
        async def async_load(self):
            return copy.deepcopy(self.pending) if self.pending is not None else json.loads(path.read_text())
        async def async_save(self, value):
            self.pending = copy.deepcopy(value)
            if failure == "shutdown_deferred":
                return  # HA keeps _data for its final write; the writer can read it.
            data, self.pending = self.pending, None
            try:
                if failure == "disk_error":
                    raise OSError("disk full")
                path.write_text(json.dumps(data))
            except OSError:
                pass  # HA logs the underlying WriteError and returns normally.
    ns["Store"] = BufferedStore
    ns["store"] = BufferedStore(ns["hass"], 1, "entry")
    if failure:
        with pytest.raises(RuntimeError):
            await ns["handle_resume_powerwall_settings"](recovery_call())
        assert not runtime["powerwall_settings_pending"].get("resume_cloud")
        assert not json.loads(path.read_text())["tesla_settings_pending"].get("resume_cloud")
    else:
        await ns["handle_resume_powerwall_settings"](recovery_call())
        assert runtime["powerwall_settings_pending"]["resume_cloud"] is True
        assert json.loads(path.read_text())["tesla_settings_pending"]["resume_cloud"] is True


@pytest.mark.asyncio
async def test_recovery_save_closes_ingress_until_new_mutex_is_installed():
    ns, runtime, saved = recovery_context()
    started, release = asyncio.Event(), asyncio.Event()
    save = ns["store"].async_save.side_effect
    async def paused_save(value):
        started.set()
        await release.wait()
        await save(value)
    ns["store"].async_save.side_effect = paused_save
    recovery = asyncio.create_task(ns["handle_resume_powerwall_settings"](recovery_call()))
    await started.wait()
    cloud = AsyncMock(return_value=True)
    with pytest.raises(asyncio.CancelledError):
        await ns["dispatch_powerwall_setting"](ns["hass"], ns["entry"], site_id="site",
            updates={"default_real_mode": "backup"}, cloud_call=cloud,
            is_current=lambda: True, deadline=time.monotonic()+.1, label="during save")
    cloud.assert_not_awaited()
    release.set()
    await recovery
    assert not runtime.get("powerwall_settings_recovering")
    assert await ns["dispatch_powerwall_setting"](ns["hass"], ns["entry"], site_id="site",
        updates={"default_real_mode": "backup"}, cloud_call=cloud,
        is_current=lambda: True, deadline=time.monotonic()+.1, label="after save")


@pytest.mark.asyncio
async def test_old_site_incident_cannot_lock_out_new_site_or_capture_its_restore_basis():
    ns, runtime, saved = recovery_context()
    ns["entry"].data["tesla_energy_site_id"] = "new-site"
    cloud = AsyncMock(return_value=True)
    assert await ns["dispatch_powerwall_setting"](ns["hass"], ns["entry"], site_id="new-site",
        updates={"default_real_mode": "backup"}, cloud_call=cloud,
        is_current=lambda: True, deadline=time.monotonic()+.1, label="new site")
    await ns["handle_resume_powerwall_settings"](recovery_call())
    assert saved["tesla_settings_pending"]["site_id"] == "site"
    assert saved["tesla_settings_pending"]["restore_basis"] == {}
    assert saved["force_mode_state"]["saved_operation_mode"] == "backup"


@pytest.mark.asyncio
async def test_recovery_is_idempotent_and_survives_cloud_failure_and_restart():
    ns, runtime, saved = recovery_context()
    await ns["handle_resume_powerwall_settings"](recovery_call())
    before = copy.deepcopy(saved)
    await ns["handle_resume_powerwall_settings"](recovery_call())
    assert saved == before
    assert ns["_command_generation"] == [1]
    ns2, runtime2, _ = recovery_context(copy.deepcopy(saved), paired=False)
    cloud = AsyncMock(side_effect=[False, True])
    for expected in (False, True):
        assert await ns2["dispatch_powerwall_setting"](ns2["hass"], ns2["entry"], site_id="site",
            updates={"default_real_mode": "backup"}, cloud_call=cloud,
            is_current=lambda: True, deadline=time.monotonic()+.1, label="after restart") is expected
    assert cloud.await_count == 2
    assert runtime2["powerwall_settings_pending"]["restore_basis"] == before["tesla_settings_pending"]["restore_basis"]


@pytest.mark.asyncio
async def test_late_receipt_matches_immutable_id_and_preserves_released_restore_basis():
    ns, runtime, saved = recovery_context()
    original = copy.deepcopy(saved["tesla_settings_pending"])
    await ns["handle_resume_powerwall_settings"](recovery_call())
    result = await ns["_record_tesla_settings_submission"](None, previous=original)
    assert result["terminal"] is True and result["resume_cloud"] is True
    assert result["restore_basis"]["pending_tesla_restore"] == {"reason": "original restore"}
    assert saved["tesla_settings_pending"] == result
    saved["tesla_settings_pending"] = {"id": "new", "site_id": "site"}
    with pytest.raises(RuntimeError):
        await ns["_record_tesla_settings_submission"](None, previous=original)
    assert saved["tesla_settings_pending"]["id"] == "new"


@pytest.mark.asyncio
async def test_recovery_revokes_an_old_rpc_before_its_actual_send():
    ns, runtime, saved = recovery_context({"force_mode_state": {"saved_operation_mode": "backup"}})
    staged, release = asyncio.Event(), asyncio.Event()
    sent = []
    async def write(din, updates, *, before_submit, before_write):
        await before_submit(int(time.time()) + 12)
        staged.set()
        await release.wait()
        before_write()
        sent.append(updates)
        return True
    ns["get_local_transport"] = lambda *_: SimpleNamespace(write_config=write)
    old = asyncio.create_task(ns["dispatch_powerwall_setting"](ns["hass"], ns["entry"], site_id="site",
        updates={"default_real_mode": "autonomous"}, cloud_call=AsyncMock(),
        before_submit=ns["_record_tesla_settings_submission"], is_current=lambda: True,
        deadline=time.monotonic()+2, label="old"))
    await staged.wait()
    incident_id = runtime["powerwall_settings_pending"]["id"]
    await ns["handle_resume_powerwall_settings"](recovery_call(incident_id=incident_id))
    release.set()
    await asyncio.gather(old, *runtime["powerwall_settings_submissions"], return_exceptions=True)
    assert not sent
    assert saved["tesla_settings_pending"]["resume_cloud"] is True
    assert saved["tesla_settings_pending"]["terminal"] is True  # Definitely unsent.


@pytest.mark.asyncio
async def test_actual_late_ack_updates_replacement_runtime_without_publishing_old_intent():
    ns, runtime, saved = recovery_context({"force_mode_state": {"saved_operation_mode": "backup"}})
    submitted, release = asyncio.Event(), asyncio.Event()
    transport = staged_transport(submitted=submitted, release=release)
    ns["get_local_transport"] = lambda *_: transport
    old = asyncio.create_task(ns["dispatch_powerwall_setting"](ns["hass"], ns["entry"], site_id="site",
        updates={"default_real_mode": "autonomous"}, cloud_call=AsyncMock(return_value=True),
        before_submit=ns["_record_tesla_settings_submission"], is_current=lambda: True,
        deadline=time.monotonic()+.03, label="old"))
    await submitted.wait()
    assert not await old
    await ns["handle_resume_powerwall_settings"](recovery_call(incident_id=saved["tesla_settings_pending"]["id"]))
    replacement = {"powerwall_settings_pending": copy.deepcopy(saved["tesla_settings_pending"]),
                   "powerwall_setting_observations": {"operation_mode": {"value": "backup", "source": "cloud"}}}
    ns["hass"].data["power_sync"]["entry"] = replacement
    release.set()
    await asyncio.gather(*runtime["powerwall_settings_submissions"])
    assert replacement["powerwall_settings_pending"]["terminal"] is True
    assert replacement["powerwall_settings_pending"]["resume_cloud"] is True
    assert replacement["powerwall_setting_observations"]["operation_mode"]["value"] == "backup"
    transport.read_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_crash_after_release_save_reconstructs_permission_from_durable_record():
    ns, runtime, saved = recovery_context()
    save = ns["store"].async_save.side_effect
    async def crash(value):
        await save(value)
        raise asyncio.CancelledError
    ns["store"].async_save.side_effect = crash
    with pytest.raises(asyncio.CancelledError):
        await ns["handle_resume_powerwall_settings"](recovery_call())
    assert not runtime["powerwall_settings_pending"].get("resume_cloud")
    ns2, runtime2, _ = recovery_context(copy.deepcopy(saved))
    assert runtime2["powerwall_settings_pending"]["resume_cloud"] is True
    assert await ns2["dispatch_powerwall_setting"](ns2["hass"], ns2["entry"], site_id="site",
        updates={"default_real_mode": "backup"}, cloud_call=AsyncMock(return_value=True),
        is_current=lambda: True, deadline=time.monotonic()+.1, label="after crash")
    before = copy.deepcopy(saved)
    ns["store"].async_save.side_effect = save
    await ns["handle_resume_powerwall_settings"](recovery_call())
    assert runtime["powerwall_settings_pending"]["resume_cloud"] is True
    assert saved == before  # Same-process retry adopts the saved decision.


@pytest.mark.asyncio
async def test_setup_install_cannot_recreate_old_gate_while_terminal_receipt_saves():
    ns, runtime, saved = recovery_context()
    original = copy.deepcopy(saved["tesla_settings_pending"])
    started, release = asyncio.Event(), asyncio.Event()
    load = ns["store"].async_load.side_effect
    first = True
    async def paused_load():
        nonlocal first
        value = load()
        if first:
            first = False
            started.set()
            await release.wait()
        return value
    ns["store"].async_load.side_effect = paused_load
    runtime.clear()  # Replacement setup has not installed its pending state.
    ns["hass"].data["power_sync_settings"] = {"entry": {"store": ns["store"]}}
    ns.update(STORAGE_VERSION=1, STORAGE_KEY="test")
    install = asyncio.create_task(load_settings_startup(ns)())
    await started.wait()
    async def receipt():
        value = await ns["_record_tesla_settings_submission"](None, previous=original)
        active_runtime = ns["hass"].data["power_sync"]["entry"]
        if (active_runtime.get("powerwall_settings_pending") or {}).get("id") == original["id"]:
            active_runtime["powerwall_settings_pending"] = value
    terminal = asyncio.create_task(receipt())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(install, terminal)
    assert saved["tesla_settings_pending"] is None
    assert ns["hass"].data["power_sync"]["entry"]["powerwall_settings_pending"] is None


def test_repair_notice_is_persistent_and_cannot_release_by_dismissal():
    ir = SimpleNamespace(async_create_issue=Mock(), async_delete_issue=Mock(),
                         IssueSeverity=SimpleNamespace(WARNING="warning"))
    ns = dict(ir=ir, DOMAIN="power_sync", json=__import__("json"))
    sync = load_function(ROOT / "__init__.py", "_sync_powerwall_settings_issue", ns, strip_imports=True)
    incident = {"id": "incident", "site_id": "site", "updates": {"default_real_mode": "autonomous"}}
    before = copy.deepcopy(incident)
    sync(object(), "entry", incident)
    kwargs = ir.async_create_issue.call_args.kwargs
    assert kwargs["is_persistent"] is True and kwargs["is_fixable"] is False
    assert kwargs["translation_placeholders"]["incident_id"] == "incident"
    assert incident == before  # Registry state never grants execution permission.
    sync(object(), "entry", None)
    ir.async_delete_issue.assert_called_once()


@pytest.mark.asyncio
async def test_unload_bounds_receipt_wait_without_cancelling_observation():
    tree = ast.parse((ROOT / "__init__.py").read_text())
    unload = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_unload_entry")
    capture = next(n for n in ast.walk(unload) if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "submissions" for t in n.targets))
    drain = next(n for n in ast.walk(unload) if isinstance(n, ast.If)
        and isinstance(n.test, ast.Name) and n.test.id == "submissions")
    receipt = asyncio.create_task(asyncio.Event().wait())
    async def bounded_wait(tasks, *, timeout):
        assert 0 < timeout <= 5
        return await asyncio.wait(tasks, timeout=.01)
    ns = dict(entry_data={"powerwall_settings_submissions": {receipt}},
              asyncio=SimpleNamespace(wait=bounded_wait))
    wrapper = ast.parse("async def unload_receipts(): pass").body[0]
    wrapper.body = [capture, drain]
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])), "receipt_unload", "exec"), ns)
    try:
        await asyncio.wait_for(ns["unload_receipts"](), .1)
        assert not receipt.done()
    finally:
        receipt.cancel()
        await asyncio.gather(receipt, return_exceptions=True)


@pytest.mark.asyncio
async def test_full_unload_and_settings_setup_boundary_keep_released_cloud_available():
    ns, runtime, saved = recovery_context()
    await ns["handle_resume_powerwall_settings"](recovery_call())
    receipt = asyncio.create_task(asyncio.Event().wait())
    submissions = runtime["powerwall_settings_submissions"] = {receipt}
    runtime["force_charge_state"] = {"active": True, "saved_operation_mode": "backup"}
    runtime["powerwall_battery_dispatch"] = SimpleNamespace(stop=AsyncMock())
    ns["hass"].data["power_sync_settings"] = {"entry": {"store": ns["store"], "submissions": submissions}}
    ns["hass"].config_entries.async_unload_platforms = AsyncMock(return_value=True)
    ns["hass"].services = SimpleNamespace(async_remove=Mock())
    real_asyncio = asyncio
    class BoundedClock:
        def __getattr__(self, name):
            return getattr(real_asyncio, name)
        async def wait(self, tasks, *, timeout):
            assert timeout == 5.0
            return await real_asyncio.wait(tasks, timeout=.01)
    ns.update(asyncio=BoundedClock(), _stop_tesla_native_rate=AsyncMock(), PLATFORMS=[],
              SERVICE_SYNC_TOU="sync_tou", SERVICE_SYNC_NOW="sync_now",
              cleanup_dynamic_ev_entry=Mock(), STORAGE_VERSION=1, STORAGE_KEY="test")
    unload = load_function(ROOT / "__init__.py", "async_unload_entry", ns, strip_imports=True)
    try:
        assert await asyncio.wait_for(unload(ns["hass"], ns["entry"]), .5)
        assert not receipt.done()
        # A reload must not rely on an artificially pre-created entry mapping.
        await load_settings_startup(ns)()
        replacement = ns["hass"].data["power_sync"]["entry"]
        assert replacement["powerwall_settings_submissions"] is submissions
        assert replacement["powerwall_settings_pending"]["resume_cloud"] is True
        assert await ns["dispatch_powerwall_setting"](ns["hass"], ns["entry"], site_id="site",
            updates={"default_real_mode": "backup"}, cloud_call=AsyncMock(return_value=True),
            is_current=lambda: True, deadline=time.monotonic()+.1, label="after full unload")
        assert saved["force_mode_state"]["saved_operation_mode"] == "backup"
    finally:
        receipt.cancel()
        await asyncio.gather(receipt, return_exceptions=True)


def test_tesla_tracking_wrapper_is_identity_for_other_battery_brands():
    ns = dict(active_battery_system="solaredge", BATTERY_SYSTEM_TESLA="tesla")
    wrap = load_function(ROOT / "__init__.py", "_track_tesla_settings_control", ns)
    handler = lambda call: None
    assert wrap(handler) is handler


@pytest.mark.asyncio
async def test_late_remote_full_file_commit_is_not_misreported_as_settled_by_cloud_readback():
    ns, runtime, saved = recovery_context({"force_mode_state": {"saved_operation_mode": "backup"}})
    accepted, commit = asyncio.Event(), asyncio.Event()
    gateway = {"default_real_mode": "backup", "export": "pv_only"}
    async def write(din, updates, *, before_submit, before_write):
        old_snapshot = {**gateway, **updates}
        await before_submit(int(time.time()) + 12)
        before_write()
        accepted.set()
        await commit.wait()  # A model permitted by the unknown queue contract.
        gateway.clear()
        gateway.update(old_snapshot)
        raise ns["PowerwallUnreachableError"]("receipt lost after remote commit")
    ns["get_local_transport"] = lambda *_: SimpleNamespace(write_config=write)
    old = asyncio.create_task(ns["dispatch_powerwall_setting"](ns["hass"], ns["entry"], site_id="site",
        updates={"default_real_mode": "autonomous"}, cloud_call=AsyncMock(return_value=False),
        before_submit=ns["_record_tesla_settings_submission"], is_current=lambda: True,
        deadline=time.monotonic()+.02, label="old"))
    await accepted.wait()
    assert not await old
    await ns["handle_resume_powerwall_settings"](recovery_call(incident_id=saved["tesla_settings_pending"]["id"]))
    async def confirmed_cloud():
        gateway["default_real_mode"] = "backup"
        gateway["export"] = "never"
        return gateway["default_real_mode"] == "backup"
    assert await ns["dispatch_powerwall_setting"](ns["hass"], ns["entry"], site_id="site",
        updates={"default_real_mode": "backup"}, cloud_call=confirmed_cloud,
        is_current=lambda: True, deadline=time.monotonic()+.1, label="new")
    commit.set()
    await asyncio.gather(*runtime["powerwall_settings_submissions"], return_exceptions=True)
    assert gateway == {"default_real_mode": "autonomous", "export": "pv_only"}
    assert saved["tesla_settings_pending"]["resume_cloud"] is True
    assert not saved["tesla_settings_pending"].get("terminal")
    assert saved["tesla_settings_pending"]["restore_basis"]["force_mode_state"]["saved_operation_mode"] == "backup"
    assert await ns["dispatch_powerwall_setting"](ns["hass"], ns["entry"], site_id="site",
        updates={"default_real_mode": "backup"}, cloud_call=confirmed_cloud,
        is_current=lambda: True, deadline=time.monotonic()+.1, label="supervised correction")
    assert gateway == {"default_real_mode": "backup", "export": "never"}
    assert not saved["tesla_settings_pending"].get("terminal")


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
    ({"response": {"result": True}}, {"response": {"components": {"customer_preferred_export_rule": None}, "customer_preferred_export_rule": "pv_only"}}, True),
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
@pytest.mark.parametrize("stopping,restoring", [(False, False), (True, True)])
async def test_native_cloud_export_route_is_field_selective_and_fenced(stopping, restoring):
    transport = SimpleNamespace(write_config=AsyncMock(), read_config=AsyncMock())
    ns, runtime = settings_context(transport)
    runtime["powerwall_local_dispatch_stopping"] = stopping
    sent = []
    fences = []
    async def before_write():
        return lambda: fences.append('checked')
    def post(*args, **kwargs):
        assert fences == ['checked']
        sent.append(kwargs['json'])
        return HttpReply({'response': {'result': True}})
    ns.update(_command_generation=[0], _tesla_charge_kick_generation=[0], aiohttp=aiohttp,
        _record_tesla_settings_submission=AsyncMock(),
        async_get_clientsession=lambda h: SimpleNamespace(post=post, get=lambda *a, **kw:
            HttpReply({'response': {'components': {'customer_preferred_export_rule': 'pv_only'}}})),
        CONF_FLEET_API_BASE_URL='base', get_tesla_api_base_url=lambda *a: 'https://tesla.test')
    load_function(ROOT / '__init__.py', '_tesla_grid_export_write_result', ns)
    fn = load_function(ROOT / '__init__.py', '_tesla_force_apply_export_rule', ns, strip_imports=True)
    status = {}
    assert await fn([('site', 'token', 'fleet')], 'pv_only', reason='native restore',
        prefer_local=False, before_write=before_write, submission_status=status, restoring=restoring)
    assert sent == [{'customer_preferred_export_rule': 'pv_only'}]
    assert status == {'submitted': True}
    transport.write_config.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('resume_cloud', [False, True])
async def test_cloud_export_fallback_respects_existing_settings_recovery_guard(resume_cloud):
    ns, runtime = settings_context(paired=False)
    runtime['powerwall_settings_pending'] = {'id': 'prior-write', 'site_id': 'site',
        'resume_cloud': resume_cloud}
    if resume_cloud:
        runtime['powerwall_settings_cloud_handoff'] = 'prior-write'
    post = Mock(return_value=HttpReply({'response': {'result': True}}))
    ns.update(_command_generation=[0], _tesla_charge_kick_generation=[0], aiohttp=aiohttp,
        _record_tesla_settings_submission=AsyncMock(),
        async_get_clientsession=lambda h: SimpleNamespace(post=post, get=lambda *a, **kw:
            HttpReply({'response': {'components': {'customer_preferred_export_rule': 'pv_only'}}})),
        CONF_FLEET_API_BASE_URL='base', get_tesla_api_base_url=lambda *a: 'https://tesla.test')
    load_function(ROOT / '__init__.py', '_tesla_grid_export_write_result', ns)
    fn = load_function(ROOT / '__init__.py', '_tesla_force_apply_export_rule', ns, strip_imports=True)
    assert await fn([('site', 'token', 'fleet')], 'pv_only', reason='native restore',
        prefer_local=False) is resume_cloud
    assert post.call_count == int(resume_cloud)


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [{'response': {'result': False}}, {'response': {'result': True}}])
async def test_cloud_export_submission_status_distinguishes_rejection_from_failed_read(payload):
    ns, _ = settings_context(paired=False)
    post = Mock(return_value=HttpReply(payload))
    ns.update(_command_generation=[0], _tesla_charge_kick_generation=[0], aiohttp=aiohttp,
        _record_tesla_settings_submission=AsyncMock(),
        async_get_clientsession=lambda h: SimpleNamespace(post=post, get=lambda *a, **kw: HttpReply({'response': {}})),
        CONF_FLEET_API_BASE_URL='base', get_tesla_api_base_url=lambda *a: 'https://tesla.test')
    load_function(ROOT / '__init__.py', '_tesla_grid_export_write_result', ns)
    fn = load_function(ROOT / '__init__.py', '_tesla_force_apply_export_rule', ns, strip_imports=True)
    status = {}
    assert not await fn([('site', 'token', 'fleet')], 'pv_only', reason='native restore',
        prefer_local=False, submission_status=status)
    assert post.call_count == 1
    assert status.get('submitted') is True
    assert bool(status.get('rejected')) is (payload['response']['result'] is False)


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
    add_store_reader(ns, store)
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
async def test_actual_restore_after_release_ends_forcing_but_retains_original_uncertain_basis():
    ns, runtime, saved, call = restore_context()
    await ns["persist_force_mode_state"]()
    original_basis = copy.deepcopy(saved["force_mode_state"])
    incident = {"id": "incident", "site_id": "site", "updates": {"default_real_mode": "autonomous"}}
    saved["tesla_settings_pending"] = copy.deepcopy(incident)
    runtime["powerwall_settings_pending"] = incident
    ns["entry"].data[ns["CONF_TESLA_ENERGY_SITE_ID"]] = "site"
    ns["hass"].config_entries = SimpleNamespace(async_get_entry=lambda _: ns["entry"])
    load_function(ROOT / "__init__.py", "handle_resume_powerwall_settings", ns)
    await ns["handle_resume_powerwall_settings"](recovery_call())
    assert ns["_tesla_force_apply_operation_mode"].await_count == 0  # Resumption itself writes nothing.
    assert (await ns["handle_restore_normal"](call))["success"] is True
    assert saved["force_mode_state"] is None and saved["pending_tesla_restore"] is None
    retained = saved["tesla_settings_pending"]
    assert retained["resume_cloud"] is True and not retained.get("terminal")
    assert retained["restore_basis"]["force_mode_state"] == original_basis
    assert not ns["force_discharge_state"]["active"]
    await ns["_prepare_tesla_setting_change"](SimpleNamespace())


@pytest.mark.asyncio
async def test_failed_live_restore_keeps_owner_but_does_not_revoke_cloud_resumption():
    ns, runtime, saved, call = restore_context()
    await ns["persist_force_mode_state"]()
    incident = {"id": "incident", "site_id": "site", "resume_cloud": True,
                "restore_basis": {"force_mode_state": copy.deepcopy(saved["force_mode_state"])}}
    saved["tesla_settings_pending"] = runtime["powerwall_settings_pending"] = incident
    runtime["powerwall_settings_cloud_handoff"] = incident["id"]
    ns["_tesla_force_apply_export_rule"].return_value = False
    with pytest.raises(RuntimeError, match="restoration remains pending"):
        await ns["_prepare_tesla_setting_change"](call)
    assert saved["tesla_settings_pending"]["resume_cloud"] is True
    assert ns["force_discharge_state"]["active"] is True
    ns["_tesla_force_apply_export_rule"].return_value = True
    await ns["_prepare_tesla_setting_change"](call)
    assert not ns["force_discharge_state"]["active"]
    assert saved["tesla_settings_pending"]["resume_cloud"] is True


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
