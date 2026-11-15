"""Advanced backup preparation reuses owned service/recovery semantics."""
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from conftest import load_power_sync_submodule
from test_powerwall_fixed_output import fixed_config, fixed_snapshot

m = load_power_sync_submodule("powerwall_local.backup_preparation", "backup_preparation_test")
transport = load_power_sync_submodule("powerwall_local.transport", "backup_preparation_test")


def fixture():
    saved, calls, live = {}, [], {"operation": None, "readable": True, "can_start": True}
    behavior = {"fail": False, "allowed": True}

    async def load(): return deepcopy(saved)
    async def save(value): saved.clear(); saved.update(deepcopy(value))
    async def status(): return deepcopy(live)

    async def start(minutes, context, requested_at):
        assert saved["start"]["phase"] == "prepared"
        calls.append(("start", minutes, requested_at))
        if behavior["fail"]:
            live["operation"] = {"operation_id": "owned", "phase": "recovery_required", "active": True, "ends_at": requested_at + minutes * 60, "can_cancel": True}
            raise RuntimeError("unknown")
        live["operation"] = {"operation_id": "owned", "phase": "active", "active": True, "ends_at": requested_at + minutes * 60, "can_cancel": True}

    async def stop(operation_id, context):
        calls.append(("stop", operation_id))
        live["operation"] = None

    async def reconcile(context):
        if live["operation"]:
            live["operation"]["phase"] = "active"
    control = lambda: m.BackupPreparationControl(load=load, save=save, status=status, start=start, stop=stop, allowed=lambda: behavior["allowed"], reconcile=reconcile)
    return control, behavior, calls, live, saved


@pytest.mark.asyncio
async def test_start_is_durable_and_reconstruction_or_duplicate_cannot_extend_it():
    factory, behavior, calls, live, saved = fixture()
    body = {"request_id": "start", "duration_minutes": 60}
    first = await factory().execute(body)
    deadline = first["operation"]["ends_at"]
    replay = await factory().execute(body)
    assert replay["replayed"] and replay["operation"]["ends_at"] == deadline
    assert len(calls) == 1
    with pytest.raises(ValueError, match="conflict"):
        await factory().execute({**body, "duration_minutes": 30})
    with pytest.raises(ValueError, match="changed"):
        await factory().execute({"request_id": "stop", "operation": "cancel", "operation_id": "successor"})
    assert len(calls) == 1
    behavior["allowed"] = False
    await factory().execute({"request_id": "stop", "operation": "cancel", "operation_id": "owned"})
    assert calls[-1] == ("stop", "owned"), "owned cleanup survives loss of acquisition permission"


@pytest.mark.asyncio
async def test_unknown_start_is_not_replayed_and_cleanup_is_required_before_a_new_start():
    factory, behavior, calls, live, saved = fixture()
    behavior["fail"] = True
    with pytest.raises(RuntimeError):
        await factory().execute({"request_id": "start", "duration_minutes": 60})
    assert saved["start"]["phase"] == "prepared"
    replay = await factory().execute({"request_id": "start", "duration_minutes": 60})
    assert replay["replayed"] and not replay["success"] and len(calls) == 1
    assert (await factory().read())["recovery_required"]
    with pytest.raises(ValueError, match="owned or unavailable"):
        await factory().execute({"request_id": "new", "duration_minutes": 60})
    await factory().execute({"request_id": "reconcile", "operation": "reconcile"})
    assert not (await factory().read())["recovery_required"]
    assert calls == [("start", 60, saved["start"]["started_at"])]


@pytest.mark.parametrize("minutes", [True, None, "60", 0, 1441, 1.5])
@pytest.mark.asyncio
async def test_invalid_backup_window_never_calls_the_service(minutes):
    factory, behavior, calls, live, saved = fixture()
    with pytest.raises(ValueError):
        await factory().execute({"request_id": "start", "duration_minutes": minutes})
    assert not calls and not saved


def test_backup_does_not_enable_grid_charging_or_bypass_recorded_native_caps():
    m.validate_backup_preparation(fixed_config(), fixed_snapshot())
    for options, sample in [({"optimization_allow_grid_charge": False}, {}), ({}, {"grid_charging_enabled": False}),
                           ({"optimization_grid_charge_soc_cap": .8}, {}), ({}, {"imported": 0}),
                           ({"native_site_import_cap_w": 1000}, {}), ({"native_site_export_cap_w": 0}, {})]:
        with pytest.raises(ValueError):
            m.validate_backup_preparation(fixed_config(**options), fixed_snapshot(**sample))


def test_manual_backup_fingerprint_distinguishes_replacement_with_the_same_end():
    first = {"start_time": 1000, "duration_seconds": 600, "priority": (1 << 64) - 1}
    assert transport.manual_backup_matches_window({"manual_backup": first}, 1600, first) is True
    replacement = {**first, "start_time": 1100, "duration_seconds": 500}
    assert transport.manual_backup_matches_window({"manual_backup": replacement}, 1600, first) is False
    assert transport.manual_backup_matches_window(None, 1600, first) is None


@pytest.mark.parametrize('events, allowed', [
    (None, False), ({'manual_backup': None}, False),
    ({'manual_backup': {'start_time': 10, 'duration_seconds': 20}, 'backup_events': []}, True),
    ({'manual_backup': {'start_time': 100, 'duration_seconds': 60}, 'backup_events': []}, False),
    ({'manual_backup': None, 'backup_events': []}, True),
    ({'manual_backup': None, 'backup_events': [{'start_time': 90, 'duration_seconds': 20}]}, False),
    ({'manual_backup': None, 'backup_events': [{'start_time': 160, 'duration_seconds': 20}]}, True),
    ({'manual_backup': None, 'backup_events': [{'start_time': 10, 'duration_seconds': 20}]}, True),
    ({'manual_backup': None, 'backup_events': [{}]}, False),
])
def test_new_native_window_requires_absence_and_no_scheduled_overlap(events, allowed):
    assert transport.backup_window_available(events, 100, 160) is allowed


@pytest.mark.asyncio
@pytest.mark.parametrize('events', [None, {'manual_backup': {'start_time': 10000, 'duration_seconds': 60}, 'backup_events': []},
    {'manual_backup': None, 'backup_events': [{'start_time': 10000, 'duration_seconds': 60}]}])
async def test_strict_native_transport_never_cancels_an_existing_or_unknown_window(monkeypatch, events):
    client = object.__new__(transport.TEDAPIv1rTransport)
    client.get_backup_events = AsyncMock(return_value=events)
    client.cancel_manual_backup = AsyncMock(return_value=True)
    client.post_v1r = AsyncMock()
    monkeypatch.setattr(transport.time, 'time', lambda: 10000)
    with pytest.raises(Exception, match='occupied or unavailable'):
        await client.schedule_manual_backup('DIN', 60, end_ts=10060, replace_existing=False, start_ts=10000)
    client.cancel_manual_backup.assert_not_awaited()
    client.post_v1r.assert_not_awaited()


@pytest.mark.asyncio
async def test_strict_native_transport_submits_the_prejournaled_fingerprint(monkeypatch):
    client = object.__new__(transport.TEDAPIv1rTransport)
    client.get_backup_events = AsyncMock(return_value={'manual_backup': None, 'backup_events': []})
    client.cancel_manual_backup = AsyncMock()
    reply = transport.transport_pb2.MessageEnvelope()
    reply.teg.schedule_manual_backup_event_response.SetInParent()
    client.post_v1r = AsyncMock(return_value=transport.TEDAPIResponse(True, reply.SerializeToString()))
    monkeypatch.setattr(transport.time, 'time', lambda: 10020)
    assert await client.schedule_manual_backup('DIN', 60, end_ts=10060, replace_existing=False, start_ts=10000)
    packet = transport.transport_pb2.MessageEnvelope()
    packet.ParseFromString(client.post_v1r.await_args.args[0])
    info = packet.teg.schedule_manual_backup_event_request.scheduling_info
    assert info.start_time.seconds == 10000 and info.duration_seconds == 60 and info.priority == (1 << 64) - 1
    client.cancel_manual_backup.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_preserves_strict_window_parameters_at_transport_boundary():
    from pathlib import Path
    from types import SimpleNamespace as NS
    from conftest import load_power_sync_function
    forward = load_power_sync_function('schedule_max_backup', {}, path=Path(__file__).resolve().parents[1] / 'custom_components/power_sync/powerwall_local/client.py')
    client = NS(_din='DIN', _transport=NS(schedule_manual_backup=AsyncMock(return_value=True)))
    assert await forward(client, 60, end_ts=10060, replace_existing=False, start_ts=10000)
    client._transport.schedule_manual_backup.assert_awaited_once_with('DIN', 60, end_ts=10060, replace_existing=False, start_ts=10000)
