"""Cloud/local final mutation ordering, read exemptions and durable intent."""
from __future__ import annotations

import asyncio
import ast
from copy import deepcopy
import importlib
import json
import logging
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1] / "custom_components/power_sync"
PKG = "powerwall_mutation_gate_test"
for name, path in ((PKG, ROOT), (f"{PKG}.powerwall_local", ROOT / "powerwall_local")):
    package = ModuleType(name)
    package.__path__ = [str(path)]
    sys.modules[name] = package
core = importlib.import_module(f"{PKG}.powerwall_control")
http = importlib.import_module(f"{PKG}.powerwall_control_http")
transport = importlib.import_module(f"{PKG}.powerwall_local.transport")
fleet = importlib.import_module(f"{PKG}.powerwall_local.fleet_api_bms")


async def settle():
    for _ in range(4):
        await asyncio.sleep(0)


class Reply:
    status = 200
    def __init__(self, raw=b"", *, entered=None, release=None):
        self.raw, self.entered, self.release = raw, entered, release
        self.closed = False
    async def __aenter__(self):
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        return self
    async def __aexit__(self, *exc):
        self.closed = True
    async def read(self):
        return self.raw
    async def text(self):
        return '{"response": {"result": true}}'
    def close(self):
        self.closed = True
    def __await__(self):
        return self.__aenter__().__await__()


class Session:
    def __init__(self, reply=None):
        self.reply = reply or Reply()
        self.posts = []
        self.get_result = object()
        self.closed = False
    async def __aenter__(self):
        return self
    async def __aexit__(self, *exc):
        self.closed = True
    def post(self, url, *args, **kwargs):
        self.posts.append((url, args, kwargs))
        return self.reply
    def get(self, *args, **kwargs):
        return self.get_result
    def request(self, method, url, *args, **kwargs):
        if method.upper() == "GET":
            return self.get_result
        return self.post(url, *args, **kwargs)


def local_transport(site, session):
    result = object.__new__(transport.TEDAPIv1rTransport)
    result._host, result._public_key_der = "gateway.invalid", b"public-key"
    result._build_tlv_payload = lambda *args: b"tlv"
    result._sign = lambda *args: b"signature"
    result._site_control = site
    result._control_current = lambda: True
    result._control_identity = lambda: site.site_id
    async def get_session():
        return session
    result._session = get_session
    return result


def envelope(group, field):
    result = transport.transport_pb2.MessageEnvelope()
    result.delivery_channel = 2
    result.recipient.din = "DIN"
    getattr(getattr(result, group), field).SetInParent()
    return result.SerializeToString()


@pytest.mark.asyncio
async def test_http_reads_non_tesla_and_await_protocol_preserved():
    hass = SimpleNamespace(data={})
    raw = Session()
    session = http.controlled_session(hass, raw)
    assert session.get("https://fleet/api/1/energy_sites/123/live_status") is raw.get_result
    assert session.post("https://other.invalid/v1/example") is raw.reply
    assert session.closed is False
    assert http.controlled_session(hass, session) is session
    assert await session.post("https://api.powersync.cc/api/proxy/api/1/energy_sites/123/backup") is raw.reply
    site = core.coordinator_for(hass, site_id=123)
    assert [item["label"] for item in site.snapshot()["effects"]] == ["cloud_backup"]


@pytest.mark.asyncio
async def test_all_energy_suffixes_gated_without_payload_or_path_in_receipts():
    hass = SimpleNamespace(data={})
    raw = Session()
    session = http.controlled_session(hass, raw)
    async with session.post("https://api.teslemetry.com/api/1/energy_sites/123/unrecognized/private-value?token=secret",
                            json={"secret": "payload"}, headers={"Authorization": "token"}):
        snapshot = core.coordinator_for(hass, site_id=123).snapshot()
        assert snapshot["effects"][0]["label"] == "cloud_energy_command"
        assert "private-value" not in repr(snapshot) and "secret" not in repr(snapshot)


@pytest.mark.asyncio
async def test_composed_session_flattens_gates_and_preserves_all_entry_guards():
    hass = SimpleNamespace(data={})
    raw = Session()
    state = SimpleNamespace(first=True, second=True)
    original = http.controlled_session(hass, raw, is_current=lambda: state.first, identity=lambda: 123)
    composed = http.controlled_session(hass, original, is_current=lambda: state.second, identity=lambda: "123")
    async with composed.post("https://fleet/api/1/energy_sites/123/backup"):
        pass
    site = core.coordinator_for(hass, site_id=123)
    assert len(site.snapshot()["effects"]) == 1
    state.first = False
    with pytest.raises(core.ControlRequestStale):
        async with composed.post("https://fleet/api/1/energy_sites/123/operation"):
            pass
    assert len(raw.posts) == 1


@pytest.mark.asyncio
async def test_cloud_and_local_same_site_serialize_but_local_reads_do_not_wait():
    hass = SimpleNamespace(data={})
    site = core.coordinator_for(hass, site_id=123)
    entered, release = asyncio.Event(), asyncio.Event()
    cloud_raw = Session(Reply(entered=entered, release=release))
    cloud = http.controlled_session(hass, cloud_raw)
    local_raw = Session()
    local = local_transport(site, local_raw)
    async def cloud_write():
        async with cloud.post("https://fleet/api/1/energy_sites/123/operation"):
            pass
    active = asyncio.create_task(cloud_write())
    await entered.wait()
    queued = asyncio.create_task(local.post_v1r(envelope("teg", "dispatch_battery_power_request"), "DIN"))
    await settle()
    assert local_raw.posts == []
    # Both config reads and exact signed query envelopes can run while a
    # mutation owns the final gate (Native Max guards need these snapshots).
    await local.post_v1r(envelope("filestore", "read_file_request"), "DIN")
    await local.post_v1r(fleet.build_device_controller_query_envelope("DIN"), "DIN")
    assert len(local_raw.posts) == 2
    assert site.snapshot()["operation"] is not None
    release.set()
    await asyncio.gather(active, queued)
    assert len(local_raw.posts) == 3
    assert [effect["label"] for effect in site.snapshot()["effects"]] == ["cloud_operation", "local_dispatch"]


@pytest.mark.parametrize("group,field", [
    ("filestore", "read_file_request"), ("common", "get_system_info_request"),
    ("common", "get_networking_status_request"), ("common", "check_internet_request"),
    ("authorization", "list_authorized_clients_request"), ("teg", "get_backup_events_request"),
    ("teg", "get_delay_battery_calibration_status_request"),
])
def test_only_proven_read_envelopes_are_exempt(group, field):
    raw = envelope(group, field)
    assert transport._mutation_label(raw) is None
    # An additional unknown top-level field must not inherit the read exemption.
    assert transport._mutation_label(raw + b"\xf2\x01\x00") == "mutation"


def test_unknown_and_islanding_and_manual_backup_are_mutations():
    tp = importlib.import_module(f"{PKG}.powerwall_local.tesla_local_pb2")
    island = tp.MessageEnvelope()
    island.teg.setIslandModeRequest.mode = 6
    assert transport._mutation_label(island.SerializeToString()) == "mutation"
    for field in ("schedule_manual_backup_event_request", "cancel_manual_backup_event_request",
                  "dispatch_battery_power_request", "delay_battery_calibration_request"):
        assert transport._mutation_label(envelope("teg", field)) is not None
    assert transport._mutation_label(b"not a protobuf") == "mutation"
    assert transport._mutation_label(fleet.build_pw3_components_query_envelope("DIN")) is None


@pytest.mark.asyncio
async def test_stale_cloud_guard_after_queue_never_creates_raw_request():
    hass = SimpleNamespace(data={})
    site = core.coordinator_for(hass, site_id=123)
    entered, release = asyncio.Event(), asyncio.Event()
    state = SimpleNamespace(current=True)
    raw = Session()
    session = http.controlled_session(hass, raw, is_current=lambda: state.current)
    async def hold():
        async with site.write_context("hold"):
            entered.set()
            await release.wait()
    active = asyncio.create_task(hold())
    await entered.wait()
    async def send():
        async with session.post("https://fleet/api/1/energy_sites/123/backup"):
            pass
    queued = asyncio.create_task(send())
    await settle()
    state.current = False
    release.set()
    await active
    with pytest.raises(core.ControlRequestStale):
        await queued
    assert raw.posts == []


@pytest.mark.asyncio
async def test_per_request_fences_survive_queue_and_are_stripped_from_raw_request():
    hass = SimpleNamespace(data={})
    site = core.coordinator_for(hass, site_id=123)
    raw = Session()
    session = http.controlled_session(hass, raw)
    state = SimpleNamespace(allowed=True)
    entered, release = asyncio.Event(), asyncio.Event()
    rejected = []
    async def hold():
        async with site.write_context("hold"):
            entered.set()
            await release.wait()
    active = asyncio.create_task(hold())
    await entered.wait()
    def final_fence():
        if not state.allowed:
            raise ValueError("exact original fence")
    async def send():
        async with session.request("POST", "https://fleet/api/1/energy_sites/123/time_of_use_settings",
                _power_sync_before_send=final_fence, _power_sync_is_current=lambda: True,
                _power_sync_on_rejected=lambda: rejected.append("unsent")):
            pass
    queued = asyncio.create_task(send())
    await settle()
    state.allowed = False
    release.set()
    await active
    with pytest.raises(ValueError, match="exact original fence"):
        await queued
    assert rejected == ["unsent"] and raw.posts == []
    async with session.request("POST", "https://fleet/api/1/energy_sites/123/backup",
            _power_sync_is_current=lambda: True, _power_sync_before_send=lambda: None,
            _power_sync_on_rejected=lambda: rejected.append("wrong"),
            _power_sync_deadline=site._clock() + 60, json={"reserve": 80}):
        pass
    assert raw.posts[0][2] == {"json": {"reserve": 80}}
    assert rejected == ["unsent"]
    assert session.request("GET", "https://fleet/api/1/energy_sites/123/live_status") is raw.get_result


@pytest.mark.asyncio
async def test_request_deadline_rejection_marks_unsent_but_driver_error_does_not():
    hass = SimpleNamespace(data={})
    site = core.coordinator_for(hass, site_id=123)
    raw = Session()
    session = http.controlled_session(hass, raw)
    rejected = []
    with pytest.raises(core.ControlDeadlineExpired):
        async with session.post("https://fleet/api/1/energy_sites/123/backup",
                _power_sync_deadline=site._clock() - 1,
                _power_sync_on_rejected=lambda: rejected.append("expired unsent")):
            pass
    assert rejected == ["expired unsent"] and raw.posts == []
    class Broken(Reply):
        async def __aenter__(self):
            raise OSError("driver lost response")
    raw.reply = Broken()
    with pytest.raises(OSError, match="driver lost response"):
        await session.post("https://fleet/api/1/energy_sites/123/backup",
                           _power_sync_on_rejected=lambda: rejected.append("wrong"))
    assert rejected == ["expired unsent"]
    assert site.snapshot()["uncertain_effects"]


@pytest.mark.asyncio
@pytest.mark.parametrize("await_request", [False, True])
async def test_driver_task_rechecks_captured_scope_before_creating_raw_request(await_request):
    hass = SimpleNamespace(data={})
    raw = Session()
    session = http.controlled_session(hass, raw)
    permissions = iter((True, False))
    rejected = []
    request = session.post("https://fleet/api/1/energy_sites/123/backup",
                           _power_sync_is_current=lambda: next(permissions),
                           _power_sync_on_rejected=lambda: rejected.append("unsent"))
    with pytest.raises(core.ControlRequestStale):
        if await_request:
            await request
        else:
            async with request:
                pytest.fail("Revoked authority reached HTTP")
    assert rejected == ["unsent"] and raw.posts == []
    snapshot = core.coordinator_for(hass, site_id=123).snapshot()
    assert snapshot["effects"][0]["status"] == "not_submitted"
    assert snapshot["uncertain_effects"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("handoff,recovering,allowed", [
    (None, False, False), ("other-incident", False, False),
    ("incident", True, False), ("incident", False, True),
])
async def test_cloud_quarantine_honors_only_exact_supervised_settings_handoff(handoff, recovering, allowed):
    pending = {"id": "incident", "site_id": "00123", "resume_cloud": True,
               "updates": {"site_info.backup_reserve_percent": 80}}
    runtime = {"powerwall_settings_pending": pending, "powerwall_settings_cloud_handoff": handoff,
               "powerwall_settings_recovering": recovering}
    original = deepcopy(runtime)
    hass = SimpleNamespace(data={"power_sync": {"entry-a": runtime, "entry-b": {}}})
    raw = Session()
    session = http.controlled_session(hass, raw, entry_id="entry-a")
    async def send(site_id):
        async with session.post(f"https://fleet/api/1/energy_sites/{site_id}/operation"):
            pass
    if allowed:
        await send(123)
    else:
        with pytest.raises(core.ControlRequestStale):
            await send(123)
    # A different site's cloud control remains independent.
    await send(456)
    assert len(raw.posts) == (2 if allowed else 1)
    assert runtime == original and runtime["powerwall_settings_pending"] is pending


@pytest.mark.asyncio
async def test_cloud_quarantine_created_during_queue_blocks_unrelated_writer_entry():
    hass = SimpleNamespace(data={"power_sync": {"settings-owner": {}, "spike-owner": {}}})
    site = core.coordinator_for(hass, site_id=123)
    entered, release = asyncio.Event(), asyncio.Event()
    raw = Session()
    session = http.controlled_session(hass, raw)
    rejected = []
    async def hold():
        async with site.write_context("previous signed RPC"):
            entered.set()
            await release.wait()
    active = asyncio.create_task(hold())
    await entered.wait()
    async def spike():
        async with session.post("https://fleet/api/1/energy_sites/123/time_of_use_settings",
                                _power_sync_on_rejected=lambda: rejected.append("unsent")):
            pass
    queued = asyncio.create_task(spike())
    await settle()
    incident = {"id": "unknown-write", "site_id": "123"}
    hass.data["power_sync"]["settings-owner"]["powerwall_settings_pending"] = incident
    release.set()
    await active
    with pytest.raises(core.ControlRequestStale):
        await queued
    assert raw.posts == [] and rejected == ["unsent"]
    assert hass.data["power_sync"]["settings-owner"]["powerwall_settings_pending"] is incident


@pytest.mark.asyncio
async def test_missing_incident_site_uses_entry_identity_then_fails_closed_if_unidentifiable():
    entry = SimpleNamespace(data={"tesla_energy_site_id": "00123"}, options={})
    incident = {"id": "unknown-scope"}
    hass = SimpleNamespace(data={"power_sync": {"entry": {"entry": entry,
                                                           "powerwall_settings_pending": incident}}})
    raw = Session()
    session = http.controlled_session(hass, raw)
    with pytest.raises(core.ControlRequestStale):
        async with session.post("https://fleet/api/1/energy_sites/123/operation"):
            pass
    async with session.post("https://fleet/api/1/energy_sites/456/operation"):
        pass
    hass.data["power_sync"]["entry"].pop("entry")
    with pytest.raises(core.ControlRequestStale):
        async with session.post("https://fleet/api/1/energy_sites/456/operation"):
            pass
    assert len(raw.posts) == 1 and incident == {"id": "unknown-scope"}


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["00123", "456"])
async def test_settings_dispatch_preserves_epoch_through_canonical_and_secondary_site_queues(target):
    hass = SimpleNamespace(data={"power_sync": {"entry": {}}})
    runtime = hass.data["power_sync"]["entry"]
    runtime["powerwall_site_control"] = core.coordinator_for(hass, site_id=123)
    entry = SimpleNamespace(entry_id="entry", data={"tesla_energy_site_id": "123"}, options={})
    node = next(node for node in ast.parse((ROOT / "powerwall_local/dispatch.py").read_text()).body
                if isinstance(node, ast.AsyncFunctionDef) and node.name == "dispatch_powerwall_setting")
    import time
    namespace = {"__package__": f"{PKG}.powerwall_local", "asyncio": asyncio, "time": time,
                 "DOMAIN": "power_sync", "_LOGGER": logging.getLogger(__name__),
                 "_record_setting_observation": lambda *args: None}
    exec(compile(ast.Module([node], type_ignores=[]), "dispatch.py", "exec",
                 flags=__import__("__future__").annotations.compiler_flag), namespace)
    target_site = core.coordinator_for(hass, site_id=target)
    entered, release = asyncio.Event(), asyncio.Event()
    raw = Session()
    session = http.controlled_session(hass, raw)
    async def hold():
        async with target_site.write_context("hold"):
            entered.set()
            await release.wait()
    active = asyncio.create_task(hold())
    await entered.wait()
    async def cloud():
        async with session.post(f"https://fleet/api/1/energy_sites/{target}/backup"):
            return True
    queued = asyncio.create_task(namespace["dispatch_powerwall_setting"](
        hass, entry, site_id=target, updates={"site_info.backup_reserve_percent": 80},
        cloud_call=cloud, is_current=lambda: True, deadline=time.monotonic() + 5, label="reserve"))
    await settle()
    runtime["powerwall_settings_epoch"] = 1
    release.set()
    await active
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert raw.posts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("restoring", [False, True])
async def test_local_settings_tracked_child_retains_parent_policy_fence_and_restore_exception(restoring):
    import time
    hass = SimpleNamespace(data={"power_sync": {"entry": {}}})
    runtime = hass.data["power_sync"]["entry"]
    site = core.coordinator_for(hass, site_id=123)
    runtime["powerwall_site_control"] = site
    entry = SimpleNamespace(entry_id="entry", data={"tesla_energy_site_id": "123", "powerwall_local_din": "DIN"},
                            options={"tesla_control_method": "local"})
    state = {"default_real_mode": "backup"}
    actual_mutations = []
    raw = Session()
    def post(url, *args, **kwargs):
        signed = transport.signed_message_pb2.RoutableMessage()
        signed.ParseFromString(kwargs["data"])
        request = transport.transport_pb2.MessageEnvelope()
        request.ParseFromString(signed.protobuf_message_as_bytes)
        response = transport.transport_pb2.MessageEnvelope()
        if request.filestore.HasField("read_file_request"):
            response.filestore.read_file_response.file.blob = json.dumps(state).encode()
        else:
            actual_mutations.append("update")
            state.clear()
            state.update(json.loads(request.filestore.update_file_request.file.blob))
            response.filestore.update_file_response.SetInParent()
        wrapper = transport.signed_message_pb2.RoutableMessage()
        wrapper.protobuf_message_as_bytes = response.SerializeToString()
        return Reply(wrapper.SerializeToString())
    raw.post = post
    local = local_transport(site, raw)
    tree = ast.parse((ROOT / "powerwall_local/dispatch.py").read_text())
    names = {"_SettingsPolicyDenied", "write_config_confirmed", "dispatch_powerwall_setting", "_record_setting_observation"}
    nodes = [node for node in tree.body if getattr(node, "name", None) in names]
    namespace = {"__package__": f"{PKG}.powerwall_local", "asyncio": asyncio, "time": time,
                 "DOMAIN": "power_sync", "_LOGGER": logging.getLogger(__name__),
                 "get_local_transport": lambda *args: local,
                 "_LOCAL_FALLBACK_EXC": (transport.PowerwallLocalError, asyncio.TimeoutError)}
    exec(compile(ast.Module(nodes, type_ignores=[]), "dispatch.py", "exec",
                 flags=__import__("__future__").annotations.compiler_flag), namespace)
    entered, release = asyncio.Event(), asyncio.Event()
    permitted = SimpleNamespace(value=True)
    cloud_calls = []
    async def cloud():
        cloud_calls.append("fallback")
        return True
    async def hold():
        async with site.write_context("hold"):
            entered.set()
            await release.wait()
    async def exercise():
        active = asyncio.create_task(hold())
        await entered.wait()
        async def submit():
            return await namespace["dispatch_powerwall_setting"](
                hass, entry, site_id="123", updates={"default_real_mode": "autonomous"}, cloud_call=cloud,
                is_current=lambda: True, deadline=time.monotonic() + 5, label="setting")
        # The source dispatch intentionally creates tracked local children.
        # Start it in this exact logical operation task, not a test child.
        async def revoke():
            await settle()
            permitted.value = False
            release.set()
        revoker = asyncio.create_task(revoke())
        result = await submit()
        await asyncio.gather(active, revoker)
        return result
    async with site.operation(label="forcing", apply_guard=lambda: permitted.value):
        if restoring:
            async with site.operation(restoring=True, apply_guard=lambda: True):
                result = await exercise()
        else:
            result = await exercise()
    assert result is restoring
    assert actual_mutations == (["update"] if restoring else [])
    assert cloud_calls == []


@pytest.mark.asyncio
async def test_local_signature_expiry_after_queue_persists_intent_but_never_posts(monkeypatch):
    clock = SimpleNamespace(now=100)
    monkeypatch.setattr(transport, "time", SimpleNamespace(time=lambda: clock.now))
    site = core.PowerwallSiteControl(123)
    raw = Session()
    local = local_transport(site, raw)
    entered, release = asyncio.Event(), asyncio.Event()
    sequence = []
    async def hold():
        async with site.write_context("hold"):
            entered.set()
            await release.wait()
    active = asyncio.create_task(hold())
    await entered.wait()
    async def intent(expiry):
        sequence.append(("intent", expiry))
    queued = asyncio.create_task(local.post_v1r(envelope("teg", "dispatch_battery_power_request"), "DIN",
                                               before_submit=intent, before_send=lambda: sequence.append(("fence", None))))
    await settle()
    clock.now = 113
    release.set()
    await active
    with pytest.raises(transport.PowerwallLocalError, match="expired"):
        await queued
    assert sequence == [("intent", 112)] and raw.posts == []
    assert [effect["label"] for effect in site.snapshot()["effects"]] == ["hold"]


@pytest.mark.asyncio
async def test_http_cancelled_submitted_driver_drains_and_retains_unknown_obligation():
    hass = SimpleNamespace(data={})
    site = core.coordinator_for(hass, site_id=123)
    journal = {"id": "journal", "phase": "submitted"}
    site.set_obligations("existing_settings", journal)
    entered, release = asyncio.Event(), asyncio.Event()
    raw = Session(Reply(entered=entered, release=release))
    session = http.controlled_session(hass, raw)
    async def send():
        async with session.post("https://fleet/api/1/energy_sites/123/operation"):
            pass
    caller = asyncio.create_task(send())
    await entered.wait()
    caller.cancel()
    await settle()
    assert not caller.done()  # Final gate remains owned while the driver drains.
    assert site.snapshot()["effects"][0]["status"] == "submitted"
    assert site.snapshot()["obligations"]["existing_settings"] == journal
    caller.cancel()  # A second cancellation cannot drop the gate early.
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    snapshot = site.snapshot()
    assert snapshot["uncertain_effects"][0]["label"] == "cloud_operation"
    assert snapshot["obligations"]["existing_settings"] == journal


@pytest.mark.asyncio
async def test_local_submitted_cancellation_keeps_session_and_worker_until_completion():
    site = core.PowerwallSiteControl(123)
    entered, release = asyncio.Event(), asyncio.Event()
    raw = Session(Reply(entered=entered, release=release))
    local = local_transport(site, raw)
    task = asyncio.create_task(local.post_v1r(envelope("teg", "dispatch_battery_power_request"), "DIN"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not raw.closed
    assert site.snapshot()["uncertain_effects"][0]["label"] == "local_dispatch"
    release.set()
    await settle()
    assert raw.closed
    assert site.snapshot()["effects"][0]["status"] == "returned"


@pytest.mark.asyncio
async def test_full_config_transaction_prevents_disjoint_field_lost_update():
    site = core.PowerwallSiteControl(123)
    state = {"site_info": {"backup_reserve_percent": 15, "default_real_mode": "self_consumption"}}
    read_entered, release_read = asyncio.Event(), asyncio.Event()
    reads = []
    async def post(payload, din, **kwargs):
        request = transport.transport_pb2.MessageEnvelope()
        request.ParseFromString(payload)
        reply = transport.transport_pb2.MessageEnvelope()
        if request.filestore.HasField("read_file_request"):
            snapshot = deepcopy(state)
            reads.append(snapshot)
            if len(reads) == 1:
                read_entered.set()
                await release_read.wait()
            reply.filestore.read_file_response.file.blob = json.dumps(snapshot).encode()
            reply.filestore.read_file_response.hash = b"opaque-gateway-value"
        else:
            state.clear()
            state.update(json.loads(request.filestore.update_file_request.file.blob))
            reply.filestore.update_file_response.SetInParent()
        return transport.TEDAPIResponse(True, reply.SerializeToString())
    first = local_transport(site, Session())
    second = local_transport(site, Session())
    first.post_v1r = second.post_v1r = post
    one = asyncio.create_task(first.write_config("DIN", {"site_info.backup_reserve_percent": 80}))
    await read_entered.wait()
    two = asyncio.create_task(second.write_config("DIN", {"site_info.default_real_mode": "autonomous"}))
    await settle()
    assert len(reads) == 1
    release_read.set()
    assert await asyncio.gather(one, two) == [True, True]
    assert reads[1]["site_info"]["backup_reserve_percent"] == 80
    assert state["site_info"] == {"backup_reserve_percent": 80, "default_real_mode": "autonomous"}


def test_client_factory_binding_fences_pairing_runtime_and_site_changes():
    tree = ast.parse((ROOT / "powerwall_local/views.py").read_text())
    names = {"_control_site_id", "_bind_site_control"}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {"__package__": f"{PKG}.powerwall_local", "HomeAssistant": object, "ConfigEntry": object,
                 "PowerwallLocalClient": object, "DOMAIN": "power_sync",
                 "CONF_POWERWALL_LOCAL_DIN": "din", "CONF_POWERWALL_LOCAL_PRIVATE_KEY": "key",
                 "CONF_POWERWALL_LOCAL_VERSION": "version", "CONF_POWERWALL_LOCAL_PAIRED": "paired",
                 "CONF_POWERWALL_LOCAL_ENERGY_SITE_ID": "local_site",
                 "_desired_gateway_host": lambda entry: entry.data.get("host", "127.0.0.1")}
    exec(compile(ast.Module(nodes, type_ignores=[]), "views.py", "exec"), namespace)
    entry = SimpleNamespace(entry_id="entry", data={"tesla_energy_site_id": "123", "paired": True,
                                                    "din": "DIN", "key": "KEY"}, options={})
    runtime = {}
    hass = SimpleNamespace(data={"power_sync": {"entry": runtime}})
    client = SimpleNamespace(_transport=SimpleNamespace())
    namespace["_bind_site_control"](hass, entry, client)
    assert client._transport._control_current()
    assert client._transport._site_control is core.coordinator_for(hass, site_id=123)
    hass.data["power_sync"]["foreign"] = {"powerwall_site_control": client._transport._site_control,
                                         "force_charge_state": {"active": True}}
    assert not client._transport._control_current()
    assert client._control_matches_entry()  # Read-only cached client remains usable.
    del hass.data["power_sync"]["foreign"]
    entry.data["key"] = "REPAIRED"
    assert not client._transport._control_current()
    entry.data["key"] = "KEY"
    entry.data["tesla_energy_site_id"] = "456"
    assert not client._transport._control_current()
    entry.data["tesla_energy_site_id"] = "123"
    hass.data["power_sync"]["entry"] = {}
    assert not client._transport._control_current()


@pytest.mark.asyncio
async def test_signed_cloud_client_rechecks_expiry_after_queue_even_without_lan(monkeypatch):
    client_module = importlib.import_module(f"{PKG}.powerwall_local.client")
    import aiohttp
    clock = SimpleNamespace(now=100)
    monkeypatch.setattr(transport, "time", SimpleNamespace(time=lambda: clock.now))
    hass = SimpleNamespace(data={})
    site = core.coordinator_for(hass, site_id=123)
    raw = Session()
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: raw)
    signed = transport.signed_message_pb2.RoutableMessage()
    signed.signature_data.rsa_data.expires_at = 112
    client = object.__new__(client_module.PowerwallLocalClient)
    client._fleet_api_base, client._fleet_api_token, client._energy_site_id = "https://fleet", "token", 123
    client._din = "DIN"
    client._local_access_enabled = False
    client._transport = SimpleNamespace(build_signed_island_mode=lambda *args, **kwargs: signed.SerializeToString(),
                                        check_signed_expiry=transport.TEDAPIv1rTransport.check_signed_expiry)
    client._control_session = lambda session, **kwargs: http.controlled_session(hass, session, **kwargs)
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold():
        async with site.write_context("hold"):
            entered.set()
            await release.wait()
    active = asyncio.create_task(hold())
    await entered.wait()
    queued = asyncio.create_task(client._send_signed_device_command(off_grid=True))
    await settle()
    clock.now = 113
    release.set()
    await active
    assert await queued is False
    assert raw.posts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_source", ["marker", "binding", "operation", "unknown"])
async def test_cloud_foreign_owner_is_checked_at_final_gate(owner_source):
    owner = {"entry": SimpleNamespace(data={"tesla_energy_site_id": 123}, options={}),
             "force_charge_state": {"active": True, "end_time": 0}}
    hass = SimpleNamespace(data={"power_sync": {"entry-a": owner, "entry-b": {}}})
    raw = Session()
    session = http.controlled_session(hass, raw, entry_id="entry-b" if owner_source == "binding" else None)
    site = core.coordinator_for(hass, site_id="00123")
    rejected = []
    kwargs = {"_power_sync_on_rejected": lambda: rejected.append(True)}
    if owner_source == "marker":
        kwargs["_power_sync_entry_id"] = "entry-b"
    async def send():
        async with session.post("https://proxy/api/proxy/api/1/energy_sites/123/time_of_use_settings", **kwargs):
            pytest.fail("Foreign force was overwritten")
    with pytest.raises(core.ControlRequestStale):
        if owner_source == "operation":
            async with site.operation(entry_id="entry-b"):
                await send()
        else:
            await send()
    assert raw.posts == [] and rejected == [True]
    # The original owner keeps cleanup authority, and unrelated sites work.
    own = http.controlled_session(hass, raw, entry_id="entry-a")
    async with site.operation(entry_id="entry-a", restoring=True):
        async with own.post("https://fleet/api/1/energy_sites/123/operation"):
            pass
    async with session.post("https://fleet/api/1/energy_sites/456/operation"):
        pass
    assert len(raw.posts) == 2
    assert all(not any(key.startswith("_power_sync_") for key in call[2]) for call in raw.posts)
    assert owner["force_charge_state"]["active"] is True


@pytest.mark.asyncio
async def test_actual_tariff_upload_cannot_overwrite_other_entry_force_even_after_queue(monkeypatch):
    # Execute the production TOU uploader, including its final unsent receipt.
    from test_tesla_tariff_rate_limit import upload_harness
    upload, hass, raw, _clock = upload_harness.__wrapped__(monkeypatch)
    raw.responses = [200, 200]
    site = core.coordinator_for(hass, site_id=123)
    owner = {"entry": SimpleNamespace(data={"tesla_energy_site_id": "00123"}, options={}),
             "force_charge_state": {"active": False}}
    hass.data["power_sync"] = {"entry-a": owner, "entry-b": {"powerwall_site_control": site}}
    upload.__globals__["async_get_clientsession"] = lambda _hass: http.controlled_session(hass, raw)
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold():
        async with site.write_context("unrelated submitted effect", entry_id="entry-a"):
            entered.set()
            await release.wait()
    active = asyncio.create_task(hold())
    await entered.wait()
    status = {}
    queued = asyncio.create_task(upload(hass, "123", {"name": "entry-b-force"}, "token",
                                        entry_id="entry-b", max_retries=1, accepted_status=status))
    await settle()
    assert not queued.done() and raw.posts == []
    # Ownership arises after B's original caller checks and before final I/O.
    owner["force_charge_state"]["active"] = True
    release.set()
    await active
    assert await queued is False
    assert status == {"not_submitted": True} and raw.posts == []
    assert await upload(hass, "00123", {"name": "entry-a-cleanup"}, "token",
                        entry_id="entry-a", max_retries=1) is True
    assert await upload(hass, "456", {"name": "different-site"}, "token",
                        entry_id="entry-b", max_retries=1) is True
    assert len(raw.posts) == 2 and owner["force_charge_state"]["active"] is True


@pytest.mark.asyncio
async def test_preparation_reservation_fences_queued_foreign_writer_without_revoking_owner():
    hass, raw = SimpleNamespace(data={}), Session()
    site = core.coordinator_for(hass, site_id=123)
    session = http.controlled_session(hass, raw)
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold():
        async with site.write_context("earlier driver", entry_id="entry-a"):
            entered.set()
            await release.wait()
    holder = asyncio.create_task(hold())
    await entered.wait()
    async def send(owner):
        async with session.post("https://fleet/api/1/energy_sites/123/backup", _power_sync_entry_id=owner):
            pass
    foreign = asyncio.create_task(send("entry-b"))
    await settle()
    assert site.route_entry_id is None
    async with site.route_scope("entry-a"):
        # A has not yet published any active force state while preparing.
        own = asyncio.create_task(send("entry-a"))
        release.set()
        await holder
        with pytest.raises(core.ControlRequestStale):
            await foreign
        await own
        assert len(raw.posts) == 1
        assert core.foreign_site_owner(hass, "entry-a", 123) is None
    assert site.route_entry_id is None
