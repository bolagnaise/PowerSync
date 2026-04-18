"""Unified local Powerwall client covering both PW2 and PW3.

PW2 exposes a plain HTTPS REST API with Bearer auth from ``/api/login/Basic``.
No RSA signing is required — just the customer password (last 5 digits of
the gateway serial) and the gateway IP.

PW3 removed most REST surface and routes config + commands through a signed
protobuf transport at ``/tedapi/v1r``. REST endpoints like
``/api/meters/aggregates`` still work with Bearer auth after the initial
customer login. The islanding command is unknown on PW3 and we try a
fallback chain: REST ``/api/v2/islanding/mode`` -> config.json rewrite ->
Storm Watch Manual Backup.

This module presents one interface to the rest of the integration so that
coordinator + service layers do not need to care which generation they're
talking to.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .exceptions import (
    PowerwallAuthError,
    PowerwallLocalError,
    PowerwallUnreachableError,
)
from .signaling import TeslaSignalingClient
from .transport import TEDAPIv1rTransport

_LOGGER = logging.getLogger(__name__)



class PowerwallVersion(str, Enum):
    """Known Powerwall generations. Stored in the config entry."""

    PW2 = "pw2"
    PW3 = "pw3"


@dataclass
class PowerwallSnapshot:
    """Normalized local monitoring snapshot.

    Powers are in watts (positive = flowing in the named direction). SOC is
    0-100. ``grid_status`` uses Tesla's enum strings eg ``SystemGridConnected``
    / ``SystemIslandedActive``.
    """

    soc: float | None
    solar_w: float | None
    battery_w: float | None  # positive = discharge
    grid_w: float | None  # positive = import
    load_w: float | None
    grid_status: str | None
    operation_mode: str | None
    backup_reserve_percent: int | None
    raw: dict[str, Any]


class PowerwallLocalClient:
    """Dispatches local calls between PW2 REST and PW3 TEDAPI v1r."""

    def __init__(
        self,
        host: str,
        customer_password: str,
        *,
        version: PowerwallVersion,
        private_key_pem: bytes | None = None,
        din: str | None = None,
        fleet_api_base: str | None = None,
        fleet_api_token: str | None = None,
        energy_site_id: int | str | None = None,
        signaling: TeslaSignalingClient | None = None,
    ) -> None:
        self._host = host
        self._customer_password = customer_password
        self._version = version
        self._din = din
        self._fleet_api_base = fleet_api_base
        self._fleet_api_token = fleet_api_token
        self._energy_site_id = energy_site_id
        self._signaling = signaling

        # Saved pre-curtailment state so we can restore the user's actual
        # operation mode + backup reserve when curtailment ends.
        self._saved_real_mode: str | None = None
        self._saved_reserve_percent: int | None = None
        self._curtailment_active = False


        # Both generations use the same signed transport for symmetry — on
        # PW2 the RSA signing path is unused but the REST helpers live in
        # the same class so we avoid duplicating the session/SSL setup.
        if private_key_pem is None:
            # Unsigned client (PW2-only, or pre-pairing monitoring).
            self._transport: TEDAPIv1rTransport | None = None
            self._unsigned = _UnsignedRESTClient(host, customer_password)
        else:
            self._transport = TEDAPIv1rTransport(
                host, private_key_pem, customer_password
            )
            self._unsigned = None

    @property
    def version(self) -> PowerwallVersion:
        return self._version

    @property
    def host(self) -> str:
        return self._host

    @property
    def signaling(self) -> TeslaSignalingClient | None:
        return self._signaling

    @property
    def signaling_connected(self) -> bool:
        return self._signaling is not None and self._signaling.is_connected

    @property
    def din(self) -> str | None:
        if self._din:
            return self._din
        if self._transport and self._transport.din:
            return self._transport.din
        return None

    async def _get(self, path: str) -> Any | None:
        if self._transport is not None:
            return await self._transport.api_get(path)
        assert self._unsigned is not None
        return await self._unsigned.api_get(path)

    async def _post(self, path: str, body: dict[str, Any]) -> Any | None:
        if self._transport is not None:
            return await self._transport.api_post(path, body)
        assert self._unsigned is not None
        return await self._unsigned.api_post(path, body)

    async def login(self) -> bool:
        if self._transport is not None:
            ok = await self._transport.login()
            if ok:
                # Always refresh the DIN from the gateway — the stored
                # value might be a partial serial instead of the full
                # {part_number}--{serial_number} the TEDAPI v1r transport
                # needs for TLV signature personalization.
                fetched = await self._transport.fetch_din()
                if fetched:
                    self._din = fetched
            return ok
        assert self._unsigned is not None
        return await self._unsigned.login()

    async def get_snapshot(self) -> PowerwallSnapshot:
        """Fetch the standard monitoring set in parallel-friendly order."""
        meters = await self._get("/api/meters/aggregates") or {}
        soe = await self._get("/api/system_status/soe") or {}
        grid = await self._get("/api/system_status/grid_status") or {}
        operation = await self._get("/api/operation") or {}

        def _watts(section: dict[str, Any] | None) -> float | None:
            if not isinstance(section, dict):
                return None
            v = section.get("instant_power")
            return float(v) if v is not None else None

        return PowerwallSnapshot(
            soc=_float_or_none(soe.get("percentage")),
            solar_w=_watts(meters.get("solar")),
            battery_w=_watts(meters.get("battery")),
            grid_w=_watts(meters.get("site")),
            load_w=_watts(meters.get("load")),
            grid_status=grid.get("grid_status") if isinstance(grid, dict) else None,
            operation_mode=(
                operation.get("real_mode") if isinstance(operation, dict) else None
            ),
            backup_reserve_percent=_int_or_none(
                operation.get("backup_reserve_percent")
                if isinstance(operation, dict)
                else None
            ),
            raw={
                "meters": meters,
                "soe": soe,
                "grid": grid,
                "operation": operation,
            },
        )

    async def verify_pairing(self) -> int | None:
        """Check our key's state on the gateway via list_authorized_clients.

        Returns the state integer (2=pending, 3=verified) or None if
        we couldn't determine it. Matches on our specific public key.
        """
        if not self._fleet_api_base or not self._fleet_api_token or not self._energy_site_id:
            return None

        import base64
        import aiohttp

        # Get our public key base64 to match against the gateway's list
        our_pubkey_b64: str | None = None
        if self._transport is not None:
            our_pubkey_b64 = base64.b64encode(
                self._transport._public_key_der
            ).decode()

        url = (
            f"{self._fleet_api_base}/api/1/energy_sites/"
            f"{self._energy_site_id}/command"
        )
        headers = {
            "Authorization": f"Bearer {self._fleet_api_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "command_type": "grpc_command",
            "command_properties": {
                "identifier_type": 1,
                "message": {
                    "authorization": {
                        "list_authorized_clients_request": {}
                    }
                },
            },
        }

        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                    url, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
        except Exception as err:
            _LOGGER.warning("verify_pairing: request failed: %s", err)
            return None

        # Extract clients list from response
        clients: list[dict] = []
        try:
            msg = data["response"]["message"]["Payload"]["Authorization"]["Message"]
            for key in ("ListAuthorizedClientsResponse", "list_authorized_clients_response"):
                if key in msg:
                    clients = msg[key].get("clients") or msg[key].get("Clients") or []
                    break
        except (KeyError, TypeError):
            try:
                msg = data["response"]["message"]["payload"]["authorization"]["message"]
                for key in ("ListAuthorizedClientsResponse", "list_authorized_clients_response"):
                    if key in msg:
                        clients = msg[key].get("clients") or msg[key].get("Clients") or []
                        break
            except (KeyError, TypeError):
                return None

        # Match our specific key
        for client in clients:
            pub = client.get("public_key") or client.get("Public_key", "")
            state = client.get("state") or client.get("State")
            if our_pubkey_b64 and pub == our_pubkey_b64:
                _LOGGER.info(
                    "verify_pairing: found our key — state=%s", state,
                )
                try:
                    return int(state)
                except (TypeError, ValueError):
                    return None

        _LOGGER.warning("verify_pairing: our key not found in %d clients", len(clients))
        return None

    async def go_off_grid(self, *, mode_override: int | None = None) -> bool:
        """Physically disconnect from the grid (contactor open).

        Primary path (both PW2 and PW3): local TEDAPI v1r with signed
        setIslandModeRequest sent directly to the gateway over LAN.
        No cloud relay needed — the RSA-signed protobuf goes straight
        to the gateway which verifies the signature locally.

        Cloud fallback: signed routable_message via device_command.
        PW3 default mode=6, PW2 default mode=2. Use mode_override to
        test alternative mode values.
        """
        # Verify our key is state=3 (verified) before attempting off-grid
        key_state = await self.verify_pairing()
        if key_state is not None and key_state != 3:
            _LOGGER.error(
                "go_off_grid: pairing key state=%d (not verified). "
                "Toggle the DC isolator to complete pairing.",
                key_state,
            )
            return False
        if key_state is None:
            _LOGGER.warning(
                "go_off_grid: could not verify pairing state — proceeding anyway"
            )

        # Determine default mode — mode=6 works for both PW2 and PW3
        if mode_override is not None:
            mode = mode_override
            _LOGGER.info("go_off_grid: using mode_override=%d", mode)
        else:
            mode = 6

        if not self._din:
            _LOGGER.warning("go_off_grid: no DIN")
            return False

        # Cloud signed routable_message — the only working path for
        # both PW2 and PW3. Local TEDAPI v1r returns success but does
        # not physically operate the contactor.
        _LOGGER.info("go_off_grid: cloud signed device_command (mode=%d)", mode)
        return await self._send_signed_device_command(
            off_grid=True, mode_override=mode,
        )

    async def reconnect_grid(self) -> bool:
        """Reconnect to the grid (contactor close)."""
        if not self._din:
            return False

        _LOGGER.info("reconnect_grid: cloud signed device_command (mode=1)")
        return await self._send_signed_device_command(off_grid=False)

    async def curtail_via_backup_mode(self) -> bool:
        """Stop grid export by switching to backup mode + 100% reserve.

        Uses local TEDAPI config.json write — no contactor cycling, no
        inverter restart, no solar dropout. Takes ~90s for the gateway
        to apply the config change. Saves the user's current operation
        mode + reserve so ``restore_from_curtailment`` can put them back.

        This is the mechanism for automated curtailment (negative pricing,
        demand charge windows). For manual off-grid use ``go_off_grid``.
        """
        if not self._transport or not self._din:
            _LOGGER.warning("curtail_via_backup_mode: no transport/din")
            return False

        # Read current config to save the user's values
        try:
            config = await self._transport.read_config(self._din)
            if config:
                self._saved_real_mode = config.get("default_real_mode", "self_consumption")
                si = config.get("site_info", {})
                self._saved_reserve_percent = int(si.get("backup_reserve_percent", 5))
                _LOGGER.info(
                    "curtail: saved mode=%s reserve=%s%%",
                    self._saved_real_mode, self._saved_reserve_percent,
                )
        except Exception as err:
            _LOGGER.warning("curtail: failed to read pre-curtailment config: %s", err)
            if self._saved_real_mode is None:
                self._saved_real_mode = "self_consumption"
            if self._saved_reserve_percent is None:
                self._saved_reserve_percent = 5

        ok = await self._transport.write_config(self._din, {
            "default_real_mode": "backup",
            "site_info.backup_reserve_percent": 100,
        })
        if ok:
            self._curtailment_active = True
            _LOGGER.info("curtail: config write succeeded — backup/100%%")
        else:
            _LOGGER.warning("curtail: config write failed")
        return ok

    async def restore_from_curtailment(self) -> bool:
        """Restore the user's operation mode + reserve after curtailment.

        Writes back the values captured by ``curtail_via_backup_mode``.
        """
        if not self._transport or not self._din:
            return False

        mode = self._saved_real_mode or "self_consumption"
        reserve = self._saved_reserve_percent if self._saved_reserve_percent is not None else 5

        _LOGGER.info("restore: writing mode=%s reserve=%s%%", mode, reserve)
        ok = await self._transport.write_config(self._din, {
            "default_real_mode": mode,
            "site_info.backup_reserve_percent": reserve,
        })
        if ok:
            self._curtailment_active = False
            _LOGGER.info("restore: config write succeeded")
        else:
            _LOGGER.warning("restore: config write failed")
        return ok

    @property
    def curtailment_active(self) -> bool:
        return self._curtailment_active

    async def _send_signed_device_command(
        self, *, off_grid: bool, mode_override: int | None = None,
    ) -> bool:
        """Send a signed island-mode command via cloud ``device_command``.

        Builds an RSA-signed ``RoutableMessage`` and sends through the
        cloud ``device_command`` endpoint as ``routable_message``. The
        gateway verifies our RSA signature from the paired key.
        """
        if not self._fleet_api_base or not self._fleet_api_token or not self._energy_site_id:
            return False
        if not self._transport or not self._din:
            return False

        import base64
        import aiohttp

        action = "off_grid" if off_grid else "on_grid"
        url = (
            f"{self._fleet_api_base}/api/1/energy_sites/"
            f"{self._energy_site_id}/device_command"
        )
        headers = {
            "Authorization": f"Bearer {self._fleet_api_token}",
            "Content-Type": "application/json",
        }

        # Both PW2 and PW3: mode=6 off-grid (force=True), mode=1 reconnect
        try:
            signed_bytes = self._transport.build_signed_island_mode(
                self._din, off_grid=off_grid, mode_override=mode_override,
            )
        except Exception as err:
            _LOGGER.error("signed_device_command: failed to build signed bytes: %s", err)
            return False

        msg_b64 = base64.b64encode(signed_bytes).decode()
        actual_mode = mode_override if mode_override is not None else (6 if off_grid else 1)
        _LOGGER.info(
            "signed_device_command: %s — setIslandMode(mode=%d) %d bytes",
            action, actual_mode, len(signed_bytes),
        )

        # Use "routable_message" field (NOT "energy_device_message").
        # Discovered via mitmproxy capture of Tesla app — the gateway
        # processes signed RoutableMessage bytes when sent via this field
        # and verifies the RSA signature from the paired key.
        payload = {
            "data": {
                "target_id": self._din,
                "routable_message": msg_b64,
                "command_timeout_s": 10,
                "identifier_type": 1,
            }
        }

        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=35),
                ) as resp:
                    body = await resp.text()
                    _LOGGER.info(
                        "signed_device_command %s: HTTP %d — %s",
                        action, resp.status, body[:500],
                    )
                    if resp.status != 200:
                        return False

                    return resp.status == 200 and "response" in body

        except Exception as err:
            _LOGGER.error("signed_device_command %s error: %s", action, err)
            return False

    async def test_signed_read_via_fleet(self) -> dict | None:
        """SPIKE: send a signed filestore.readFileRequest via Fleet API device_command.

        Same signing path as island-mode cloud relay, but the inner MessageEnvelope
        carries filestore.readFileRequest instead of teg.setIslandModeRequest.
        Goal: prove Fleet API relays non-TEG envelopes and returns gateway config.
        Remove this method after go/no-go decision.
        """
        import base64
        import json as _json

        import aiohttp

        from . import tedapi_combined_pb2 as combined_pb2

        if not self._fleet_api_base or not self._fleet_api_token or not self._energy_site_id:
            _LOGGER.warning("SPIKE: fleet_api not configured, cannot test")
            return None
        if not self._transport or not self._din:
            _LOGGER.warning("SPIKE: transport/din not ready, cannot test")
            return None

        # Build the same envelope that read_config() uses on LAN
        msg = combined_pb2.Message()
        envelope = msg.message
        envelope.deliveryChannel = combined_pb2.DELIVERY_CHANNEL_HERMES_COMMAND
        envelope.sender.authorizedClient = 1
        envelope.recipient.din = self._din
        req = envelope.filestore.readFileRequest
        req.domain = combined_pb2.FILE_STORE_API_DOMAIN_CONFIG_JSON
        req.name = "config.json"

        # Sign identically to island-mode cloud path
        try:
            signed_bytes = self._transport.build_signed_bytes(
                envelope.SerializeToString(), self._din
            )
        except Exception as err:
            _LOGGER.error("SPIKE: signing failed: %s", err)
            return None

        msg_b64 = base64.b64encode(signed_bytes).decode()
        url = (
            f"{self._fleet_api_base}/api/1/energy_sites/"
            f"{self._energy_site_id}/device_command"
        )
        payload = {
            "data": {
                "target_id": self._din,
                "routable_message": msg_b64,
                "command_timeout_s": 10,
                "identifier_type": 1,
            }
        }
        headers = {
            "Authorization": f"Bearer {self._fleet_api_token}",
            "Content-Type": "application/json",
        }

        _LOGGER.warning("SPIKE: posting signed readFileRequest to Fleet API %s din=%s site=%s", url, self._din, self._energy_site_id)
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                    url, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=35),
                ) as resp:
                    body = await resp.text()
                    _LOGGER.warning("SPIKE: HTTP %d — %s", resp.status, body[:2000])
                    if resp.status != 200:
                        return None

                    # Fleet API response: {"response": {"message_envelope_as_bytes": "<b64>"}}
                    # The value is a MessageEnvelope serialised directly (not wrapped in RoutableMessage).
                    try:
                        resp_json = _json.loads(body)
                        envelope_b64 = (
                            resp_json.get("response", {}).get("message_envelope_as_bytes")
                        )
                        if envelope_b64:
                            env_bytes = base64.b64decode(envelope_b64)
                            env_resp = combined_pb2.MessageEnvelope()
                            env_resp.ParseFromString(env_bytes)
                            if env_resp.HasField("filestore"):
                                blob = env_resp.filestore.readFileResponse.file.blob
                                result = _json.loads(blob.decode("utf-8"))
                                _LOGGER.warning("SPIKE: SUCCESS — decoded config blob keys: %s", list(result.keys()))
                                return result
                            _LOGGER.warning("SPIKE: envelope had no filestore — fields: %s", [f[0].name for f in env_resp.ListFields()])
                        else:
                            _LOGGER.warning("SPIKE: no message_envelope_as_bytes in response keys: %s", list(resp_json.get("response", {}).keys()))
                    except Exception as decode_err:
                        _LOGGER.error("SPIKE: decode failed: %s", decode_err)
        except Exception as err:
            _LOGGER.error("SPIKE: request error: %s", err)
        return None

    async def verify_paired(self) -> bool:
        """Best-effort check that the RSA key is still accepted.

        Sends a trivial TEDAPI read; if the gateway replies with
        MESSAGEFAULT_ERROR_UNKNOWN_KEY_ID the transport raises
        ``PowerwallSignatureError`` which we surface as "not paired".
        """
        if self._transport is None:
            return await self.login()
        if not self._din:
            self._din = await self._transport.fetch_din()
        if not self._din:
            return False
        try:
            config = await self._transport.read_config(self._din)
        except PowerwallLocalError:
            return False
        return config is not None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class _UnsignedRESTClient:
    """Minimal REST client for PW2 without an RSA transport.

    Shares the self-signed SSL context with the v1r transport but skips all
    protobuf machinery. Used before pairing completes so the app can still
    display live data, and on pure PW2 installs where pairing is optional.
    """

    def __init__(self, host: str, customer_password: str) -> None:
        import aiohttp

        from .transport import _insecure_ssl_context

        self._host = host
        self._customer_password = customer_password
        self._ssl = _insecure_ssl_context()
        self._timeout = aiohttp.ClientTimeout(total=8.0)
        self._token: str | None = None
        self._aiohttp = aiohttp

    async def _session(self):
        connector = self._aiohttp.TCPConnector(ssl=self._ssl, limit=4)
        return self._aiohttp.ClientSession(
            connector=connector, timeout=self._timeout
        )

    async def login(self) -> bool:
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
                    if resp.status in (401, 403):
                        raise PowerwallAuthError(
                            f"Gateway rejected customer password ({resp.status})"
                        )
                    if resp.status != 200:
                        return False
                    data = await resp.json()
                    self._token = data.get("token")
                    return self._token is not None
        except self._aiohttp.ClientError as err:
            raise PowerwallUnreachableError(str(err)) from err

    async def api_get(self, path: str) -> Any | None:
        if not self._token and not await self.login():
            return None
        url = f"https://{self._host}{path}"
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            async with await self._session() as sess:
                async with sess.get(url, headers=headers) as resp:
                    if resp.status in (401, 403) and await self.login():
                        headers["Authorization"] = f"Bearer {self._token}"
                        async with sess.get(url, headers=headers) as r2:
                            if r2.status != 200:
                                return None
                            return await r2.json()
                    if resp.status != 200:
                        return None
                    return await resp.json()
        except self._aiohttp.ClientError as err:
            raise PowerwallUnreachableError(str(err)) from err

    async def api_post(self, path: str, body: dict[str, Any]) -> Any | None:
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
                    if resp.status in (401, 403) and await self.login():
                        headers["Authorization"] = f"Bearer {self._token}"
                        async with sess.post(url, json=body, headers=headers) as r2:
                            if r2.status not in (200, 201, 204):
                                text = await r2.text()
                                _LOGGER.warning(
                                    "POST %s retry returned %s: %s",
                                    path, r2.status, text[:300],
                                )
                                return None
                            return {} if r2.status == 204 else await r2.json()
                    if resp.status not in (200, 201, 204):
                        text = await resp.text()
                        _LOGGER.warning(
                            "POST %s returned %s: %s",
                            path, resp.status, text[:300],
                        )
                        return None
                    return {} if resp.status == 204 else await resp.json()
        except self._aiohttp.ClientError as err:
            raise PowerwallUnreachableError(str(err)) from err
