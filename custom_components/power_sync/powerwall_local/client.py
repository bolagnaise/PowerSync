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

ISLAND_MODE_OFFGRID = "intentional_reconnect_failsafe"
ISLAND_MODE_ONGRID = "backup"
ISLAND_MODE_PATH = "/api/v2/islanding/mode"


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
        # Determine default mode based on version
        if mode_override is not None:
            mode = mode_override
            _LOGGER.info("go_off_grid: using mode_override=%d", mode)
        elif self._version == PowerwallVersion.PW3:
            mode = 6
        else:
            mode = 2

        # Local TEDAPI v1r — direct to gateway, no cloud needed
        if self._din and self._transport:
            _LOGGER.info("go_off_grid: local TEDAPI set_island_mode (mode=%d)", mode)
            ok = await self._transport.set_island_mode(self._din, off_grid=True)
            if ok:
                return True
            _LOGGER.warning("go_off_grid: local TEDAPI failed, trying cloud")

            # Cloud fallback: signed routable_message (both PW2 and PW3)
            _LOGGER.info("go_off_grid: cloud fallback signed device_command (mode=%d)", mode)
            return await self._send_signed_device_command(
                off_grid=True, mode_override=mode,
            )

        # No transport — cloud only (signed path)
        if self._din:
            _LOGGER.info("go_off_grid: cloud signed device_command (mode=%d, no transport)", mode)
            return await self._send_signed_device_command(
                off_grid=True, mode_override=mode,
            )

        _LOGGER.warning("go_off_grid: no transport and no DIN")
        return False

    async def reconnect_grid(self) -> bool:
        """Reconnect to the grid (contactor close)."""
        # Local TEDAPI v1r — direct to gateway
        if self._din and self._transport:
            _LOGGER.info("reconnect_grid: local TEDAPI set_island_mode (mode=1)")
            ok = await self._transport.set_island_mode(self._din, off_grid=False)
            if ok:
                return True
            _LOGGER.warning("reconnect_grid: local TEDAPI failed, trying cloud")

            return await self._send_signed_device_command(off_grid=False)

        if self._din:
            return await self._send_signed_device_command(off_grid=False)

        return False

    async def _send_signed_warmup(self) -> None:
        """Send a signed routable_message to establish the gateway session.

        The PW2 Tesla app sends a signed get_backup_events_request before
        the actual unsigned island command. This establishes the cloud
        delivery path so the subsequent unsigned command reaches the gateway.

        Uses build_signed_island_mode with mode=2 as a lightweight signed
        message that the gateway will process. The important thing is that
        the gateway receives and verifies a signed message from our key,
        which wakes the cloud session for subsequent unsigned commands.
        """
        if not self._fleet_api_base or not self._fleet_api_token or not self._energy_site_id:
            return
        if not self._transport or not self._din:
            return

        import base64
        import aiohttp

        # Send a signed setIslandModeRequest as the warm-up. The gateway
        # will verify our RSA signature which establishes the delivery path.
        try:
            signed_bytes = self._transport.build_signed_island_mode(
                self._din, off_grid=True, mode_override=2,
            )
        except Exception as err:
            _LOGGER.warning("signed_warmup: failed to build: %s", err)
            return

        msg_b64 = base64.b64encode(signed_bytes).decode()
        url = (
            f"{self._fleet_api_base}/api/1/energy_sites/"
            f"{self._energy_site_id}/device_command"
        )
        payload = {
            "data": {
                "target_id": self._din,
                "routable_message": msg_b64,
                "identifier_type": 1,
            }
        }
        headers = {
            "Authorization": f"Bearer {self._fleet_api_token}",
            "Content-Type": "application/json",
        }

        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                    url, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    body = await resp.text()
                    _LOGGER.info(
                        "signed_warmup: HTTP %d — %s", resp.status, body[:200],
                    )
        except Exception as err:
            _LOGGER.warning("signed_warmup error: %s", err)

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

    async def _local_tedapi_island(self, *, off_grid: bool) -> bool:
        """Try local TEDAPI islanding — set mode then trigger contactor.

        Sends both ``setIslandModeRequest`` and (for off-grid)
        ``triggerIslandingBlackStartRequest`` via the local TEDAPI v1r
        transport. The gateway must be reachable on the LAN and our
        RSA key must be paired.
        """
        assert self._transport is not None
        din = self._din
        if not din:
            return False

        import asyncio

        action = "off_grid" if off_grid else "on_grid"
        _LOGGER.info("local_tedapi_island: %s (din=%s)", action, din)

        # Step 1: Set the desired island mode
        try:
            mode_ok = await self._transport.set_island_mode(din, off_grid=off_grid)
            _LOGGER.info(
                "local_tedapi_island: set_island_mode(%s) → %s",
                action, mode_ok,
            )
        except Exception as err:
            _LOGGER.warning("local_tedapi_island: set_island_mode error: %s", err)
            mode_ok = False

        # Step 2: For off-grid, trigger the actual contactor open
        if off_grid:
            # Brief delay to let the mode setting propagate
            await asyncio.sleep(1)
            try:
                trigger_ok = await self._transport.trigger_islanding(din)
                _LOGGER.info(
                    "local_tedapi_island: trigger_islanding → %s", trigger_ok,
                )
            except Exception as err:
                _LOGGER.warning(
                    "local_tedapi_island: trigger_islanding error: %s", err,
                )
                trigger_ok = False
            return mode_ok or trigger_ok

        return mode_ok

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

        # PW3: mode=6 off-grid, mode=1 reconnect
        # PW2: mode=2 off-grid, mode=1 reconnect
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

    async def _send_island_command(self, *, off_grid: bool) -> bool:
        """Send islanding command via Fleet API ``/command`` endpoint.

        This is the same cloud relay used for RSA key pairing — it sends
        a ``grpc_command`` JSON payload that the cloud forwards to the
        gateway as a protobuf message. This is likely how Netzero
        implements off-grid control.

        Tries ``triggerIslandingBlackStartRequest`` first (the actual
        contactor command), then falls back to ``setIslandModeRequest``.
        """
        if not self._fleet_api_base or not self._fleet_api_token or not self._energy_site_id:
            _LOGGER.warning("island_command: missing fleet API context")
            return False

        import aiohttp

        action = "off_grid" if off_grid else "on_grid"
        url = (
            f"{self._fleet_api_base}/api/1/energy_sites/"
            f"{self._energy_site_id}/command"
        )
        headers = {
            "Authorization": f"Bearer {self._fleet_api_token}",
            "Content-Type": "application/json",
        }

        # Attempt 1: triggerIslandingBlackStartRequest (contactor command)
        if off_grid:
            payload_trigger = {
                "command_properties": {
                    "message": {
                        "teg": {
                            "trigger_islanding_black_start_request": {}
                        }
                    },
                    "identifier_type": 1,
                },
                "command_type": "grpc_command",
            }
            _LOGGER.info(
                "island_command: sending triggerIslandingBlackStartRequest "
                "via cloud command → %s",
                url,
            )
            result = await self._post_cloud_command(
                url, payload_trigger, headers, "triggerIslandingBlackStart"
            )
            if result:
                return True

        # Attempt 2: setIslandModeRequest (mode preference)
        mode = 2 if off_grid else 1  # 2 = off_grid, 1 = on_grid
        payload_mode = {
            "command_properties": {
                "message": {
                    "teg": {
                        "set_island_mode_request": {
                            "mode": mode,
                            "force": True,
                        }
                    }
                },
                "identifier_type": 1,
            },
            "command_type": "grpc_command",
        }
        _LOGGER.info(
            "island_command: sending setIslandModeRequest (mode=%d) "
            "via cloud command → %s",
            mode, url,
        )
        return await self._post_cloud_command(
            url, payload_mode, headers, f"setIslandMode({action})"
        )

    async def _post_cloud_command(
        self,
        url: str,
        payload: dict,
        headers: dict[str, str],
        label: str,
    ) -> bool:
        """POST a grpc_command to the Fleet API command endpoint."""
        import aiohttp

        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        _LOGGER.warning(
                            "island_command %s: HTTP %d: %s",
                            label, resp.status, body[:300],
                        )
                        return False
                    _LOGGER.info(
                        "island_command %s: 200 OK — response: %s",
                        label, body[:500],
                    )
                    return True
        except Exception as err:
            _LOGGER.error("island_command %s error: %s", label, err)
            return False

    # Off-grid: base64 protobuf — captured from Tesla app via mitmproxy.
    # Decodes to field 6 → field 5 → field 1 = 2 (setIslandMode mode=2).
    _OFFGRID_MSG = "MgQqAggC"
    # Reconnect: base64 protobuf — captured from Tesla app via mitmproxy.
    # Different message structure than off-grid (field 1 → field 22 empty).
    _ONGRID_MSG = "CgOyAQA="

    # Max retries for device_command — gateway may need a moment to
    # establish its cloud session after the pre-warm ping.
    _DEVICE_CMD_MAX_RETRIES = 3
    _DEVICE_CMD_RETRY_DELAY_S = 3

    async def _send_device_command(self, *, off_grid: bool) -> bool:
        """Send off-grid/reconnect via Tesla cloud ``/device_command`` endpoint.

        This is the exact mechanism the Tesla mobile app uses — discovered
        via mitmproxy capture of the app's "Go Off-Grid" button. The cloud
        relays a base64-encoded protobuf to the gateway which physically
        opens or closes the grid contactor. Confirmed working on PW3
        firmware 26.2.1.

        Includes a pre-warm step (lightweight API call to wake the gateway's
        cloud session) and retries with backoff for reliability.
        """
        if not self._fleet_api_base or not self._fleet_api_token or not self._energy_site_id:
            _LOGGER.warning(
                "device_command: missing fleet_api_base=%s token=%s site=%s",
                bool(self._fleet_api_base),
                bool(self._fleet_api_token),
                self._energy_site_id,
            )
            return False

        import asyncio
        import aiohttp

        msg = self._OFFGRID_MSG if off_grid else self._ONGRID_MSG
        action = "off_grid" if off_grid else "on_grid"

        if self.signaling_connected:
            _LOGGER.info(
                "device_command %s: signaling WebSocket connected — "
                "gateway cloud session should be active",
                action,
            )
        else:
            _LOGGER.warning(
                "device_command %s: signaling WebSocket NOT connected — "
                "will pre-warm gateway session before sending command",
                action,
            )

        url = (
            f"{self._fleet_api_base}/api/1/energy_sites/"
            f"{self._energy_site_id}/device_command"
        )
        payload = {
            "data": {
                "target_id": self._din,
                "energy_device_message": msg,
                "command_timeout_s": 30,
                "identifier_type": 1,
            }
        }
        headers = {
            "Authorization": f"Bearer {self._fleet_api_token}",
            "Content-Type": "application/json",
        }

        # Pre-warm: hit a lightweight energy site endpoint to nudge the
        # cloud into establishing a session with the gateway. This gives
        # device_command a delivery path even without signaling WebSocket.
        if not self.signaling_connected:
            await self._prewarm_gateway_session(headers)

        for attempt in range(1, self._DEVICE_CMD_MAX_RETRIES + 1):
            _LOGGER.info(
                "device_command: %s → %s (din=%s, attempt %d/%d)",
                action, url, self._din, attempt, self._DEVICE_CMD_MAX_RETRIES,
            )
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.post(
                        url,
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=35),
                    ) as resp:
                        if resp.status == 408:
                            body = await resp.text()
                            _LOGGER.warning(
                                "device_command %s: 408 timeout (attempt %d) "
                                "— gateway may not have an active cloud "
                                "session: %s",
                                action, attempt, body[:200],
                            )
                            if attempt < self._DEVICE_CMD_MAX_RETRIES:
                                await asyncio.sleep(
                                    self._DEVICE_CMD_RETRY_DELAY_S * attempt
                                )
                                continue
                            return False

                        if resp.status == 429:
                            body = await resp.text()
                            _LOGGER.warning(
                                "device_command %s: rate limited (429): %s",
                                action, body[:200],
                            )
                            if attempt < self._DEVICE_CMD_MAX_RETRIES:
                                await asyncio.sleep(5)
                                continue
                            return False

                        if resp.status != 200:
                            body = await resp.text()
                            _LOGGER.warning(
                                "device_command %s failed (%s): %s",
                                action, resp.status, body[:300],
                            )
                            return False

                        data = await resp.json()
                        _LOGGER.info(
                            "device_command %s: response=%s",
                            action, str(data)[:300],
                        )
                        return "response" in data

            except asyncio.TimeoutError:
                _LOGGER.warning(
                    "device_command %s: HTTP timeout (attempt %d)",
                    action, attempt,
                )
                if attempt < self._DEVICE_CMD_MAX_RETRIES:
                    await asyncio.sleep(
                        self._DEVICE_CMD_RETRY_DELAY_S * attempt
                    )
                    continue
                return False
            except Exception as err:
                _LOGGER.error(
                    "device_command %s error (attempt %d): %s",
                    action, attempt, err,
                )
                return False

        return False

    async def _prewarm_gateway_session(self, headers: dict[str, str]) -> None:
        """Hit a lightweight Fleet API endpoint to wake the gateway.

        The cloud may establish a session with the gateway in response
        to an API call, giving device_command a delivery path even when
        the signaling WebSocket is not connected.
        """
        import aiohttp

        prewarm_url = (
            f"{self._fleet_api_base}/api/1/energy_sites/"
            f"{self._energy_site_id}/live_status"
        )
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(
                    prewarm_url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    _LOGGER.info(
                        "device_command pre-warm: GET live_status → %d",
                        resp.status,
                    )
        except Exception as err:
            _LOGGER.debug("device_command pre-warm failed (non-fatal): %s", err)

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
