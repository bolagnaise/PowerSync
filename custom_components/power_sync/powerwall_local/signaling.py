"""Persistent WebSocket to Tesla's signaling service (Hermes).

The Tesla mobile app maintains a WebSocket connection to
``wss://signaling.vn.teslamotors.com/v1/mobile`` which keeps the
Powerwall gateway's cloud session alive. Without this connection,
``device_command`` REST calls are unreliable — the gateway may not
receive them if it has no active cloud session (408 timeouts).

This module implements the same persistent connection so that
PowerSync can reliably deliver off-grid / reconnect commands via
the REST ``device_command`` endpoint at any time.

Protocol details captured via mitmproxy of the Tesla mobile app and
corroborated by open-source implementations (lotharbach/tesla-hermes-signaling).

JWT exchange
~~~~~~~~~~~~
The WebSocket requires a purpose-specific JWT (``X-Jwt`` header) that
is **not** the Fleet API Bearer token. It is obtained by exchanging
the access token at::

    POST https://owner-api.teslamotors.com/api/1/users/jwt/hermes

This endpoint may reject Fleet API tokens (third-party ``client_id``)
and only accept owner-api tokens (``client_id="ownerapi"``). The client
tries the exchange with whatever token it has and logs clearly if it
fails.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import struct
import time
import uuid
from enum import Enum, IntEnum
from typing import Any, Callable, Awaitable

import aiohttp

_LOGGER = logging.getLogger(__name__)

SIGNALING_URL = "wss://signaling.vn.teslamotors.com/v1/mobile"
TESLA_APP_KEY = "D0C585CC5108DF5152B56EC365D5A89523765C18"

# Hermes JWT exchange endpoints — try PowerSync proxy first (works with
# psync_ tokens), then Fleet API, then owner-api.
HERMES_JWT_URLS = [
    "https://api.powersync.cc/api/proxy/hermes_jwt",
    "https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/users/jwt/hermes",
    "https://owner-api.teslamotors.com/api/1/users/jwt/hermes",
]

# Keepalive ping every 30s — Tesla app does similar to hold the session.
PING_INTERVAL_S = 30

# Reconnect backoff: starts at 2s, doubles each attempt, caps at 60s.
RECONNECT_BASE_S = 2
RECONNECT_MAX_S = 60

# Hermes JWT is cached and refreshed 60s before it would expire.
# Default lifetime assumed ~300s; refresh at 240s.
JWT_REFRESH_MARGIN_S = 60

HERMES_LIKELY_REQUIRED_SCOPES = ("user_data",)


# -----------------------------------------------------------------------
# Lightweight protobuf helpers (no generated code / no protoc dependency)
# -----------------------------------------------------------------------
# The hermes signaling protocol uses a simple protobuf schema. Rather
# than pulling in a .proto → generated module, we hand-encode/decode
# the tiny subset we need (CommandType field and STATUS_CODE_CLIENT_ACK).


class CommandType(IntEnum):
    """Subset of hermes CommandType values we care about."""

    SIGNED_COMMAND = 1047
    SIGNED_COMMAND_RESPONSE = 1048
    STREAMING_CONFIG = 1056


class StatusCode(IntEnum):
    """Subset of hermes StatusCode values."""

    OK = 0
    SERVER_ACK = 1202
    CLIENT_ACK = 2202
    TOO_MANY_REQUESTS = 1429


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Decode a protobuf varint starting at *pos*. Returns (value, new_pos)."""
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            return result, pos
        shift += 7
    return result, pos


def _write_varint(value: int) -> bytes:
    """Encode an integer as a protobuf varint."""
    parts = []
    while value > 0x7F:
        parts.append((value & 0x7F) | 0x80)
        value >>= 7
    parts.append(value & 0x7F)
    return bytes(parts)


def _parse_command_fields(data: bytes) -> dict[int, Any]:
    """Parse top-level fields from a CommandMessage protobuf.

    Returns a dict of {field_number: value}. Only handles varint and
    length-delimited wire types — enough for txid, commandType,
    statusCode, messageId.
    """
    fields: dict[int, Any] = {}
    pos = 0
    while pos < len(data):
        tag, pos = _read_varint(data, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:  # varint
            val, pos = _read_varint(data, pos)
            fields[field_num] = val
        elif wire_type == 2:  # length-delimited
            length, pos = _read_varint(data, pos)
            fields[field_num] = data[pos : pos + length]
            pos += length
        elif wire_type == 5:  # 32-bit
            fields[field_num] = data[pos : pos + 4]
            pos += 4
        elif wire_type == 1:  # 64-bit
            fields[field_num] = data[pos : pos + 8]
            pos += 8
        else:
            break  # unknown wire type — stop parsing
    return fields


def _parse_hermes_message(data: bytes) -> dict[int, Any] | None:
    """Unwrap HermesMessage → CommandMessage and parse fields.

    HermesMessage has a single field 1 (CommandMessage, length-delimited).
    """
    outer = _parse_command_fields(data)
    inner_bytes = outer.get(1)
    if isinstance(inner_bytes, bytes):
        return _parse_command_fields(inner_bytes)
    return None


def _build_client_ack(txid: bytes, message_id: bytes | None = None) -> bytes:
    """Build a HermesMessage with STATUS_CODE_CLIENT_ACK for the given txid.

    Wire format::

        HermesMessage {
            commandMessage (field 1) = CommandMessage {
                txid (field 1) = <txid>
                statusCode (field 7) = 2202
                messageId (field 12) = <message_id>  (if provided)
            }
        }
    """
    # Build inner CommandMessage
    inner = b""
    # field 1 (txid): wire type 2 (length-delimited)
    inner += _write_varint((1 << 3) | 2) + _write_varint(len(txid)) + txid
    # field 7 (statusCode): wire type 0 (varint)
    inner += _write_varint((7 << 3) | 0) + _write_varint(StatusCode.CLIENT_ACK)
    if message_id:
        # field 12 (messageId): wire type 2 (length-delimited)
        inner += (
            _write_varint((12 << 3) | 2)
            + _write_varint(len(message_id))
            + message_id
        )
    # Wrap in HermesMessage field 1
    outer = _write_varint((1 << 3) | 2) + _write_varint(len(inner)) + inner
    return outer


# -----------------------------------------------------------------------
# Connection state
# -----------------------------------------------------------------------


class SignalingState(str, Enum):
    """Connection lifecycle states."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    UNAVAILABLE = "unavailable"


def _decode_jwt_scopes(token: str) -> list[str]:
    """Best-effort decode of the unverified JWT ``scp`` claim for diagnostics."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return []
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
        scopes = data.get("scp")
        if isinstance(scopes, list):
            return [str(scope) for scope in scopes]
        if isinstance(scopes, str):
            return [scope for scope in scopes.split() if scope]
    except Exception:
        return []
    return []


def _missing_scope_hint(scopes: list[str]) -> str:
    """Return a short operator hint for known Hermes scope failures."""
    if not scopes:
        return ""

    missing = [
        scope for scope in HERMES_LIKELY_REQUIRED_SCOPES if scope not in scopes
    ]
    if not missing:
        return ""

    return (
        " Likely missing scope(s): "
        f"{', '.join(missing)}. Re-authorize Tesla with Profile Information "
        "enabled."
    )


def _is_missing_scope_response(status: int, body: str) -> bool:
    """Return True when Tesla says the token is valid but lacks endpoint scope."""
    if status != 403:
        return False
    body_l = body.lower()
    return "unauthorized missing scopes" in body_l or "missing scopes" in body_l


def _is_hermes_unsupported_response(status: int, body: str) -> bool:
    """Return True when Tesla permanently routes this token to signed_command."""
    if status != 412:
        return False
    body_l = body.lower()
    return "not supported" in body_l and "signed_command" in body_l


# Type alias: async callable that returns the Tesla access token.
# Called before each connection to get a fresh token for the hermes
# JWT exchange.
AccessTokenProvider = Callable[[], Awaitable[str | None]]


class TeslaSignalingClient:
    """Persistent WebSocket to Tesla's Hermes signaling service.

    Keeps the Powerwall gateway's cloud session alive so that
    ``device_command`` REST calls succeed reliably.

    Parameters
    ----------
    access_token_provider:
        Async callable returning the current Tesla access token
        (Fleet API or owner-api). The client exchanges this for a
        hermes JWT via ``/api/1/users/jwt/hermes``.
    din:
        Full gateway DIN (``{part_number}--{serial_number}``).
    """

    def __init__(
        self,
        access_token_provider: AccessTokenProvider,
        din: str,
    ) -> None:
        self._access_token_provider = access_token_provider
        self._din = din

        self._state = SignalingState.DISCONNECTED
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

        # Cached hermes JWT
        self._hermes_jwt: str | None = None
        self._hermes_jwt_obtained_at: float = 0
        self._hermes_jwt_is_fallback = False  # True when using raw token, not a real JWT
        self._working_jwt_url: str | None = None
        self._auth_denied = False
        self._unavailable_reason: str | None = None
        # Stop retrying after repeated exchange+fallback failures.
        self._fallback_rejection_count = 0
        self._missing_scope_rejection = False

        # Metrics
        self._connected_since: float | None = None
        self._consecutive_failures = 0
        self._last_error: str | None = None
        self._total_connects = 0
        self._last_pong_ts: float | None = None
        self._messages_received = 0
        self._acks_sent = 0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def state(self) -> SignalingState:
        return self._state

    @property
    def is_connected(self) -> bool:
        return self._state == SignalingState.CONNECTED and self._ws is not None

    @property
    def uptime_seconds(self) -> float | None:
        if self._connected_since is None:
            return None
        return time.monotonic() - self._connected_since

    def health_status(self) -> dict[str, Any]:
        return {
            "state": self._state.value,
            "connected_since": self._connected_since,
            "uptime_s": self.uptime_seconds,
            "total_connects": self._total_connects,
            "consecutive_failures": self._consecutive_failures,
            "last_error": self._last_error,
            "unavailable_reason": self._unavailable_reason,
            "last_pong_ts": self._last_pong_ts,
            "messages_received": self._messages_received,
            "acks_sent": self._acks_sent,
        }

    def _mark_unavailable(self, reason: str) -> None:
        """Stop signaling attempts after a known permanent Tesla rejection."""
        self._auth_denied = True
        self._unavailable_reason = reason
        self._last_error = reason
        self._state = SignalingState.UNAVAILABLE
        self._stop_event.set()

    async def start(self) -> None:
        """Start the persistent connection loop as a background task."""
        if self._auth_denied:
            _LOGGER.info(
                "signaling: unavailable — %s",
                self._unavailable_reason or "previously got auth denied",
            )
            return
        if self._task is not None and not self._task.done():
            _LOGGER.debug("signaling: already running")
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run_loop(), name="tesla_signaling"
        )
        _LOGGER.info("signaling: background task started (din=%s)", self._din)

    async def stop(self) -> None:
        """Gracefully shut down the WebSocket and background task."""
        self._stop_event.set()
        await self._close_ws()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._state = SignalingState.DISCONNECTED
        _LOGGER.info("signaling: stopped")

    # ------------------------------------------------------------------
    # Hermes JWT exchange
    # ------------------------------------------------------------------

    async def _get_hermes_jwt(self) -> str | None:
        """Exchange the access token for a hermes-scoped JWT.

        Tries multiple endpoints (owner-api then Fleet API) since the
        token type determines which endpoint accepts it. Caches the JWT
        and the working endpoint URL for subsequent refreshes.

        Returns None if all endpoints reject the token.
        """
        # Use cached JWT if it's fresh enough
        age = time.monotonic() - self._hermes_jwt_obtained_at
        if self._hermes_jwt and age < (300 - JWT_REFRESH_MARGIN_S):
            return self._hermes_jwt

        access_token = await self._access_token_provider()
        if not access_token:
            _LOGGER.warning("signaling: access token provider returned None")
            return None
        self._missing_scope_rejection = False

        # Also try using the access token directly as X-Jwt — some
        # community reports suggest this may work without the exchange.
        # We'll try the exchange first, then fall back to raw token.
        urls_to_try = list(HERMES_JWT_URLS)
        # If we previously found a working URL, try it first
        if self._working_jwt_url:
            urls_to_try.remove(self._working_jwt_url)
            urls_to_try.insert(0, self._working_jwt_url)

        connection_id = str(uuid.uuid4())
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        payload = {"uuid": connection_id}

        for url in urls_to_try:
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.post(
                        url,
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status in (401, 403):
                            body = await resp.text()
                            if _is_missing_scope_response(resp.status, body):
                                self._missing_scope_rejection = True
                                scopes = _decode_jwt_scopes(access_token)
                                _LOGGER.warning(
                                    "signaling: hermes JWT exchange at %s "
                                    "rejected the access token for missing scopes. "
                                    "Token scopes=%s Response: %s",
                                    url,
                                    scopes if scopes else "?",
                                    body[:300],
                                )
                            else:
                                _LOGGER.info(
                                    "signaling: hermes JWT exchange at %s "
                                    "returned %d — trying next endpoint. "
                                    "Response: %s",
                                    url,
                                    resp.status,
                                    body[:200],
                                )
                            continue
                        if resp.status != 200:
                            body = await resp.text()
                            if _is_hermes_unsupported_response(resp.status, body):
                                reason = (
                                    "Tesla does not support Hermes JWT exchange for "
                                    "this access token; signed_command is required"
                                )
                                self._mark_unavailable(reason)
                                _LOGGER.warning(
                                    "signaling: Tesla rejected Hermes JWT exchange "
                                    "with a permanent signed_command requirement. "
                                    "Stopping endpoint retries and raw-token fallback."
                                )
                                return None
                            # The PowerSync.cc proxy returns structured JSON with
                            # `error`, `detail`, and `token_scopes` from the
                            # upstream Tesla response. Log those explicitly so
                            # we don't lose `token_scopes` to the 200-char clip.
                            err_code = ""
                            detail = ""
                            scopes: list = []
                            try:
                                parsed = json.loads(body)
                                if isinstance(parsed, dict):
                                    err_code = str(parsed.get("error", ""))
                                    detail = str(parsed.get("detail", ""))
                                    raw_scopes = parsed.get("token_scopes")
                                    if isinstance(raw_scopes, list):
                                        scopes = raw_scopes
                            except (ValueError, TypeError):
                                pass
                            _LOGGER.warning(
                                "signaling: hermes JWT exchange at %s "
                                "failed (%d) error=%s scopes=%s detail=%s",
                                url,
                                resp.status,
                                err_code or "?",
                                scopes if scopes else "?",
                                (detail or body)[:400],
                            )
                            continue

                        data = await resp.json()
                        jwt = data.get("token")
                        if not jwt:
                            _LOGGER.warning(
                                "signaling: hermes JWT response missing "
                                "'token' from %s: %s",
                                url,
                                str(data)[:200],
                            )
                            continue

                        self._hermes_jwt = jwt
                        self._hermes_jwt_obtained_at = time.monotonic()
                        self._hermes_jwt_is_fallback = False
                        self._working_jwt_url = url
                        self._fallback_rejection_count = 0
                        _LOGGER.info(
                            "signaling: hermes JWT obtained from %s", url
                        )
                        return jwt

            except Exception as err:
                _LOGGER.warning(
                    "signaling: hermes JWT exchange error at %s: %s", url, err
                )
                continue

        if self._missing_scope_rejection:
            scopes = _decode_jwt_scopes(access_token)
            hint = _missing_scope_hint(scopes)
            reason = (
                "Tesla rejected the access token for Hermes JWT exchange "
                "because it is missing required scopes"
            )
            self._mark_unavailable(reason)
            _LOGGER.warning(
                "signaling: Tesla rejected the access token for Hermes JWT "
                "exchange because it is missing required scopes. Normal Tesla "
                "Fleet API telemetry may still work with this token, but Hermes "
                "signaling cannot use it. Re-authorize the Tesla provider if "
                "its granted scopes are stale; otherwise Hermes signaling is "
                "marked unavailable for this token. Token scopes=%s%s",
                scopes if scopes else "?",
                hint,
            )
            return None

        # All exchange endpoints failed — try using the access token
        # directly as the JWT. This is a last resort; the WebSocket
        # handshake will reject it if it's not valid, and the reconnect
        # loop will handle the failure.
        _LOGGER.warning(
            "signaling: all hermes JWT exchange endpoints failed — "
            "falling back to raw access token as X-Jwt "
            "(token starts with: %s...)",
            access_token[:20] if access_token else "None",
        )
        self._hermes_jwt = access_token
        self._hermes_jwt_obtained_at = time.monotonic()
        self._hermes_jwt_is_fallback = True
        return access_token

    # ------------------------------------------------------------------
    # Connection loop
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        """Main loop: connect → read/ping → reconnect on failure."""
        while not self._stop_event.is_set():
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self._consecutive_failures += 1
                self._last_error = str(err)
                _LOGGER.warning(
                    "signaling: connection failed (%d consecutive): %s",
                    self._consecutive_failures,
                    err,
                )

            await self._close_ws()
            if self._stop_event.is_set() and self._auth_denied:
                return
            self._state = SignalingState.RECONNECTING
            self._connected_since = None

            # Exponential backoff
            delay = min(
                RECONNECT_BASE_S * (2 ** (self._consecutive_failures - 1)),
                RECONNECT_MAX_S,
            )
            _LOGGER.info("signaling: reconnecting in %.0fs", delay)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=delay
                )
                # stop_event was set during the wait — exit
                return
            except asyncio.TimeoutError:
                # Normal: the delay elapsed, continue to reconnect
                pass

    async def _connect_and_listen(self) -> None:
        """Single connection attempt: exchange JWT, connect, listen."""
        self._state = SignalingState.CONNECTING

        jwt = await self._get_hermes_jwt()
        if not jwt:
            if not self._auth_denied:
                self._last_error = "Failed to obtain hermes JWT"
                self._consecutive_failures += 1
            return

        connection_id = str(uuid.uuid4())
        headers = {
            "X-Tesla-App-Key": TESLA_APP_KEY,
            "X-Jwt": jwt,
            "X-Connection-Id": connection_id,
        }

        _LOGGER.debug(
            "signaling: connecting to %s (conn_id=%s)",
            SIGNALING_URL,
            connection_id[:8],
        )

        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(
                SIGNALING_URL,
                headers=headers,
                heartbeat=PING_INTERVAL_S,
                timeout=aiohttp.ClientTimeout(total=15),
            )
        except Exception:
            await self._close_ws()
            raise

        self._state = SignalingState.CONNECTED
        connect_time = time.monotonic()
        self._connected_since = connect_time
        self._consecutive_failures = 0
        self._total_connects += 1
        _LOGGER.info(
            "signaling: connected (total connects: %d)", self._total_connects
        )

        await self._listen()

        # If the connection lasted less than 5s, the server likely
        # rejected us (sends one message then closes). Count this as
        # a failure so backoff kicks in and we don't hammer the server.
        duration = time.monotonic() - connect_time
        if duration < 5:
            self._consecutive_failures += 1
            _LOGGER.warning(
                "signaling: connection lasted only %.1fs — "
                "server may be rejecting us (failure count: %d)",
                duration, self._consecutive_failures,
            )

    async def _listen(self) -> None:
        """Read messages from the WebSocket until it closes.

        The server sends STREAMING_CONFIG on connect and expects a
        CLIENT_ACK. We ACK every message that has a txid.
        """
        assert self._ws is not None
        async for msg in self._ws:
            if self._stop_event.is_set():
                return

            if msg.type == aiohttp.WSMsgType.BINARY:
                self._messages_received += 1
                await self._handle_binary(msg.data)

            elif msg.type == aiohttp.WSMsgType.PONG:
                self._last_pong_ts = time.monotonic()

            elif msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
            ):
                close_code = self._ws.close_code if self._ws else None
                _LOGGER.info(
                    "signaling: server closed connection "
                    "(type=%s, close_code=%s, extra=%s)",
                    msg.type, close_code, repr(msg.data)[:200],
                )
                return

            elif msg.type == aiohttp.WSMsgType.ERROR:
                _LOGGER.warning(
                    "signaling: WebSocket error: %s", self._ws.exception()
                )
                return

    async def _handle_binary(self, data: bytes) -> None:
        """Process an inbound binary protobuf frame.

        Parses the HermesMessage, logs the command type, and sends
        a CLIENT_ACK if the message has a txid.
        """
        fields = _parse_hermes_message(data)
        if fields is None:
            # Maybe the message isn't wrapped in HermesMessage — try
            # parsing the raw bytes as a CommandMessage directly.
            fields = _parse_command_fields(data)

        if not fields:
            _LOGGER.warning(
                "signaling: unparseable binary frame (%d bytes): %s",
                len(data), data[:100].hex(),
            )
            return

        cmd_type = fields.get(5)  # commandType
        txid = fields.get(1)  # txid
        message_id = fields.get(12)  # messageId

        # Decode payload for logging (e.g. "authorization denied")
        payload_bytes = fields.get(10)
        payload_text = ""
        if isinstance(payload_bytes, bytes):
            try:
                payload_text = payload_bytes.decode("utf-8", errors="replace")
            except Exception:
                payload_text = payload_bytes.hex()[:40]

        # Detect authorization denied from the Hermes server.
        # If we fell back to a raw token (JWT exchange failed), the denial is
        # expected — clear the cached token so the next reconnect tries the
        # exchange again. Only treat as permanent when a real JWT was rejected.
        if "authorization denied" in payload_text.lower():
            if self._hermes_jwt_is_fallback:
                self._fallback_rejection_count += 1
                if self._fallback_rejection_count >= 3:
                    reason = (
                        "Hermes JWT exchange failed and raw token fallback was "
                        "rejected"
                    )
                    _LOGGER.warning(
                        "signaling: hermes JWT exchange repeatedly failed and raw "
                        "token fallback also rejected. Normal Tesla Fleet API "
                        "telemetry can still work while Hermes signaling is "
                        "unavailable if Tesla rejects the token for the private "
                        "Hermes channel. Signaling will stop retrying."
                    )
                    self._mark_unavailable(reason)
                    return
                _LOGGER.warning(
                    "signaling: HermesServer rejected raw token fallback — "
                    "the JWT exchange endpoints were unavailable. "
                    "Clearing cached token and will retry."
                )
                self._hermes_jwt = None
                self._hermes_jwt_obtained_at = 0
                self._hermes_jwt_is_fallback = False
            else:
                reason = (
                    "HermesServer returned authorization denied for the access token"
                )
                _LOGGER.warning(
                    "signaling: HermesServer returned 'authorization denied' — "
                    "the access token is not valid for hermes signaling. "
                    "An owner-api token (client_id='ownerapi') may be required. "
                    "Signaling will stop retrying."
                )
                self._mark_unavailable(reason)
            return

        cmd_name = "unknown"
        if cmd_type is not None:
            try:
                cmd_name = CommandType(cmd_type).name
            except ValueError:
                cmd_name = str(cmd_type)

        _LOGGER.info(
            "signaling: received %s (cmd_type=%s, payload=%s)",
            cmd_name, cmd_type, payload_text[:100] if payload_text else "empty",
        )

        # ACK any message with a txid to keep the session healthy
        if isinstance(txid, bytes) and self._ws is not None:
            ack = _build_client_ack(
                txid,
                message_id if isinstance(message_id, bytes) else None,
            )
            try:
                await self._ws.send_bytes(ack)
                self._acks_sent += 1
                _LOGGER.debug("signaling: sent CLIENT_ACK for %s", cmd_name)
            except Exception as err:
                _LOGGER.warning("signaling: failed to send ACK: %s", err)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def _close_ws(self) -> None:
        """Close the WebSocket and session, tolerating already-closed."""
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None
