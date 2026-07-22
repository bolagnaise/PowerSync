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

import asyncio
import base64
import ipaddress
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
    PowerwallLocalError,
    PowerwallSignatureError,
    PowerwallUnreachableError,
)
from ..powerwall_host import normalize_powerwall_gateway_host

_LOGGER = logging.getLogger(__name__)

_SIGNATURE_TYPE_RSA = 7
_DOMAIN_ENERGY_DEVICE = 7
_TAG_END = 0xFF
_SIGNATURE_TTL_SECONDS = 12


def _enum_suffix(enum_type: Any, value: int, prefix: str) -> str:
    """Return a stable short enum name for local API payloads."""
    try:
        name = enum_type.Name(value)
    except ValueError:
        return str(value)
    return name.removeprefix(prefix)


def _ipv4(value: int) -> str | None:
    """Render Tesla's fixed32 IPv4 value without leaking raw binary data."""
    if not value:
        return None
    try:
        return str(ipaddress.IPv4Address(value))
    except ipaddress.AddressValueError:
        return None


def _network_interface_payload(interface: Any) -> dict[str, Any]:
    """Return the useful, credential-free subset of a network interface."""
    connectivity = interface.connectivity_status
    rssi = connectivity.rssi
    ipv4 = interface.ipv4_config
    return {
        "enabled": bool(interface.enabled),
        "active_route": bool(interface.active_route),
        "ipv4": {
            "dhcp_enabled": bool(ipv4.dhcp_enabled),
            "address": _ipv4(ipv4.address),
            "subnet_mask": _ipv4(ipv4.subnet_mask),
            "gateway": _ipv4(ipv4.gateway),
            "dns": [address for value in ipv4.dns if (address := _ipv4(value))],
        },
        "connectivity": {
            "physical": bool(connectivity.connected_physical),
            "internet": bool(connectivity.connected_internet),
            "tesla": bool(connectivity.connected_tesla),
            "rssi_dbm": rssi.value if connectivity.HasField("rssi") else None,
            "signal_strength_percent": (
                rssi.signal_strength_percent.value
                if connectivity.HasField("rssi")
                and rssi.HasField("signal_strength_percent")
                else None
            ),
            "snr_db": (
                connectivity.snr.value if connectivity.HasField("snr") else None
            ),
        },
    }


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
        *,
        din: str | None = None,
        timeout: float = 8.0,
    ) -> None:
        self._host = normalize_powerwall_gateway_host(host)
        # Bound the socket connect explicitly (not just the request total): a
        # connect to an unreachable gateway otherwise runs to the OS TCP timeout
        # (~100s) rather than the intended budget.
        self._timeout = aiohttp.ClientTimeout(
            total=timeout,
            connect=min(5.0, timeout),
            sock_connect=min(5.0, timeout),
        )

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
        # DIN is supplied by the caller from cloud pairing — no Bearer-authed
        # /tedapi/din fetch path remains.
        self._din: str | None = din

    @property
    def din(self) -> str | None:
        return self._din

    async def _session(self) -> aiohttp.ClientSession:
        # Fresh session per call keeps connection pooling scoped to the
        # caller; TEDAPI polling is low rate and the handshake is cheap.
        connector = aiohttp.TCPConnector(ssl=self._ssl, limit=4)
        return aiohttp.ClientSession(connector=connector, timeout=self._timeout)

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

        _LOGGER.debug(
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
        except asyncio.TimeoutError as err:
            raise PowerwallUnreachableError(
                f"Timed out connecting to Powerwall gateway at {self._host}"
            ) from err
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

    def build_signed_bytes(
        self, envelope_bytes: bytes, din: str, *, ttl_seconds: int | None = None
    ) -> bytes:
        """Build a signed ``RoutableMessage`` and return the raw bytes.

        Same signing as ``post_v1r`` but returns the serialized protobuf
        instead of posting locally. The caller can base64-encode these
        bytes and send them through the cloud ``device_command`` endpoint
        as ``energy_device_message``.

        ``ttl_seconds`` overrides the default 12 s TTL. Use 300 for cloud
        relay calls where gateway round-trip latency is higher.
        """
        routable = combined_pb2.RoutableMessage()
        routable.to_destination.domain = combined_pb2.DOMAIN_ENERGY_DEVICE
        routable.protobuf_message_as_bytes = envelope_bytes
        routable.uuid = str(uuid.uuid4()).encode()

        expires_at = math.ceil(time.time()) + (ttl_seconds if ttl_seconds is not None else _SIGNATURE_TTL_SECONDS)
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

        Both PW2 and PW3: mode=6 off-grid, mode=1 reconnect.
        force=True is required for off-grid (without it the gateway
        acknowledges but does not physically open the contactor).
        Use mode_override to specify explicitly.
        """
        from . import tesla_local_pb2 as tp

        mode = mode_override if mode_override is not None else (6 if off_grid else 1)
        env = tp.MessageEnvelope()
        env.deliveryChannel = 2  # HERMES_COMMAND for cloud relay
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

    async def set_island_mode(
        self,
        din: str,
        *,
        off_grid: bool,
        force: bool | None = None,
        mode_override: int | None = None,
    ) -> bool:
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

        mode = mode_override if mode_override is not None else (6 if off_grid else 1)
        if force is None:
            force = off_grid
        env = tp.MessageEnvelope()
        env.deliveryChannel = 2  # HERMES_COMMAND, matching the Tesla app
        env.sender.authorizedClient = 1  # CUSTOMER_MOBILE_APP
        env.recipient.din = din
        env.teg.setIslandModeRequest.mode = mode
        env.teg.setIslandModeRequest.force = bool(force)

        _LOGGER.info(
            "set_island_mode: mode=%s force=%s (%s) din=%s",
            mode, force, "off_grid" if off_grid else "on_grid", din,
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
        if duration_s < 60:
            raise ValueError("duration_s must be at least 60")

        # Tesla requires a previous event (including an expired one) to be
        # cancelled before it accepts a replacement. A missing cancel response
        # is harmless; continue and let the schedule response decide success.
        await self.cancel_manual_backup(din)

        env = combined_pb2.MessageEnvelope()
        env.deliveryChannel = combined_pb2.DELIVERY_CHANNEL_HERMES_COMMAND
        env.sender.authorizedClient = 1
        env.recipient.din = din
        teg_req = env.teg.schedule_manual_backup_event_request
        teg_req.scheduling_info.start_time.seconds = int(time.time())
        teg_req.scheduling_info.duration_seconds = min(duration_s, 86400)
        teg_req.scheduling_info.priority = (1 << 64) - 1

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

    async def get_backup_events(self, din: str) -> dict[str, Any] | None:
        """Return the active manual backup and scheduled backup events."""
        env = combined_pb2.MessageEnvelope()
        env.deliveryChannel = combined_pb2.DELIVERY_CHANNEL_HERMES_COMMAND
        env.sender.authorizedClient = 1
        env.recipient.din = din
        env.teg.get_backup_events_request.SetInParent()

        resp = await self.post_v1r(env.SerializeToString(), din)
        if not resp.ok or not resp.inner_bytes:
            return None
        try:
            reply = combined_pb2.MessageEnvelope()
            reply.ParseFromString(resp.inner_bytes)
            if not reply.HasField("teg") or not reply.teg.HasField(
                "get_backup_events_response"
            ):
                return None
            events = reply.teg.get_backup_events_response
            manual: dict[str, Any] | None = None
            if events.HasField("manual_backup_event"):
                scheduling = events.manual_backup_event.scheduling_info
                end_time = scheduling.start_time.seconds + scheduling.duration_seconds
                manual = {
                    "start_time": scheduling.start_time.seconds,
                    "duration_seconds": scheduling.duration_seconds,
                    "end_time": end_time,
                    "active": int(time.time()) < end_time,
                    "priority": scheduling.priority,
                }
            scheduled = [
                {
                    "id": event.id,
                    "name": event.name,
                    "start_time": event.scheduling_info.start_time.seconds,
                    "duration_seconds": event.scheduling_info.duration_seconds,
                    "priority": event.scheduling_info.priority,
                }
                for event in events.backup_events
            ]
            return {"manual_backup": manual, "backup_events": scheduled}
        except Exception as err:
            _LOGGER.debug("get_backup_events parse error: %s", err)
            return None

    async def _read_common(
        self,
        din: str,
        request_field: str,
        response_field: str,
    ) -> Any | None:
        """Send a read-only Common API request and return its typed response."""
        env = combined_pb2.MessageEnvelope()
        env.deliveryChannel = combined_pb2.DELIVERY_CHANNEL_HERMES_COMMAND
        env.sender.authorizedClient = 1
        env.recipient.din = din
        getattr(env.common, request_field).SetInParent()

        resp = await self.post_v1r(env.SerializeToString(), din)
        if not resp.ok or not resp.inner_bytes:
            return None
        try:
            reply = combined_pb2.MessageEnvelope()
            reply.ParseFromString(resp.inner_bytes)
            if not reply.HasField("common") or not reply.common.HasField(
                response_field
            ):
                return None
            return getattr(reply.common, response_field)
        except Exception as err:
            _LOGGER.debug("%s parse error: %s", response_field, err)
            return None

    async def get_system_info(self, din: str) -> dict[str, Any] | None:
        """Read gateway identity, model class, and firmware from Common API."""
        response = await self._read_common(
            din,
            "get_system_info_request",
            "get_system_info_response",
        )
        if response is None:
            return None
        return {
            "part_number": response.device_id.part_number or None,
            "serial_number": response.device_id.serial_number or None,
            "din": response.din.value or None,
            "firmware_version": response.firmare_version.version or None,
            "firmware_githash": (
                response.firmare_version.githash.hex()
                if response.firmare_version.githash
                else None
            ),
            "device_type": _enum_suffix(
                combined_pb2.DeviceType,
                response.device_type,
                "DEVICE_TYPE_",
            ),
        }

    async def get_networking_status(self, din: str) -> dict[str, Any] | None:
        """Read credential-free interface state from Common API field 22/23."""
        response = await self._read_common(
            din,
            "get_networking_status_request",
            "get_networking_status_response",
        )
        if response is None:
            return None
        return {
            name: _network_interface_payload(getattr(response, name))
            for name in ("wifi", "eth", "gsm")
            if response.HasField(name)
        }

    async def check_internet(self, din: str) -> dict[str, Any] | None:
        """Read live internet/Tesla reachability for each gateway interface."""
        response = await self._read_common(
            din,
            "check_internet_request",
            "check_internet_response",
        )
        if response is None:
            return None
        return {
            name: _network_interface_payload(getattr(response, name))
            for name in ("wifi", "eth", "gsm")
            if response.HasField(name)
        }

    async def list_authorized_clients(self, din: str) -> dict[str, Any] | None:
        """Read paired authorized clients directly from the gateway over LAN."""
        env = combined_pb2.MessageEnvelope()
        env.deliveryChannel = combined_pb2.DELIVERY_CHANNEL_HERMES_COMMAND
        env.sender.authorizedClient = 1
        env.recipient.din = din
        env.authorization.list_authorized_clients_request.SetInParent()

        resp = await self.post_v1r(env.SerializeToString(), din)
        if not resp.ok or not resp.inner_bytes:
            return None
        try:
            reply = combined_pb2.MessageEnvelope()
            reply.ParseFromString(resp.inner_bytes)
            if not reply.HasField("authorization") or not reply.authorization.HasField(
                "list_authorized_clients_response"
            ):
                return None
            records = reply.authorization.list_authorized_clients_response
            clients = [
                {
                    "public_key": base64.b64encode(record.public_key).decode("ascii"),
                    "state": _enum_suffix(
                        combined_pb2.AuthorizedState,
                        record.state,
                        "AUTHORIZED_STATE_",
                    ),
                    "type": _enum_suffix(
                        combined_pb2.AuthorizedClientType,
                        record.type,
                        "AUTHORIZED_CLIENT_TYPE_",
                    ),
                    "description": record.description,
                    "key_type": _enum_suffix(
                        combined_pb2.AuthorizedKeyType,
                        record.key_type,
                        "AUTHORIZED_KEY_TYPE_",
                    ),
                    "roles": [
                        _enum_suffix(
                            combined_pb2.AuthorizationRole,
                            role,
                            "AUTHORIZATION_ROLE_",
                        )
                        for role in record.roles
                    ],
                    "verification": _enum_suffix(
                        combined_pb2.AuthorizedVerificationType,
                        record.verification,
                        "AUTHORIZED_VERIFICATION_TYPE_",
                    ),
                    "added_time": (
                        record.added_time.seconds
                        if record.HasField("added_time")
                        else None
                    ),
                    "identifier": (
                        record.identifier if record.HasField("identifier") else None
                    ),
                    "authorized_by_public_key": (
                        base64.b64encode(record.authorized_by_public_key).decode("ascii")
                        if record.HasField("authorized_by_public_key")
                        else None
                    ),
                }
                for record in records.clients
            ]
            return {
                "clients": clients,
                "enable_line_switch_off": records.enable_line_switch_off,
            }
        except Exception as err:
            _LOGGER.debug("list_authorized_clients parse error: %s", err)
            return None
