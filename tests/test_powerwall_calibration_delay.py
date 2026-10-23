"""Calibration deferral protocol, capability gates and one-attempt semantics."""
import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_powerwall_v1r_teslemetry_parity import _bare_client, client_mod, transport_mod, transport_pb2

calibration = importlib.import_module(f"{client_mod.__package__}.calibration_delay")


def status_payload(left=2, remaining=0, maximum=3):
    envelope = transport_pb2.MessageEnvelope()
    value = envelope.teg.get_delay_battery_calibration_status_response
    value.SetInParent()
    value.calibration_delays_left = left
    value.calibration_delay_remaining_seconds = remaining
    value.max_calibration_delays = maximum
    return envelope.SerializeToString()


def ack_payload():
    envelope = transport_pb2.MessageEnvelope()
    envelope.teg.delay_battery_calibration_response.SetInParent()
    return envelope.SerializeToString()


@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, "24", 1 << 32, None])
def test_invalid_delay_never_reaches_transport(value):
    client = _bare_client()
    client._transport = SimpleNamespace(post_v1r=AsyncMock())
    with pytest.raises(ValueError): asyncio.run(client.delay_battery_calibration(value))
    client._transport.post_v1r.assert_not_awaited()


def test_exact_native_fields_and_strict_readback():
    for hours in (1, 24, (1 << 32) - 1):
        envelope = calibration.calibration_envelope("DIN--1", hours)
        assert envelope.teg.delay_battery_calibration_request.delay_hours == hours
        assert envelope.teg.WhichOneof("message") == "delay_battery_calibration_request"
    assert calibration.calibration_envelope("DIN--1").teg.HasField("get_delay_battery_calibration_status_request")
    parsed = calibration.parse_calibration_status(status_payload(2, 3600, 3))
    assert parsed["supported"] and parsed["calibration_delays_left"] == 2
    assert parsed["calibration_delay_remaining_seconds"] == 3600
    assert not calibration.parse_calibration_status(status_payload(4, 0, 3))["supported"]
    assert calibration.calibration_delay_acknowledged(ack_payload())
    assert not calibration.calibration_delay_acknowledged(status_payload())
    # A truncated length field or duplicate TEG payload must not be accepted.
    for raw in (b"", b"\x2a\x03\xe2\x04", ack_payload() * 2, status_payload() * 2):
        assert not calibration.calibration_delay_acknowledged(raw)
        assert not calibration.parse_calibration_status(raw)["supported"]
    # Canonical wire vectors are shared with the Cloud protocol tests.
    assert ack_payload().hex() == "2a03e20400"
    assert status_payload(2, 3600, 3).hex() == "2a0af20407080210901c1803"


@pytest.mark.parametrize("exhausted", [False, True])
def test_unsupported_or_exhausted_gateway_sends_no_delay(exhausted):
    error = transport_pb2.MessageEnvelope()
    error.common.error_response.status.code = 12
    raw = status_payload(0, 0, 3) if exhausted else error.SerializeToString()
    client = _bare_client()
    client._transport = SimpleNamespace(post_v1r=AsyncMock(return_value=transport_mod.TEDAPIResponse(True, raw)))
    intent = AsyncMock()
    with pytest.raises(client_mod.PowerwallLocalError):
        asyncio.run(client.delay_battery_calibration(1, before_command=intent))
    assert client._transport.post_v1r.await_count == 1
    intent.assert_not_awaited()
    if not exhausted:
        result = asyncio.run(client.get_battery_calibration_delay_status())
        assert result["reason"] == "gateway_unimplemented" and result["gateway_status_code"] == 12


@pytest.mark.parametrize("ack,readable", [(True, True), (False, True), (True, False)])
def test_one_attempt_and_independent_acknowledgement_and_status(ack, readable):
    async def run():
        client = _bare_client()
        events = []
        async def persist(): events.append("durable_intent")
        def guard(): events.append("guard")
        reads = 0
        async def post(raw, din, **options):
            nonlocal reads
            envelope = transport_pb2.MessageEnvelope(); envelope.ParseFromString(raw)
            if envelope.teg.HasField("get_delay_battery_calibration_status_request"):
                reads += 1; events.append("read")
                payload = status_payload(2, 0, 3) if reads == 1 else status_payload(1, 3600, 3) if readable else None
                return transport_mod.TEDAPIResponse(True, payload)
            assert "durable_intent" in events
            options["before_send"]()
            assert envelope.teg.delay_battery_calibration_request.delay_hours == 1
            events.append("delay")
            if not ack: raise asyncio.TimeoutError()
            return transport_mod.TEDAPIResponse(True, ack_payload())
        client._transport = SimpleNamespace(post_v1r=post)
        result = await client.delay_battery_calibration(1, before_command=persist, before_write=guard)
        assert events.count("delay") == 1 and events.count("read") == 2
        assert result["command_acknowledged"] is ack
        assert result["status"]["supported"] is readable
        assert result["retry_safe"] is False
    asyncio.run(run())
