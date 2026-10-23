"""Typed Powerwall calibration deferral messages, separate from calibration detection.

The protocol defines positive whole hours and a finite gateway allowance.
It does not define cancellation or justify automatic retries of a deferral.
"""
from __future__ import annotations

from typing import Any

from .energy_device import transport_pb2

MAX_DELAY_HOURS = 2**32 - 1
DELAY_HOUR_OPTIONS = (1, 2, 3, 6, 12, 24)


def _strict_fields(payload: bytes) -> dict:
    fields = {}
    pos = 0
    def read():
        nonlocal pos
        value = 0
        for shift in range(0, 70, 7):
            if pos >= len(payload): raise ValueError("truncated varint")
            byte = payload[pos]; pos += 1
            value |= (byte & 127) << shift
            if byte < 128: return value
        raise ValueError("oversized varint")
    while pos < len(payload):
        tag = read(); field, wire = tag >> 3, tag & 7
        if not field or field in fields: raise ValueError("ambiguous field")
        if wire == 0: value = read()
        elif wire in (1, 2, 5):
            size = read() if wire == 2 else 8 if wire == 1 else 4
            if pos + size > len(payload): raise ValueError("truncated field")
            value = payload[pos:pos + size]; pos += size
        else: raise ValueError("unsupported wire type")
        fields[field] = (wire, value)
    return fields


def _typed_teg_body(payload: bytes, expected_field: int) -> bytes:
    outer = _strict_fields(payload)
    if any(field in outer for field in (4, 6, 7, 8, 9, 10, 11, 12, 15, 16)):
        raise ValueError("conflicting payload")
    if outer.get(5, (None,))[0] != 2: raise ValueError("missing TEG")
    teg = _strict_fields(outer[5][1])
    if set(teg) != {expected_field} or teg[expected_field][0] != 2:
        raise ValueError("unexpected TEG response")
    return teg[expected_field][1]


def validate_delay_hours(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= MAX_DELAY_HOURS:
        raise ValueError("delay_hours must be a positive whole number of hours")
    return value


def calibration_envelope(din: str, hours: int | None = None) -> Any:
    envelope = transport_pb2.MessageEnvelope()
    envelope.delivery_channel = transport_pb2.DELIVERY_CHANNEL_HERMES_COMMAND
    envelope.sender.authorized_client = 1
    envelope.recipient.din = din
    if hours is None:
        envelope.teg.get_delay_battery_calibration_status_request.SetInParent()
    else:
        envelope.teg.delay_battery_calibration_request.delay_hours = validate_delay_hours(hours)
    return envelope


def parse_calibration_status(payload: bytes | None) -> dict[str, Any]:
    unavailable = {"supported": False, "reason": "calibration_delay_status_unavailable"}
    if not payload:
        return unavailable
    try:
        _strict_fields(payload)
        envelope = transport_pb2.MessageEnvelope()
        envelope.ParseFromString(payload)
        if envelope.HasField("common") and envelope.common.HasField("error_response"):
            code = envelope.common.error_response.status.code
            return {"supported": False, "reason": "gateway_unimplemented" if code == 12 else "gateway_rejected", "gateway_status_code": code}
        fields = _strict_fields(_typed_teg_body(payload, 78))
        values = [fields.get(field, (0, 0)) for field in (1, 2, 3)]
        if any(wire != 0 or not isinstance(value, int) or not 0 <= value <= MAX_DELAY_HOURS for wire, value in values):
            return unavailable
        left, remaining, maximum = (value for wire, value in values)
        if left > maximum:
            return {"supported": False, "reason": "calibration_delay_status_invalid"}
        return {"supported": True, "verification": "gateway_status_readback",
            "calibration_delays_left": left,
            "max_calibration_delays": maximum,
            "calibration_delay_remaining_seconds": remaining}
    except Exception:
        return unavailable


def calibration_delay_acknowledged(payload: bytes | None) -> bool:
    if not payload:
        return False
    try:
        return _typed_teg_body(payload, 76) == b""
    except Exception:
        return False
