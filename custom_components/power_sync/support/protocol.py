"""Closed diagnostic protocol shared with the isolated support Worker."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re

VERSION = 1
SETUP_WINDOW_SECONDS = 30 * 60
CATEGORIES = frozenset({"system", "entities", "optimizer", "control_trace", "logs", "history"})
MAX_RESULT = 256 * 1024
MAX_REQUEST = 16 * 1024
BROKER = "https://support.powersync.cc"
from .schema import CONTRACT
CONTRACT_DIGEST = hashlib.sha256(json.dumps(CONTRACT, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
ACTIONS = frozenset({"idle", "charge", "discharge", "export", "consume", "self_consumption", "solar_export", "off_grid"})
ROLES = frozenset({"battery_level", "battery_power", "solar_power", "grid_power", "home_load", "current_import_price", "current_export_price"})
UNITS = frozenset({"W", "kW", "%", "kWh", "Wh", "A", "V", "c/kWh", "$/kWh", "AUD/kWh", "GBP/kWh", "EUR/kWh", "p/kWh"})


class SupportError(ValueError):
    """An intentionally non-sensitive protocol error code."""


def encode(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False).encode()


def digest(value) -> str:
    return hashlib.sha256(encode(value)).hexdigest()


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def number(value):
    if type(value) in (int, float) and math.isfinite(value) and abs(value) < 1e15:
        return value
    return None


def reference(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def identifier(value) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[a-zA-Z0-9_-]{16,128}", value))


def query(value, *, now: float, start: float, end: float):
    if not isinstance(value, dict) or set(value) - {"category", "start", "end", "source"}:
        raise SupportError("invalid_query")
    category = value.get("category")
    if category not in CATEGORIES:
        raise SupportError("unsupported_category")
    result = {"category": category}
    if category in {"history", "control_trace", "logs"}:
        lo, hi = value.get("start", max(start, now - 600)), value.get("end", min(end, now))
        if number(lo) is None or number(hi) is None or not start <= lo <= hi <= min(end, now + 5) or hi - lo > 3600:
            raise SupportError("history_scope_denied")
        source = value.get("source", "support_recorder")
        if source not in {"support_recorder", "ha_recorder"} or (source == "ha_recorder" and category != "history"):
            raise SupportError("invalid_history_source")
        result.update(start=lo, end=hi, source=source)
    elif set(value) != {"category"}:
        raise SupportError("invalid_query")
    return result


def validate_evidence(value):
    """Reject unknown fields/text on restore and at the final upload boundary."""
    count = 0
    strings = {**CONTRACT["string_fields"], "action": ACTIONS, "type": ACTIONS,
        "planned_action": ACTIONS, "effective_action": ACTIONS,
        "role": ROLES, "ref": ROLES, "unit": UNITS}
    allowed = set(CONTRACT["numeric_fields"] + CONTRACT["boolean_fields"] + CONTRACT["object_fields"] + CONTRACT["array_fields"]) | set(strings) | {"integration_version", "input_revision"}

    def visit(v, key="", depth=0):
        nonlocal count
        count += 1
        if count > 30000 or depth > 12:
            raise SupportError("evidence_too_complex")
        if v is None:
            return
        if type(v) in (int, float):
            if key not in CONTRACT["numeric_fields"] or number(v) is None:
                raise SupportError("invalid_numeric_evidence")
        elif type(v) is bool:
            if key not in CONTRACT["boolean_fields"]:
                raise SupportError("invalid_boolean_evidence")
        elif type(v) is str:
            if key == "integration_version" and re.fullmatch(r"\d{1,4}(\.\d{1,6}){1,3}", v):
                return
            if key == "input_revision" and re.fullmatch(r"[a-f0-9]{64}", v):
                return
            if v not in strings.get(key, ()):
                raise SupportError("unapproved_text")
        elif type(v) is list:
            if key not in CONTRACT["array_fields"] or len(v) > 6000:
                raise SupportError("invalid_array")
            for item in v:
                visit(item, depth=depth + 1)
        elif type(v) is dict:
            if key and key not in CONTRACT["object_fields"]:
                raise SupportError("invalid_object_field")
            for name, item in v.items():
                if name not in allowed:
                    raise SupportError("unapproved_field")
                visit(item, name, depth + 1)
        else:
            raise SupportError("invalid_evidence")
    if type(value) is not dict:
        raise SupportError("invalid_evidence")
    visit(value)
    if len(encode(value)) > MAX_RESULT:
        raise SupportError("evidence_too_large")
