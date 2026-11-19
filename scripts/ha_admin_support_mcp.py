"""Session-local stdio MCP for owner-approved Home Assistant administration."""
from __future__ import annotations

import argparse
import asyncio
from hashlib import sha256
import json
import secrets
import signal
import sys
import time

from aiohttp import ClientError, ClientSession, ClientTimeout, TraceConfig, WSMsgType
from yarl import URL

ADMIN_PATH = "/api/power_sync/support/admin"
MAX_BYTES = 4 * 1024 * 1024
METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")


class SupportError(Exception):
    """A safe diagnostic message for the investigator."""


def tool(name, description, properties=None, required=()):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties or {},
            "required": list(required),
            "additionalProperties": False,
        },
    }


TOOLS = [
    tool("support_admin_prepare", "Generate the challenge the Home Assistant owner approves for this MCP process."),
    tool("support_admin_connect", "Sign in as the Home Assistant owner's temporary administrator after approval."),
    tool("support_admin_request", "Send a native Home Assistant REST API request as the temporary administrator.",
         {"method": {"type": "string", "enum": list(METHODS)}, "path": {"type": "string"}, "body": {}},
         ("method", "path")),
    tool("support_admin_websocket", "Send one native Home Assistant WebSocket API command and return its result.",
         {"command": {"type": "object", "required": ["type"]}}, ("command",)),
    tool("support_admin_logout", "Revoke the temporary Home Assistant administrator and end the repair session."),
]


def parse_origin(value):
    try:
        url = URL(value)
        if (url.scheme == "https" and url.host and url.user is None
                and url.path == "/" and not url.query_string and not url.fragment):
            return url.origin()
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("Use an HTTPS Home Assistant origin without a path or credentials")


async def reject_redirect(_session, _context, _params):
    raise SupportError("Home Assistant redirected the request")


class AdminClient:
    def __init__(self, origin, client):
        self.origin = origin
        self.client = client
        self.verifier = None
        self.token = None
        self.expires_at = 0
        self.renew_at = None

    def prepare(self):
        if self.token:
            raise SupportError("Log out of the current session before starting another")
        if self.verifier is None:
            self.verifier = secrets.token_hex(32)
        return {"challenge": sha256(self.verifier.encode()).hexdigest()}

    async def connect(self):
        if self.token or self.verifier is None:
            raise SupportError("Prepare a new challenge first")
        status, result = await self.http("POST", self.origin.with_path(ADMIN_PATH + "/redeem"),
                                         {"verifier": self.verifier})
        if status != 200 or not self.valid_lease(result):
            raise SupportError("Home Assistant did not grant access. Check owner approval and expiry")
        self.verifier = None
        self.update_lease(result)
        return {"connected": True, "expires_at": self.expires_at}

    @staticmethod
    def valid_lease(result):
        return (type(result) is dict and isinstance(result.get("access_token"), str)
                and type(result.get("expires_at")) is int and result["expires_at"] > time.time()
                and type(result.get("renew_at")) is int and result["renew_at"] < result["expires_at"])

    def update_lease(self, result):
        self.token = result["access_token"]
        self.expires_at = result["expires_at"]
        self.renew_at = max(result["renew_at"], time.time() + 1)

    def renewal_delay(self):
        if self.renew_at is None or self.renew_at >= self.expires_at or self.expires_at <= time.time():
            return None
        return max(0, self.renew_at - time.time())

    async def renew(self):
        try:
            status, result = await self.http("POST", self.origin.with_path(ADMIN_PATH),
                                             {"action": "renew"}, headers=self.auth())
            if status in (401, 403):
                self.renew_at = None
                return
            if status != 200 or not self.valid_lease(result):
                raise SupportError("Could not renew the administrator session")
            self.update_lease(result)
        except (SupportError, ClientError, TimeoutError):
            # A missed renewal must never revive an expired grant.
            self.renew_at = time.time() + 15

    def auth(self):
        if not self.token or self.expires_at <= time.time():
            raise SupportError("No active Home Assistant administrator session")
        return {"Authorization": "Bearer " + self.token}

    async def http(self, method, url, body=None, *, headers=None):
        async with self.client.request(method, url, json=body, headers=headers, allow_redirects=False) as response:
            if 300 <= response.status < 400:
                raise SupportError("Home Assistant redirected the request")
            raw = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                raw.extend(chunk)
                if len(raw) > MAX_BYTES:
                    raise SupportError("Home Assistant response exceeds the tool limit")
            if not raw:
                return response.status, None
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeError):
                try:
                    result = raw.decode("utf-8")
                except UnicodeError:
                    raise SupportError("Home Assistant returned a binary response") from None
            return response.status, result

    async def request(self, method, path, body=None):
        if method not in METHODS or not isinstance(path, str):
            raise SupportError("Supply a native Home Assistant REST method and path")
        route = URL(path)
        if (route.host or route.scheme or route.fragment or not route.path.startswith("/api/")
                or {".", ".."}.intersection(route.path.split("/")) or "\\" in route.path or "%" in route.path):
            raise SupportError("Use a path within the configured Home Assistant API")
        # The bridge owns session credentials and lifecycle responses.
        if route.path == ADMIN_PATH or route.path.startswith(ADMIN_PATH + "/"):
            raise SupportError("Use the administrator session tools for login and logout")
        url = self.origin.with_path(route.path).with_query(route.query)
        status, result = await self.http(method, url, body, headers=self.auth())
        return {"status": status, "data": result}

    async def websocket(self, command):
        if type(command) is not dict or not isinstance(command.get("type"), str):
            raise SupportError("Supply a native Home Assistant WebSocket command")
        self.auth()
        target = self.origin.with_scheme("wss").with_path("/api/websocket")
        async with asyncio.timeout(25):
            async with self.client.ws_connect(target, max_msg_size=MAX_BYTES) as ws:
                await self.receive(ws, expected="auth_required")
                self.auth()
                await ws.send_json({"type": "auth", "access_token": self.token})
                await self.receive(ws, expected="auth_ok")
                await ws.send_json({**command, "id": 1})
                return await self.receive(ws, expected="result", message_id=1)

    @staticmethod
    async def receive(ws, *, expected, message_id=None):
        message = await ws.receive()
        if message.type != WSMsgType.TEXT:
            raise SupportError("Home Assistant closed the WebSocket before returning a result")
        value = message.json()
        if type(value) is not dict or value.get("type") != expected:
            raise SupportError("Home Assistant did not return the expected WebSocket result")
        if message_id is not None and value.get("id") != message_id:
            raise SupportError("Home Assistant returned an unrelated WebSocket result")
        return value

    async def logout(self):
        if not self.token:
            self.verifier = None
            return {"closed": True}
        status, result = await self.http("POST", self.origin.with_path(ADMIN_PATH),
                                         {"action": "revoke"},
                                         headers={"Authorization": "Bearer " + self.token})
        if status != 200 or result != {"state": "closed"}:
            raise SupportError("Home Assistant did not confirm logout. Check owner access status")
        self.token = self.verifier = None
        self.expires_at = 0
        self.renew_at = None
        return {"closed": True}


async def dispatch(client, request):
    if type(request) is not dict or request.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
    if "id" not in request:
        return None
    response = {"jsonrpc": "2.0", "id": request["id"]}
    method = request.get("method")
    if method == "initialize":
        params = request.get("params", {})
        version = params.get("protocolVersion") if isinstance(params, dict) else None
        response["result"] = {
            "protocolVersion": version if version in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25") else "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "powersync-ha-admin-support", "version": "1.0.0"},
        }
    elif method == "ping":
        response["result"] = {}
    elif method == "tools/list":
        response["result"] = {"tools": TOOLS}
    elif method == "tools/call":
        response["result"] = await call_tool(client, request.get("params"))
    else:
        response["error"] = {"code": -32601, "message": "Unknown method"}
    return response


async def call_tool(client, params):
    error = None
    try:
        if not isinstance(params, dict) or not isinstance(params.get("arguments", {}), dict):
            raise SupportError("Invalid tool arguments")
        arguments = params.get("arguments", {})
        match params.get("name"):
            case "support_admin_prepare" if not arguments:
                result = client.prepare()
            case "support_admin_connect" if not arguments:
                result = await client.connect()
            case "support_admin_request":
                result = await client.request(**arguments)
            case "support_admin_websocket":
                result = await client.websocket(**arguments)
            case "support_admin_logout" if not arguments:
                result = await client.logout()
            case _:
                raise SupportError("Unknown tool or arguments")
    except SupportError as exc:
        error = str(exc)
    except (TypeError, ValueError):
        error = "Invalid arguments or unexpected Home Assistant response"
    except (ClientError, TimeoutError):
        error = "Home Assistant did not return a response. Check state before repeating a write"
    return {"isError": error is not None, "content": [
        {"type": "text", "text": error if error is not None else json.dumps(result)}
    ]}


async def serve(origin):
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_BYTES)
    transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    task = asyncio.current_task()
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    trace = TraceConfig()
    trace.on_request_redirect.append(reject_redirect)
    async with ClientSession(timeout=ClientTimeout(total=20), trace_configs=[trace]) as session:
        client = AdminClient(origin, session)
        try:
            while True:
                delay = client.renewal_delay()
                if delay == 0:
                    await client.renew()
                    continue
                try:
                    line = await asyncio.wait_for(reader.readline(), delay)
                except TimeoutError:
                    await client.renew()
                    continue
                if not line:
                    break
                try:
                    request = json.loads(line)
                except (ValueError, UnicodeError):
                    print(json.dumps({"jsonrpc": "2.0", "id": None,
                                      "error": {"code": -32700, "message": "Invalid JSON"}}), flush=True)
                    continue
                response = await dispatch(client, request)
                if response is not None:
                    print(json.dumps(response), flush=True)
        except (asyncio.CancelledError, BrokenPipeError, ValueError):
            pass
        finally:
            transport.close()
            if client.token:
                try:
                    async with asyncio.timeout(5):
                        await client.logout()
                except (SupportError, ClientError, TimeoutError):
                    print("Home Assistant did not confirm support logout", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True, type=parse_origin)
    args = parser.parse_args()
    asyncio.run(serve(args.origin))


if __name__ == "__main__":
    main()
