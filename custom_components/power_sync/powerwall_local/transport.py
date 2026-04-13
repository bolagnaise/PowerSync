"""Async TEDAPI v1r signed transport for Powerwall 3.

Wraps outgoing protobuf messages in a ``RoutableMessage`` with an RSA-PKCS1v15
+ SHA-512 signature over a TLV-encoded payload, then POSTs to the gateway's
``/tedapi/v1r`` endpoint. The private key must have been pre-registered with
Tesla Fleet API (see ``pairing.py``); the matching public key is embedded in
every request as the signer identity.

Ported from jasonacox/pypowerwall's ``tedapi_v1r.py`` (requests -> aiohttp,
file-based key loading -> in-memory PEM). The TLV encoding and signing scheme
are unchanged; the gateway verifies both byte-for-byte.
"""

from __future__ import annotations

import json
import logging
import math
import ssl
import struct
import time
import uuid
from dataclasses import dataclass
from typing import Any

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from . import tedapi_combined_pb2 as combined_pb2
from .exceptions import (
    PowerwallAuthError,
    PowerwallLocalError,
    PowerwallSignatureError,
    PowerwallUnreachableError,
)

_LOGGER = logging.getLogger(__name__)

_SIGNATURE_TYPE_RSA = 7
_DOMAIN_ENERGY_DEVICE = 7
_TAG_END = 0xFF
_SIGNATURE_TTL_SECONDS = 12


def _build_insecure_ssl_context() -> ssl.SSLContext:
    """Build an SSL context that accepts the gateway's self-signed cert.

    The gateway ships with a Tesla-issued self-signed cert tied to its DIN.
    There's no way to pre-trust it without shipping Tesla's CA bundle, so we
    disable verification on this specific connector. All sensitive auth
    happens inside the TLS session via RSA signatures, not cert pinning.

    Blocking — ``ssl.create_default_context()`` calls ``load_default_certs()``
    which reads from disk. Callers must invoke this via
    ``hass.async_add_executor_job`` to avoid blocking the HA event loop.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# Module-level cache — context construction is expensive and the resulting
# object is safe to share across sessions (no per-request state). Built
# lazily on first use, always off the event loop.
_SSL_CONTEXT: ssl.SSLContext | None = None


async def get_insecure_ssl_context(hass: Any | None = None) -> ssl.SSLContext:
    """Return a cached insecure SSL context, building it off-loop on first use.

    When called with a HomeAssistant instance the build runs via
    ``async_add_executor_job`` so it doesn't trip HA's "blocking call in
    the event loop" detector. When called without hass (eg from tests)
    falls back to a sync build.
    """
    global _SSL_CONTEXT
    if _SSL_CONTEXT is not None:
        return _SSL_CONTEXT
    if hass is not None:
        _SSL_CONTEXT = await hass.async_add_executor_job(_build_insecure_ssl_context)
    else:
        _SSL_CONTEXT = _build_insecure_ssl_context()
    return _SSL_CONTEXT


def _insecure_ssl_context() -> ssl.SSLContext:
    """Sync helper retained for non-async call sites (client init).

    Prefer ``get_insecure_ssl_context(hass)`` when possible — this path
    will still trip the blocking-call detector on the event loop. The
    module-level cache means at most one expensive build per HA process.
    """
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        _SSL_CONTEXT = _build_insecure_ssl_context()
    return _SSL_CONTEXT


@dataclass
class TEDAPIResponse:
    """Parsed result from a v1r POST."""

    ok: bool
    inner_bytes: bytes | None
    fault_name: str | None = None
    http_status: int | None = None


class TEDAPIv1rTransport:
    """Async RSA-signed transport to ``/tedapi/v1r``."""

    def __init__(
        self,
        host: str,
        private_key_pem: bytes,
        customer_password: str,
        *,
        timeout: float = 8.0,
    ) -> None:
        self._host = host
        self._customer_password = customer_password
        self._timeout = aiohttp.ClientTimeout(total=timeout)

        try:
            self._private_key: rsa.RSAPrivateKey = serialization.load_pem_private_key(
                private_key_pem, password=None
            )
        except Exception as err:
            raise PowerwallLocalError(f"Invalid RSA private key: {err}") from err

        self._public_key_der = self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.PKCS1,
        )
        self._ssl = _insecure_ssl_context()
        self._token: str | None = None
        self._din: str | None = None

    @property
    def din(self) -> str | None:
        return self._din

    async def _session(self) -> aiohttp.ClientSession:
        # Fresh session per call keeps connection pooling scoped to the
        # caller; TEDAPI polling is low rate and the handshake is cheap.
        connector = aiohttp.TCPConnector(ssl=self._ssl, limit=4)
        return aiohttp.ClientSession(connector=connector, timeout=self._timeout)

    async def login(self) -> bool:
        """Log in via ``/api/login/Basic`` to get a Bearer token for REST calls."""
        url = f"https://{self._host}/api/login/Basic"
        payload = {
            "username": "customer",
            "password": self._customer_password,
            "email": "customer@customer.domain",
            "clientInfo": {"timezone": "UTC"},
        }
        try:
            async with await self._session() as sess:
                async with sess.post(url, json=payload) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        _LOGGER.warning(
                            "v1r login failed (%s): %s", resp.status, body[:200]
                        )
                        if resp.status in (401, 403):
                            raise PowerwallAuthError(
                                f"Gateway rejected customer password ({resp.status})"
                            )
                        return False
                    data = await resp.json()
                    self._token = data.get("token")
                    return self._token is not None
        except aiohttp.ClientError as err:
            raise PowerwallUnreachableError(
                f"Cannot reach gateway {self._host}: {err}"
            ) from err

    async def fetch_din(self) -> str | None:
        """Fetch the gateway DIN from ``/tedapi/din`` (Bearer-authed)."""
        if not self._token and not await self.login():
            return None
        url = f"https://{self._host}/tedapi/din"
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            async with await self._session() as sess:
                async with sess.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        return None
                    self._din = (await resp.text()).strip()
                    return self._din
        except aiohttp.ClientError as err:
            raise PowerwallUnreachableError(str(err)) from err

    @staticmethod
    def _tlv(tag: int, value: bytes) -> bytes:
        return bytes([tag, len(value)]) + value

    def _build_tlv_payload(
        self, din: str, expires_at: int, inner_bytes: bytes
    ) -> bytes:
        return b"".join(
            [
                self._tlv(0, bytes([_SIGNATURE_TYPE_RSA])),  # TAG_SIGNATURE_TYPE
                self._tlv(1, bytes([_DOMAIN_ENERGY_DEVICE])),  # TAG_DOMAIN
                self._tlv(2, din.encode()),  # TAG_PERSONALIZATION
                self._tlv(4, struct.pack(">I", expires_at)),  # TAG_EXPIRES_AT
                bytes([_TAG_END]),
                inner_bytes,
            ]
        )

    def _sign(self, payload: bytes) -> bytes:
        return self._private_key.sign(
            data=payload,
            padding=padding.PKCS1v15(),
            algorithm=hashes.SHA512(),
        )

    async def post_v1r(self, envelope_bytes: bytes, din: str) -> TEDAPIResponse:
        """Wrap an envelope in a signed ``RoutableMessage`` and POST it."""
        routable = combined_pb2.RoutableMessage()
        routable.to_destination.domain = combined_pb2.DOMAIN_ENERGY_DEVICE
        routable.protobuf_message_as_bytes = envelope_bytes
        routable.uuid = str(uuid.uuid4()).encode()

        expires_at = math.ceil(time.time()) + _SIGNATURE_TTL_SECONDS
        tlv = self._build_tlv_payload(
            din, expires_at, routable.protobuf_message_as_bytes
        )
        signature = self._sign(tlv)

        routable.signature_data.signer_identity.public_key = self._public_key_der
        routable.signature_data.rsa_data.expires_at = expires_at
        routable.signature_data.rsa_data.signature = signature

        url = f"https://{self._host}/tedapi/v1r"
        payload = routable.SerializeToString()
        headers = {"Content-Type": "application/octet-stream"}

        _LOGGER.info(
            "v1r POST to %s with DIN=%s, envelope=%d bytes",
            url, din, len(payload),
        )
        try:
            async with await self._session() as sess:
                async with sess.post(url, data=payload, headers=headers) as resp:
                    http_status = resp.status
                    if http_status != 200:
                        body_text = await resp.text()
                        _LOGGER.warning(
                            "v1r POST non-200: %s — %s", http_status, body_text[:300]
                        )
                        return TEDAPIResponse(False, None, http_status=http_status)
                    raw = await resp.read()
        except aiohttp.ClientError as err:
            raise PowerwallUnreachableError(str(err)) from err

        resp_msg = combined_pb2.RoutableMessage()
        try:
            resp_msg.ParseFromString(raw)
        except Exception as err:
            raise PowerwallLocalError(f"Malformed v1r response: {err}") from err

        fault = resp_msg.signed_message_status.message_fault
        if fault != combined_pb2.MESSAGEFAULT_ERROR_NONE:
            fault_name = combined_pb2.MessageFault_E.Name(fault)
            _LOGGER.warning("v1r response fault: %s (code %s)", fault_name, fault)
            if fault == combined_pb2.MESSAGEFAULT_ERROR_UNKNOWN_KEY_ID:
                raise PowerwallSignatureError(
                    "Gateway does not recognise our RSA key — re-pairing required"
                )
            return TEDAPIResponse(False, None, fault_name=fault_name)

        inner = resp_msg.protobuf_message_as_bytes
        return TEDAPIResponse(True, inner if inner else None)

    def build_signed_bytes(self, envelope_bytes: bytes, din: str) -> bytes:
        """Build a signed ``RoutableMessage`` and return the raw bytes.

        Same signing as ``post_v1r`` but returns the serialized protobuf
        instead of posting locally. The caller can base64-encode these
        bytes and send them through the cloud ``device_command`` endpoint
        as ``energy_device_message``.
        """
        routable = combined_pb2.RoutableMessage()
        routable.to_destination.domain = combined_pb2.DOMAIN_ENERGY_DEVICE
        routable.protobuf_message_as_bytes = envelope_bytes
        routable.uuid = str(uuid.uuid4()).encode()

        expires_at = math.ceil(time.time()) + _SIGNATURE_TTL_SECONDS
        tlv = self._build_tlv_payload(
            din, expires_at, routable.protobuf_message_as_bytes
        )
        signature = self._sign(tlv)

        routable.signature_data.signer_identity.public_key = self._public_key_der
        routable.signature_data.rsa_data.expires_at = expires_at
        routable.signature_data.rsa_data.signature = signature

        return routable.SerializeToString()

    def build_signed_raw_command(self, din: str, raw_bytes: bytes) -> bytes:
        """Wrap arbitrary raw bytes in a signed RoutableMessage.

        Used when the command protobuf format is different from the
        TEGMessages schema (e.g. the captured device_command protobufs
        from the Tesla app).
        """
        return self.build_signed_bytes(raw_bytes, din)

    def build_signed_trigger_islanding(self, din: str) -> bytes:
        """Build a signed ``triggerIslandingBlackStartRequest`` for cloud relay.

        This is the actual contactor-open command. ``setIslandModeRequest``
        sets the mode preference; this command physically opens the grid
        contactor.
        """
        from . import tesla_local_pb2 as tp

        env = tp.MessageEnvelope()
        env.deliveryChannel = 2  # HERMES_COMMAND
        env.sender.authorizedClient = 1  # CUSTOMER_MOBILE_APP
        env.recipient.din = din
        env.teg.triggerIslandingBlackStartRequest.SetInParent()

        return self.build_signed_bytes(env.SerializeToString(), din)

    def build_signed_island_mode(
        self, din: str, *, off_grid: bool, mode_override: int | None = None,
    ) -> bytes:
        """Build a signed island-mode ``RoutableMessage`` for cloud relay.

        Returns base64-ready bytes that can be sent as
        ``routable_message`` in a ``device_command`` call.

        PW3: mode=6 off-grid, mode=1 reconnect (discovered via mitmproxy).
        PW2: mode=2 off-grid, mode=1 reconnect (confirmed via MITM capture).
        Use mode_override to specify explicitly.
        """
        from . import tesla_local_pb2 as tp

        mode = mode_override if mode_override is not None else (6 if off_grid else 1)
        env = tp.MessageEnvelope()
        env.deliveryChannel = 1 if off_grid else 2  # LOCAL_HTTPS for off-grid
        env.sender.authorizedClient = 1  # CUSTOMER_MOBILE_APP
        env.recipient.din = din
        env.teg.setIslandModeRequest.mode = mode
        env.teg.setIslandModeRequest.force = off_grid  # force=True for off-grid

        return self.build_signed_bytes(env.SerializeToString(), din)

    async def read_config(self, din: str) -> dict[str, Any] | None:
        """Read ``config.json`` from the gateway via FileStore readFileRequest."""
        msg = combined_pb2.Message()
        envelope = msg.message
        envelope.deliveryChannel = combined_pb2.DELIVERY_CHANNEL_HERMES_COMMAND
        envelope.sender.authorizedClient = 1
        envelope.recipient.din = din
        req = envelope.filestore.readFileRequest
        req.domain = combined_pb2.FILE_STORE_API_DOMAIN_CONFIG_JSON
        req.name = "config.json"

        resp = await self.post_v1r(envelope.SerializeToString(), din)
        if not resp.ok or not resp.inner_bytes:
            return None
        try:
            env = combined_pb2.MessageEnvelope()
            env.ParseFromString(resp.inner_bytes)
            if env.HasField("filestore"):
                blob = env.filestore.readFileResponse.file.blob
                return json.loads(blob.decode("utf-8"))
        except Exception as err:
            _LOGGER.debug("read_config parse error: %s", err)
        return None

    async def write_config(self, din: str, updates: dict[str, Any]) -> bool:
        """Read-modify-write ``config.json`` via FileStore updateFileRequest.

        ``updates`` keys use dotted paths, eg ``site_info.default_real_mode``.
        Failed optimistic-lock writes return False; caller may retry.
        """
        read_msg = combined_pb2.Message()
        read_env = read_msg.message
        read_env.deliveryChannel = combined_pb2.DELIVERY_CHANNEL_HERMES_COMMAND
        read_env.sender.authorizedClient = 1
        read_env.recipient.din = din
        r = read_env.filestore.readFileRequest
        r.domain = combined_pb2.FILE_STORE_API_DOMAIN_CONFIG_JSON
        r.name = "config.json"

        read_resp = await self.post_v1r(read_env.SerializeToString(), din)
        if not read_resp.ok or not read_resp.inner_bytes:
            return False

        try:
            env = combined_pb2.MessageEnvelope()
            env.ParseFromString(read_resp.inner_bytes)
            if not env.HasField("filestore"):
                return False
            blob = env.filestore.readFileResponse.file.blob
            config_hash = env.filestore.readFileResponse.hash
            config = json.loads(blob.decode("utf-8"))
        except Exception as err:
            _LOGGER.error("write_config read-phase parse error: %s", err)
            return False

        for dotted, value in updates.items():
            keys = dotted.split(".")
            node = config
            for k in keys[:-1]:
                if k not in node or not isinstance(node[k], dict):
                    node[k] = {}
                node = node[k]
            node[keys[-1]] = value

        write_msg = combined_pb2.Message()
        w_env = write_msg.message
        w_env.deliveryChannel = combined_pb2.DELIVERY_CHANNEL_HERMES_COMMAND
        w_env.sender.authorizedClient = 1
        w_env.recipient.din = din
        update_req = w_env.filestore.updateFileRequest
        update_req.domain = combined_pb2.FILE_STORE_API_DOMAIN_CONFIG_JSON
        update_req.file.name = "config.json"
        update_req.file.blob = json.dumps(config).encode("utf-8")
        update_req.hash = config_hash

        write_resp = await self.post_v1r(w_env.SerializeToString(), din)
        if not write_resp.ok or not write_resp.inner_bytes:
            return False
        try:
            wenv = combined_pb2.MessageEnvelope()
            wenv.ParseFromString(write_resp.inner_bytes)
            return wenv.HasField("filestore")
        except Exception:
            return False

    async def set_island_mode(self, din: str, *, off_grid: bool) -> bool:
        """Send ``TEGAPISetIslandModeRequest`` to the gateway.

        This is the real islanding command — it physically opens or closes
        the grid contactor. Uses the ``teslapower`` proto schema (from
        pypowerwall's ``tesla_pb2.py``) because it defines
        ``setIslandModeRequest`` at field 3 in ``TEGMessages``, which the
        ``tedapi_combined`` proto didn't include. The wire format is
        identical: same field numbers, same message layout. The gateway
        processes it the same way regardless of which proto package
        generated the bytes.

        Args:
            din: Full gateway DIN (part--serial)
            off_grid: True to island, False to reconnect
        """
        from . import tesla_local_pb2 as tp

        mode = 2 if off_grid else 1
        env = tp.MessageEnvelope()
        env.deliveryChannel = 1  # LOCAL_HTTPS (try instead of HERMES_COMMAND)
        env.sender.local = 2  # LOCAL_PARTICIPANT_CUSTOMER
        env.recipient.din = din
        env.teg.setIslandModeRequest.mode = mode
        env.teg.setIslandModeRequest.force = True

        _LOGGER.info(
            "set_island_mode: mode=%s (%s) din=%s",
            mode, "off_grid" if off_grid else "on_grid", din,
        )
        resp = await self.post_v1r(env.SerializeToString(), din)
        if not resp.ok or not resp.inner_bytes:
            _LOGGER.warning(
                "set_island_mode failed: ok=%s fault=%s http=%s",
                resp.ok, resp.fault_name, resp.http_status,
            )
            return False
        try:
            reply = tp.MessageEnvelope()
            reply.ParseFromString(resp.inner_bytes)
            _LOGGER.info(
                "set_island_mode: response envelope: deliveryChannel=%s "
                "has_teg=%s has_common=%s payload_case=%s raw_hex=%s",
                reply.deliveryChannel,
                reply.HasField("teg") if True else "?",
                reply.HasField("common") if True else "?",
                reply.WhichOneof("payload"),
                resp.inner_bytes[:200].hex(),
            )
            if reply.HasField("teg"):
                teg_field = reply.teg.WhichOneof("message")
                _LOGGER.info("set_island_mode: TEG oneof field = %s", teg_field)
                if teg_field == "setIslandModeResponse":
                    result = reply.teg.setIslandModeResponse.result
                    _LOGGER.info("set_island_mode response: result=%s", result)
                    # Tesla uses result=1 for success (protobuf default 0
                    # means "unset"). Accept any non-negative result as OK.
                    return result >= 0
            _LOGGER.warning(
                "set_island_mode: unexpected response payload (no setIslandModeResponse)"
            )
        except Exception as err:
            _LOGGER.warning("set_island_mode: response parse error: %s", err)
        return False

    async def trigger_islanding(self, din: str) -> bool:
        """Send ``triggerIslandingBlackStartRequest`` — the actual contactor-open command.

        ``setIslandModeRequest`` only sets the *desired* island mode preference
        but doesn't physically open the grid contactor. This command is what
        the Tesla app sends when the user taps "Go Off-Grid" — it triggers
        the full islanding transition including grid-frequency ramp-down,
        contactor open, and inverter restart in island mode.
        """
        from . import tesla_local_pb2 as tp

        env = tp.MessageEnvelope()
        env.deliveryChannel = 2  # HERMES_COMMAND
        env.sender.authorizedClient = 1
        env.recipient.din = din
        env.teg.triggerIslandingBlackStartRequest.SetInParent()

        _LOGGER.info("trigger_islanding: din=%s", din)
        resp = await self.post_v1r(env.SerializeToString(), din)
        if not resp.ok or not resp.inner_bytes:
            _LOGGER.warning(
                "trigger_islanding failed: ok=%s fault=%s http=%s",
                resp.ok, resp.fault_name, resp.http_status,
            )
            return False
        try:
            reply = tp.MessageEnvelope()
            reply.ParseFromString(resp.inner_bytes)
            _LOGGER.info(
                "trigger_islanding: response payload_case=%s raw_hex=%s",
                reply.WhichOneof("payload"),
                resp.inner_bytes[:200].hex(),
            )
            if reply.HasField("teg"):
                teg_field = reply.teg.WhichOneof("message")
                _LOGGER.info("trigger_islanding: TEG field = %s", teg_field)
                if teg_field == "triggerIslandingBlackStartResponse":
                    return True
            if reply.HasField("common"):
                _LOGGER.warning(
                    "trigger_islanding: common response (may be error): %s",
                    resp.inner_bytes[:200].hex(),
                )
        except Exception as err:
            _LOGGER.warning("trigger_islanding: response parse error: %s", err)
        return False

    async def schedule_manual_backup(self, din: str, duration_s: int) -> bool:
        """Trigger Tesla's "Storm Watch Manual Backup" — holds SOC, stops export.

        This is the closest TEDAPI-native equivalent to "go off-grid" currently
        in the reverse-engineered protobuf. It does not physically open the
        grid contactor on all firmwares, but it stops grid import/export and
        reserves the battery for backup use.
        """
        env = combined_pb2.MessageEnvelope()
        env.deliveryChannel = combined_pb2.DELIVERY_CHANNEL_HERMES_COMMAND
        env.sender.authorizedClient = 1
        env.recipient.din = din
        teg_req = env.teg.schedule_manual_backup_event_request
        teg_req.scheduling_info.duration_seconds = max(60, min(duration_s, 86400))

        resp = await self.post_v1r(env.SerializeToString(), din)
        if not resp.ok or not resp.inner_bytes:
            return False
        try:
            reply = combined_pb2.MessageEnvelope()
            reply.ParseFromString(resp.inner_bytes)
            return reply.HasField("teg") and reply.teg.HasField(
                "schedule_manual_backup_event_response"
            )
        except Exception:
            return False

    async def cancel_manual_backup(self, din: str) -> bool:
        """Cancel an active manual backup event."""
        env = combined_pb2.MessageEnvelope()
        env.deliveryChannel = combined_pb2.DELIVERY_CHANNEL_HERMES_COMMAND
        env.sender.authorizedClient = 1
        env.recipient.din = din
        env.teg.cancel_manual_backup_event_request.SetInParent()

        resp = await self.post_v1r(env.SerializeToString(), din)
        if not resp.ok or not resp.inner_bytes:
            return False
        try:
            reply = combined_pb2.MessageEnvelope()
            reply.ParseFromString(resp.inner_bytes)
            return reply.HasField("teg") and reply.teg.HasField(
                "cancel_manual_backup_event_response"
            )
        except Exception:
            return False

    async def api_get(self, path: str) -> Any | None:
        """Authenticated REST GET against the gateway (Bearer token).

        Works on PW2 and PW3 for standard endpoints: ``/api/meters/aggregates``,
        ``/api/system_status/soe``, ``/api/system_status/grid_status``, etc.
        """
        if not self._token and not await self.login():
            return None
        url = f"https://{self._host}{path}"
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            async with await self._session() as sess:
                async with sess.get(url, headers=headers) as resp:
                    if resp.status in (401, 403):
                        # Token lapsed; re-login once and retry.
                        if await self.login():
                            headers["Authorization"] = f"Bearer {self._token}"
                            async with sess.get(url, headers=headers) as r2:
                                if r2.status != 200:
                                    return None
                                return await r2.json()
                        return None
                    if resp.status != 200:
                        return None
                    return await resp.json()
        except aiohttp.ClientError as err:
            raise PowerwallUnreachableError(str(err)) from err

    async def api_post(self, path: str, body: dict[str, Any]) -> Any | None:
        """Authenticated REST POST against the gateway."""
        if not self._token and not await self.login():
            return None
        url = f"https://{self._host}{path}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        try:
            async with await self._session() as sess:
                async with sess.post(url, json=body, headers=headers) as resp:
                    if resp.status in (401, 403):
                        if await self.login():
                            headers["Authorization"] = f"Bearer {self._token}"
                            async with sess.post(
                                url, json=body, headers=headers
                            ) as r2:
                                if r2.status not in (200, 201, 204):
                                    return None
                                if r2.status == 204:
                                    return {}
                                return await r2.json()
                        return None
                    if resp.status not in (200, 201, 204):
                        return None
                    if resp.status == 204:
                        return {}
                    return await resp.json()
        except aiohttp.ClientError as err:
            raise PowerwallUnreachableError(str(err)) from err
