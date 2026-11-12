"""HA-wide read backend. A transport must supply a freshly verified grant.

There is intentionally no anonymous endpoint or fallback to a ticket grant.
Read adapters are installed explicitly; unsupported categories remain visible.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import hashlib
import hmac
import inspect
import json
import math
import re
import secrets
import time
from typing import Callable

TOOLS = frozenset({
    "ha_get_capabilities", "ha_search_entities", "ha_get_states", "ha_get_registry",
    "ha_get_history", "ha_get_statistics", "ha_get_logbook", "ha_get_automations",
    "ha_get_scripts", "ha_get_trace", "ha_get_integration_config", "ha_get_dashboard",
    "ha_read_configuration", "ha_get_logs", "ha_get_system", "ha_get_supervisor",
})
READERS = TOOLS - {"ha_get_capabilities", "ha_search_entities", "ha_get_states"}
SECRET_KEY = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|credential|private[_-]?key|cookie|client[_-]?secret)")
MAX_RESULT = 256 * 1024
_TEXT = {"type": "string", "maxLength": 256}
_IDS = {"type": "array", "items": _TEXT, "maxItems": 1000}
_PAGE = {"limit": {"type": "integer", "minimum": 1, "maximum": 250}, "cursor": {"type": "string", "maxLength": 4096}}
PROPERTIES = {
    "ha_get_capabilities": {},
    "ha_search_entities": {**_PAGE, "entity_ids": _IDS, "domain": _TEXT, "search": _TEXT},
    "ha_get_states": {**_PAGE, "entity_ids": _IDS, "domain": _TEXT, "search": _TEXT},
    "ha_get_registry": {**_PAGE, "registry": {"type": "string", "enum": ["entity", "device", "area", "floor", "label"]}},
    "ha_get_history": {**_PAGE, "entity_ids": _IDS, "start": {"type": "number"}, "end": {"type": "number"}},
    "ha_get_statistics": {**_PAGE, "statistic_ids": _IDS, "start": {"type": "number"}, "end": {"type": "number"}, "period": {"type": "string", "enum": ["5minute", "hour", "day", "week", "month"]}},
    "ha_get_logbook": {**_PAGE, "entity_ids": _IDS, "start": {"type": "number"}, "end": {"type": "number"}},
    "ha_get_automations": {**_PAGE, "entity_id": _TEXT},
    "ha_get_scripts": {**_PAGE, "entity_id": _TEXT},
    "ha_get_trace": {**_PAGE, "domain": {"type": "string", "enum": ["automation", "script"]}, "item_id": _TEXT, "run_id": _TEXT},
    "ha_get_integration_config": {**_PAGE, "entry_id": _TEXT},
    "ha_get_dashboard": {**_PAGE, "url_path": _TEXT},
    "ha_read_configuration": {"path": _TEXT},
    "ha_get_logs": {**_PAGE, "source": {"type": "string", "enum": ["core", "system", "power_sync"]}},
    "ha_get_system": {},
    "ha_get_supervisor": {"section": {"type": "string", "enum": ["info", "host", "addons", "backup_metadata"]}},
}


class ReadError(ValueError):
    """Closed error code, never a raw HA/provider exception."""

    def __init__(self, code):
        allowed = {"read_scope_denied", "result_limit", "unsupported_attribute",
                   "unsupported_reader", "invalid_or_stale_cursor", "invalid_query",
                   "invalid_page_size", "reader_unavailable", "unsupported_mcp_method"}
        super().__init__(code if type(code) is str and code in allowed else "reader_unavailable")


@dataclass(frozen=True)
class ReadContext:
    installation: str
    actor: str
    client: str
    epoch: int
    expires_at: float
    operations: frozenset[str]
    verify: Callable[["ReadContext", str], bool]

    def check(self, operation, now):
        try:
            allowed = (type(operation) is str and operation in TOOLS and operation in self.operations
                       and now < self.expires_at and self.verify(self, operation) is True)
        except Exception:
            allowed = False
        if not allowed:
            raise ReadError("read_scope_denied")

    def binding(self):
        return [self.installation, self.actor, self.client, self.epoch]


def sanitized(value, *, key="", depth=0, budget=None):
    """Bounded structured projection. Unknown objects never become repr text."""
    budget = [MAX_RESULT] if budget is None else budget
    budget[0] -= 24
    if budget[0] < 0 or depth > 20:
        raise ReadError("result_limit")
    if SECRET_KEY.search(key):
        return {"redacted": True}
    if type(value) is int:
        if value.bit_length() > 4096:
            raise ReadError("result_limit")
        budget[0] -= len(str(value))
        if budget[0] < 0:
            raise ReadError("result_limit")
        return value
    if value is None or type(value) is bool:
        return value
    if type(value) is float:
        return value if math.isfinite(value) else {"unavailable": True}
    if isinstance(value, Enum):
        return sanitized(value.value, key=key, depth=depth + 1, budget=budget)
    if isinstance(value, datetime):
        return value.isoformat()
    if type(value) is str:
        budget[0] -= len(value.encode())
        if budget[0] < 0 or len(value) > 32768:
            raise ReadError("result_limit")
        # Strip common credentials embedded in URLs; this never fetches a URL.
        value = re.sub(r"(?i)(https?://)[^/@\s]+:[^/@\s]+@", r"\1[redacted]@", value)
        value = re.sub(r"(?i)([?&](?:token|api_key|key|password|secret)=)[^&\s]+", r"\1[redacted]", value)
        return value
    if type(value) in (list, tuple, set, frozenset):
        if len(value) > 20000:
            raise ReadError("result_limit")
        return [sanitized(v, depth=depth + 1, budget=budget) for v in value]
    if type(value) is dict:
        result = {}
        for name, item in value.items():
            if type(name) is not str or len(name) > 1024:
                raise ReadError("unsupported_attribute")
            budget[0] -= len(name.encode())
            result[name] = sanitized(item, key=name, depth=depth + 1, budget=budget)
        return result
    return {"unavailable": True, "reason": "unsupported_attribute_type"}


class HAReadBackend:
    """All-domain cached-state reads with actor/epoch-bound opaque pagination."""

    def __init__(self, hass, *, adapters=None, wall=time.time):
        self.hass, self.wall = hass, wall
        self.adapters = dict(adapters or {})
        if self.adapters.keys() - READERS:
            raise ReadError("unsupported_reader")
        self._cursor_key = secrets.token_bytes(32)

    def _token(self, value):
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw + hmac.digest(self._cursor_key, raw, "sha256")).decode()

    def _cursor(self, token, expected):
        try:
            if type(token) is not str or len(token) > 4096:
                raise ValueError
            data = base64.b64decode(token, altchars=b"-_", validate=True)
            raw, signature = data[:-32], data[-32:]
            if not hmac.compare_digest(signature, hmac.digest(self._cursor_key, raw, "sha256")):
                raise ValueError
            value = json.loads(raw)
            if value["binding"] != expected["binding"] or value["query"] != expected["query"] or value["inventory"] != expected["inventory"] or self.wall() >= value["expires_at"]:
                raise ValueError
            if type(value["offset"]) is not int or value["offset"] < 0:
                raise ValueError
            return value["offset"]
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise ReadError("invalid_or_stale_cursor") from None

    async def read(self, context, operation, arguments=None):
        context.check(operation, self.wall())
        arguments = {} if arguments is None else arguments
        if type(arguments) is not dict or len(json.dumps(arguments)) > 16384:
            raise ReadError("invalid_query")
        if arguments.keys() - PROPERTIES[operation].keys():
            raise ReadError("invalid_query")
        for name, value in arguments.items():
            rule = PROPERTIES[operation][name]
            kind = rule["type"]
            if kind == "string" and (type(value) is not str or len(value) > rule.get("maxLength", 256) or "enum" in rule and value not in rule["enum"]):
                raise ReadError("invalid_query")
            if kind == "integer" and (type(value) is not int or not rule["minimum"] <= value <= rule["maximum"]):
                raise ReadError("invalid_query")
            if kind == "number" and (type(value) not in (int, float) or not math.isfinite(value)):
                raise ReadError("invalid_query")
            if kind == "array" and (type(value) is not list or len(value) > rule["maxItems"] or any(type(item) is not str or len(item) > 256 for item in value)):
                raise ReadError("invalid_query")
        if operation == "ha_get_capabilities":
            if arguments:
                raise ReadError("invalid_query")
            available = {"ha_get_capabilities", "ha_search_entities", "ha_get_states"} | self.adapters.keys()
            result = {"profile": "ha_read_v1", "schema_version": 1,
                      "supported": sorted(available & context.operations),
                      "unavailable": sorted(TOOLS - available),
                      "complete_profile": READERS <= self.adapters.keys(),
                      "all_entity_domains": True, "max_page_size": 250}
        elif operation in {"ha_search_entities", "ha_get_states"}:
            if arguments.keys() - {"entity_ids", "domain", "search", "limit", "cursor"}:
                raise ReadError("invalid_query")
            limit = arguments.get("limit", 100)
            if type(limit) is not int or not 1 <= limit <= 250:
                raise ReadError("invalid_page_size")
            for name in ("domain", "search"):
                if name in arguments and (type(arguments[name]) is not str or len(arguments[name]) > 256):
                    raise ReadError("invalid_query")
            ids = arguments.get("entity_ids")
            if ids is not None and (type(ids) is not list or len(ids) > 1000 or any(type(x) is not str or len(x) > 256 for x in ids)):
                raise ReadError("invalid_query")
            rows = sorted(self.hass.states.async_all(), key=lambda s: s.entity_id)
            if ids is not None:
                selected = set(ids)
                rows = [s for s in rows if s.entity_id in selected]
            if arguments.get("domain"):
                rows = [s for s in rows if s.entity_id.partition(".")[0] == arguments["domain"]]
            if arguments.get("search"):
                term = arguments["search"].casefold()
                rows = [s for s in rows if term in s.entity_id.casefold() or
                        type(s.attributes.get("friendly_name")) is str and term in s.attributes["friendly_name"].casefold()]
            query = {k: v for k, v in arguments.items() if k != "cursor"}
            expected = {"binding": context.binding(),
                        "query": hashlib.sha256(json.dumps([operation, query], sort_keys=True).encode()).hexdigest(),
                        "inventory": hashlib.sha256(json.dumps([s.entity_id for s in rows]).encode()).hexdigest()}
            offset = self._cursor(arguments["cursor"], expected) if "cursor" in arguments else 0
            selected = rows[offset:offset + limit]
            budget = [MAX_RESULT]
            result_rows = []
            for state in selected:
                raw = {"entity_id": state.entity_id, "domain": state.entity_id.partition(".")[0],
                       "state": state.state, "attributes": dict(state.attributes),
                       "last_changed": state.last_changed, "last_updated": state.last_updated,
                       "last_reported": getattr(state, "last_reported", None)}
                if operation == "ha_search_entities":
                    raw.pop("attributes")
                if SECRET_KEY.search(state.entity_id):
                    raw["state"], raw["attributes"] = {"redacted": True}, {"redacted": True}
                result_rows.append(sanitized(raw, budget=budget))
            more = offset + len(selected) < len(rows)
            result = {"records": result_rows, "total": len(rows), "coherence": "per_page_cache",
                      "next_cursor": self._token({**expected, "offset": offset + len(selected), "expires_at": self.wall() + 60}) if more else None}
        else:
            adapter = self.adapters.get(operation)
            if adapter is None:
                raise ReadError("reader_unavailable")
            try:
                value = adapter(dict(arguments))
                if inspect.isawaitable(value):
                    value = await value
                result = sanitized(value)
            except ReadError:
                raise
            except Exception:
                raise ReadError("reader_unavailable") from None
        # Consent can be revoked while an awaited local reader is running.
        context.check(operation, self.wall())
        payload = {"schema_version": 1, "source": "ha_cache_or_reader", "observed_at": self.wall(),
                   "untrusted_content": True, "result": result}
        if len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()) > MAX_RESULT:
            raise ReadError("result_limit")
        return payload


class MCPReadAPI:
    """Read-only MCP dispatch, independent from authentication/transport."""

    def __init__(self, backend):
        self.backend = backend

    async def call(self, context, method, params=None):
        if method == "tools/list":
            capabilities = (await self.backend.read(context, "ha_get_capabilities"))["result"]
            return {"tools": [{"name": name, "description": "Read approved Home Assistant diagnostics.",
                "inputSchema": {"type": "object", "properties": PROPERTIES[name], "additionalProperties": False},
                "annotations": {"readOnlyHint": True, "destructiveHint": False}}
                for name in capabilities["supported"]]}
        if method != "tools/call" or type(params) is not dict or set(params) - {"name", "arguments", "_meta"}:
            raise ReadError("unsupported_mcp_method")
        result = await self.backend.read(context, params.get("name"), params.get("arguments", {}))
        return {"structuredContent": result, "content": [{"type": "text", "text": json.dumps(result, allow_nan=False)}]}
