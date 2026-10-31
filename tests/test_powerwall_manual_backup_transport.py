"""Fresh native absence permits a start without issuing a late cancellation."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from test_powerwall_v1r_teslemetry_parity import _transport_without_key, transport_mod as m

NOW = 1_700_000_000
READ = 'get_backup_events_request'
CANCEL = 'cancel_manual_backup_event_request'
START = 'schedule_manual_backup_event_request'


def reply(field, *, manual=None, scheduled=False):
    envelope = m.transport_pb2.MessageEnvelope()
    getattr(envelope.teg, field).SetInParent()
    if field == 'get_backup_events_response':
        events = envelope.teg.get_backup_events_response
        if manual is not None:
            event = events.manual_backup_event.scheduling_info
            event.start_time.seconds, event.duration_seconds = manual
        if scheduled:
            event = events.backup_events.add()
            event.id = 'other-scheduled-event'
            event.sheduling_info.start_time.seconds = NOW + 500
            event.sheduling_info.duration_seconds = 60
    return m.TEDAPIResponse(True, envelope.SerializeToString())


def wire_transport(monkeypatch, responses, *, now=None, delay=None):
    transport = _transport_without_key()
    now = now or NS(value=NOW)
    monkeypatch.setattr(m.time, 'time', lambda: now.value)
    requests = []

    async def post(payload, din, **kwargs):
        envelope = m.transport_pb2.MessageEnvelope()
        envelope.ParseFromString(payload)
        operation = envelope.teg.WhichOneof('message')
        if delay is not None:
            delay(operation)
        if kwargs.get('before_send') is not None:
            kwargs['before_send']()
        requests.append((operation, envelope))
        outcome = responses[operation]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    transport.post_v1r = post
    return transport, requests, now


@pytest.mark.asyncio
@pytest.mark.parametrize('scheduled', [False, True])
async def test_fresh_typed_absence_skips_cancel_and_starts_manual_backup(monkeypatch, scheduled):
    transport, requests, _ = wire_transport(monkeypatch, {
        READ: reply('get_backup_events_response', scheduled=scheduled),
        CANCEL: m.TEDAPIResponse(False, None),
        START: reply('schedule_manual_backup_event_response'),
    })
    assert await transport.schedule_manual_backup('DIN', 60, end_ts=NOW+60) is True
    assert [operation for operation, _ in requests] == [READ, START]
    info = requests[-1][1].teg.schedule_manual_backup_event_request.scheduling_info
    assert info.start_time.seconds == NOW and info.duration_seconds == 60
    assert info.priority == (1 << 64)-1


@pytest.mark.parametrize('snapshot,absent', [
    ({'manual_backup': None, 'backup_events': []}, True),
    ({'manual_backup': None, 'backup_events': [{'start_time': NOW+500}]}, True),
    (None, False), ({}, False), ({'backup_events': []}, False),
    ({'manual_backup': None}, False), ({'manual_backup': None, 'backup_events': None}, False),
    ({'manual_backup': None, 'backup_events': ()}, False),
    ({'manual_backup': {}, 'backup_events': []}, False),
    ({'manual_backup': {'active': False, 'end_time': NOW-1}, 'backup_events': []}, False),
    ({'manual_backup': {'active': False, 'start_time': NOW+500}, 'backup_events': []}, False),
    ({'manual_backup': False, 'backup_events': []}, False),
])
def test_manual_absence_requires_explicit_strict_native_shape(snapshot, absent):
    assert m.manual_backup_is_absent(snapshot) is absent


@pytest.mark.asyncio
@pytest.mark.parametrize('manual', [(NOW-120, 60), (NOW, 60), (NOW+500, 60)])
async def test_present_manual_event_requires_cancel_ack_even_when_past_or_future(monkeypatch, manual):
    transport, requests, _ = wire_transport(monkeypatch, {
        READ: reply('get_backup_events_response', manual=manual),
        CANCEL: reply('cancel_manual_backup_event_response'),
        START: reply('schedule_manual_backup_event_response'),
    })
    assert await transport.schedule_manual_backup('DIN', 60, end_ts=NOW+60) is True
    assert [operation for operation, _ in requests] == [READ, CANCEL, START]


@pytest.mark.asyncio
@pytest.mark.parametrize('snapshot', [None, {}, {'manual_backup': None},
    {'manual_backup': None, 'backup_events': None}, {'manual_backup': {}, 'backup_events': []}])
async def test_unknown_or_malformed_read_requires_acknowledged_cancellation(monkeypatch, snapshot):
    transport, requests, _ = wire_transport(monkeypatch, {
        CANCEL: reply('cancel_manual_backup_event_response'),
        START: reply('schedule_manual_backup_event_response'),
    })
    transport.get_backup_events = AsyncMock(return_value=snapshot)
    assert await transport.schedule_manual_backup('DIN', 60, end_ts=NOW+60) is True
    assert [operation for operation, _ in requests] == [CANCEL, START]


@pytest.mark.asyncio
@pytest.mark.parametrize('read_outcome', [m.TEDAPIResponse(False, None),
    reply('schedule_manual_backup_event_response'), RuntimeError('read failed')])
async def test_failed_or_untyped_read_still_requires_cancel_ack(monkeypatch, read_outcome):
    transport, requests, _ = wire_transport(monkeypatch, {
        READ: read_outcome, CANCEL: reply('cancel_manual_backup_event_response'),
        START: reply('schedule_manual_backup_event_response'),
    })
    assert await transport.schedule_manual_backup('DIN', 60, end_ts=NOW+60) is True
    assert [operation for operation, _ in requests] == [READ, CANCEL, START]


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_outcome', [m.TEDAPIResponse(False, None),
    reply('schedule_manual_backup_event_response'), m.TEDAPIResponse(True, b'invalid'),
    TimeoutError('lost cancel')])
async def test_unacknowledged_or_lost_cancel_never_submits_start(monkeypatch, cancel_outcome):
    transport, requests, _ = wire_transport(monkeypatch, {
        READ: reply('get_backup_events_response', manual=(NOW+500, 60)),
        CANCEL: cancel_outcome,
    })
    if isinstance(cancel_outcome, BaseException):
        with pytest.raises(type(cancel_outcome)):
            await transport.schedule_manual_backup('DIN', 60, end_ts=NOW+60)
    else:
        assert await transport.schedule_manual_backup('DIN', 60, end_ts=NOW+60) is False
    assert [operation for operation, _ in requests] == [READ, CANCEL]


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['read', 'cancel', 'final_start'])
async def test_original_deadline_blocks_late_cancel_or_start(monkeypatch, boundary):
    now = NS(value=NOW)
    def delay(operation):
        if operation == {'read': READ, 'cancel': CANCEL, 'final_start': START}[boundary]:
            now.value = NOW+61
    transport, requests, _ = wire_transport(monkeypatch, {
        READ: reply('get_backup_events_response', manual=(NOW, 60) if boundary == 'cancel' else None),
        CANCEL: reply('cancel_manual_backup_event_response'),
        START: reply('schedule_manual_backup_event_response'),
    }, now=now, delay=delay)
    with pytest.raises(m.PowerwallLocalError, match='expired'):
        await transport.schedule_manual_backup('DIN', 60, end_ts=NOW+60)
    assert START not in [operation for operation, _ in requests]
    if boundary == 'read':
        assert [operation for operation, _ in requests] == [READ]


@pytest.mark.asyncio
@pytest.mark.parametrize('explicit_deadline', [False, True])
async def test_read_and_cancel_waits_cannot_extend_packet_end(monkeypatch, explicit_deadline):
    now = NS(value=NOW)
    def delay(operation):
        if operation in (READ, CANCEL):
            now.value += 10
    transport, requests, _ = wire_transport(monkeypatch, {
        READ: reply('get_backup_events_response', manual=(NOW, 60)),
        CANCEL: reply('cancel_manual_backup_event_response'), START: reply('schedule_manual_backup_event_response'),
    }, now=now, delay=delay)
    options = {'end_ts': NOW+60} if explicit_deadline else {}
    assert await transport.schedule_manual_backup('DIN', 60, **options) is True
    info = requests[-1][1].teg.schedule_manual_backup_event_request.scheduling_info
    assert info.start_time.seconds == NOW+20 and info.duration_seconds == 40
    assert info.start_time.seconds+info.duration_seconds == NOW+60


@pytest.mark.asyncio
async def test_read_cancellation_propagates_without_cancel_or_start(monkeypatch):
    transport, requests, _ = wire_transport(monkeypatch, {READ: asyncio.CancelledError()})
    with pytest.raises(asyncio.CancelledError):
        await transport.schedule_manual_backup('DIN', 60, end_ts=NOW+60)
    assert [operation for operation, _ in requests] == [READ]


@pytest.mark.asyncio
async def test_unexpected_cancel_reply_reports_only_bounded_schema_and_common_error(monkeypatch, caplog):
    envelope = m.transport_pb2.MessageEnvelope()
    envelope.common.error_response.status.code = 7
    envelope.common.error_response.status.message = 'denied ' + 'x'*500
    envelope.recipient.din = 'PRIVATE-DIN-NOT-FOR-LOGS'
    transport, _, _ = wire_transport(monkeypatch, {CANCEL: m.TEDAPIResponse(True, envelope.SerializeToString())})
    with caplog.at_level('WARNING'):
        assert await transport.cancel_manual_backup('DIN') is False
    assert 'error_code=7' in caplog.text and 'error_response' not in caplog.text
    assert 'common' in caplog.text and 'PRIVATE-DIN' not in caplog.text
    assert 'x'*161 not in caplog.text
