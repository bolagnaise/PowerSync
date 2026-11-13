"""Sender-bound stdio MCP bridge. No HTTP listener, HA token or bearer fallback.

Run with ``python -m diagnostics_access.bridge --profile codex --config FILE``
using the separately packaged bridge component root on PYTHONPATH.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import time
import urllib.request

from . import wire
from ha_read.reader import PROPERTIES
TOOL_PROPERTIES = {**PROPERTIES, "regression_get_summary": {"job": {"type": "string", "pattern": "^[a-f0-9]{32,64}$"}}}


def private_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 16384:
            raise wire.WireError("private_file_permissions")
        return wire.loads(os.read(fd, 16385), maximum=16384)
    finally:
        os.close(fd)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Bridge:
    def __init__(self, config, profile, transport=None):
        if config.get("profile") != profile or profile not in ("codex", "claude"):
            raise wire.WireError("profile_denied")
        if not all(wire.ID.fullmatch(config.get(key, "")) for key in ("root", "grant")):
            raise wire.WireError("invalid_identity")
        self.config, self.profile = config, profile
        self.transport = transport or self.http
        self.family = None
        self.refresh = None
        self.binding = None
        self.expires = 0

    def http(self, route, value):
        opener = urllib.request.build_opener(NoRedirect())
        request = urllib.request.Request(wire.ORIGIN + route, data=wire.json_bytes(value), headers={"Content-Type": "application/json", "User-Agent": "PowerSync-Diagnostics-Bridge/1.0"})
        with opener.open(request, timeout=15) as response:
            return wire.loads(response.read(420 * 1024 + 1), maximum=420 * 1024)

    def command(self, payload):
        c = self.config
        request = wire.packet(c["private_key"], "client", c["root"], c["grant"], payload)
        response = self.transport("/command", request)
        result = wire.verify(response, c["authority_key"], "authority", c["root"], c["grant"])
        if result.get("request_nonce") != request["metadata"]["nonce"] or result.get("error"):
            raise wire.WireError("request_denied")
        return result["value"]

    def connect(self):
        if self.family and time.time() < self.expires:
            return
        descriptor = self.command({"type": "describe"})
        binding = descriptor["binding"]
        if not binding["client"].startswith(self.profile + ":") or descriptor["kind"] not in ("ha_read", "optimizer_contribution"):
            raise wire.WireError("profile_denied")
        self.binding = binding
        self.kind = descriptor["kind"]
        self.operations = descriptor["operations"] if self.kind == "ha_read" else ["regression_get_summary"]
        self.family, self.refresh = secrets.token_hex(32), secrets.token_urlsafe(32)
        self.command({"type": "delegate", "family": self.family, "binding": binding, "refresh_hash": wire.secret_hash(self.refresh)})
        self.expires = time.time() + 3500

    def close(self):
        if self.family:
            try:
                self.command({'type':'close_family','family':self.family,'binding':self.binding})
            except Exception:
                pass
            self.family=None

    def call(self, operation, arguments):
        self.connect()
        if operation not in self.operations or operation not in TOOL_PROPERTIES:
            raise wire.WireError("operation_denied")
        # One serial stdio dispatcher owns refresh rotation. A lost refresh
        # response discards this family instead of reusing its spent capability.
        following = secrets.token_urlsafe(32)
        try:
            self.command({"type": "refresh", "family": self.family, "binding": self.binding,
                          "refresh_hash": wire.secret_hash(self.refresh), "next_hash": wire.secret_hash(following)})
        except Exception:
            self.family = None
            raise
        self.refresh = following
        if self.kind == "optimizer_contribution":
            if type(arguments) is not dict or set(arguments) != {"job"} or not wire.ID.fullmatch(arguments["job"]):
                raise wire.WireError("invalid_query")
            return self.command({"type": "job_status", "id": arguments["job"], "family": self.family, "binding": self.binding})
        page = secrets.token_hex(32)
        self.command({"type": "queue", "family": self.family, "binding": self.binding,
                      "id": page, "operation": operation, "arguments": arguments})
        until = time.monotonic() + 40
        while time.monotonic() < until:
            result = self.command({"type": "release", "family": self.family, "binding": self.binding, "id": page})
            if result["status"] == "released":
                text=result['result']
                if type(text) is not str or wire.secret_hash(text) != result["result_digest"]:
                    raise wire.WireError("result_changed")
                return wire.loads(text.encode())
            time.sleep(0.5)
        raise wire.WireError("read_timeout")

    def dispatch(self, method, params):
        if method == "initialize":
            version = params.get("protocolVersion")
            if version not in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"):
                raise wire.WireError("unsupported_protocol")
            return {"protocolVersion": version, "capabilities": {"tools": {}}, "serverInfo": {"name": "powersync-ha-" + self.profile, "version": "1.0.0"}}
        if method == "ping":
            return {}
        if method == "tools/list":
            self.connect()
            available = self.operations
            if self.kind == 'ha_read':
                available = self.call('ha_get_capabilities', {})['result']['supported']
            return {"tools": [{"name": name, "description": "Read owner-approved HA diagnostics.",
                "inputSchema": {"type": "object", "properties": TOOL_PROPERTIES[name], "additionalProperties": False},
                "annotations": {"readOnlyHint": True, "destructiveHint": False}}
                for name in available if name in TOOL_PROPERTIES]}
        if method == "tools/call" and type(params) is dict and not set(params) - {"name", "arguments", "_meta"}:
            result = self.call(params.get("name"), params.get("arguments", {}))
            return {"content": [{"type": "text", "text": json.dumps(result, allow_nan=False)}], "structuredContent": result}
        raise wire.WireError("unsupported_method")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("codex", "claude"), required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    bridge = Bridge(private_file(args.config), args.profile)
    while raw := sys.stdin.buffer.readline(65537):
        rpc_id = None
        try:
            if len(raw) > 65536 or not raw.endswith(b"\n"):
                raise wire.WireError("message_limit")
            request = wire.loads(raw, maximum=65536)
            if request.get("jsonrpc") != "2.0" or set(request) - {"jsonrpc", "id", "method", "params"}:
                raise wire.WireError("invalid_rpc")
            if "id" not in request:
                if request.get("method", "").startswith("notifications/"):
                    continue
                raise wire.WireError("invalid_rpc")
            rpc_id = request["id"]
            if type(rpc_id) not in (str, int):
                raise wire.WireError("invalid_rpc")
            result = bridge.dispatch(request.get("method"), request.get("params", {}))
            reply = {"jsonrpc": "2.0", "id": rpc_id, "result": result}
        except Exception:
            reply = {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32602, "message": "Diagnostic request denied or unavailable"}}
        sys.stdout.write(json.dumps(reply, allow_nan=False) + "\n")
        sys.stdout.flush()
    if bridge.family:
        bridge.close()


if __name__ == "__main__":
    main()
