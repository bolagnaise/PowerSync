"""Unified local Powerwall client for PW2 and PW3 over RSA-signed v1r.

Both generations now route all reads and writes through the signed protobuf
transport at ``/tedapi/v1r``. Live snapshots come from a single
``DeviceControllerQuery`` envelope plus a ``config.json`` read for the
operation mode and backup reserve — no Bearer login or customer password
required. Islanding commands use the same signed transport.

The RSA private key + DIN are established during cloud pairing (Fleet API);
without both, this client refuses to construct.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .exceptions import PowerwallLocalError, PowerwallUnreachableError
from .fleet_api_bms import (
    build_device_controller_query_envelope,
    parse_device_controller_response,
)
from ..powerwall_host import normalize_powerwall_gateway_host
from .normalization import (
    normalize_local_backup_reserve_percent,
    normalize_local_soc_percent,
)
from .pairing import _authorization_command_payload
from .signaling import TeslaSignalingClient
from .transport import TEDAPIv1rTransport

_LOGGER = logging.getLogger(__name__)

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
_AUTHORIZED_STATE_CODES = {
    "PENDING_VERIFICATION": 1,
    "PENDING_VERIFICATION_TIMEOUT": 2,
    "VERIFIED": 3,
    "REMOVED": 4,
}
_ISLANDED_GRID_STATES = {"SystemIslandedActive", "SystemIslandedReady"}


def is_loopback_host(host: str | None) -> bool:
    """Return True for loopback placeholders that are not real gateway hosts."""
    return str(host or "").strip().lower() in _LOOPBACK_HOSTS


def _matching_authorized_client_state(
    clients: list[dict[str, Any]], public_key_b64: str
) -> int | None:
    """Return the numeric state for our key across local/cloud payload shapes."""
    for client in clients:
        public_key = client.get("public_key") or client.get("Public_key", "")
        if public_key != public_key_b64:
            continue
        state = client.get("state")
        if state is None:
            state = client.get("State")
        if isinstance(state, int):
            return state
        state_text = str(state or "").strip().upper()
        if state_text.startswith("AUTHORIZED_STATE_"):
            state_text = state_text.removeprefix("AUTHORIZED_STATE_")
        if state_text in _AUTHORIZED_STATE_CODES:
            return _AUTHORIZED_STATE_CODES[state_text]
        try:
            return int(state_text)
        except (TypeError, ValueError):
            return None
    return None


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
    grid_charging_enabled: bool | None = None
    grid_export_rule: str | None = None
    # Best-effort fields from /api/system_status (PW2; may be None on PW3).
    # Keep None when the endpoint is unsupported so consumers know to skip.
    system_island_state: str | None = None
    pw_count: int | None = None
    total_pack_full_wh: float | None = None
    total_pack_remaining_wh: float | None = None
    battery_blocks: list[dict[str, Any]] | None = None
    alerts: list[dict[str, Any]] | None = None


class PowerwallLocalClient:
    """RSA-signed local Powerwall client (both PW2 and PW3)."""

    def __init__(
        self,
        host: str,
        *,
        version: PowerwallVersion,
        private_key_pem: bytes,
        din: str,
        fleet_api_base: str | None = None,
        fleet_api_token: str | None = None,
        energy_site_id: int | str | None = None,
        signaling: TeslaSignalingClient | None = None,
        local_access_enabled: bool = True,
    ) -> None:
        if not private_key_pem:
            raise PowerwallLocalError(
                "PowerwallLocalClient requires the RSA private key from cloud pairing"
            )
        if not din:
            raise PowerwallLocalError(
                "PowerwallLocalClient requires the gateway DIN from cloud pairing"
            )

        self._host = normalize_powerwall_gateway_host(host)
        self._version = version
        self._din = din
        self._fleet_api_base = fleet_api_base
        self._fleet_api_token = fleet_api_token
        self._energy_site_id = energy_site_id
        self._signaling = signaling
        self._local_access_enabled = local_access_enabled

        # Saved pre-curtailment state so we can restore the user's actual
        # operation mode + backup reserve when curtailment ends.
        self._saved_real_mode: str | None = None
        self._saved_reserve_percent: int | None = None
        self._curtailment_active = False

        self._transport: TEDAPIv1rTransport = TEDAPIv1rTransport(
            self._host, private_key_pem, din=din,
        )

    @property
    def version(self) -> PowerwallVersion:
        return self._version

    @property
    def host(self) -> str:
        return self._host

    @property
    def local_access_enabled(self) -> bool:
        """Whether LAN TEDAPI calls should be attempted for this client."""
        return self._local_access_enabled

    @property
    def signaling(self) -> TeslaSignalingClient | None:
        return self._signaling

    @property
    def signaling_connected(self) -> bool:
        return self._signaling is not None and self._signaling.is_connected

    @property
    def din(self) -> str | None:
        return self._din or self._transport.din

    async def _fetch_dcq_local(self) -> dict[str, Any] | None:
        """Send a DeviceControllerQuery directly to the gateway over LAN.

        Builds the same protobuf envelope used by the Fleet-API cloud relay
        path but POSTs it through the signed v1r transport for sub-100ms
        latency. Returns the decoded JSON payload or None on any failure.
        """
        try:
            envelope = build_device_controller_query_envelope(self._din)
        except Exception as err:
            _LOGGER.error("DeviceControllerQuery encode failed: %s", err)
            return None

        try:
            resp = await self._transport.post_v1r(envelope, self._din)
        except PowerwallUnreachableError:
            raise
        except PowerwallLocalError as err:
            _LOGGER.warning("DeviceControllerQuery v1r POST failed: %s", err)
            return None
        if not resp.ok or not resp.inner_bytes:
            _LOGGER.debug(
                "DeviceControllerQuery returned no inner bytes (fault=%s, http=%s)",
                resp.fault_name, resp.http_status,
            )
            return None

        try:
            return parse_device_controller_response(resp.inner_bytes)
        except Exception as err:
            _LOGGER.warning("DeviceControllerQuery decode error: %s", err)
            return None

    async def get_snapshot(self) -> PowerwallSnapshot:
        """Fetch live status via RSA-signed DCQ + config.json read in parallel."""
        if not self._local_access_enabled or is_loopback_host(self._host):
            raise PowerwallUnreachableError(
                "Powerwall local access disabled; gateway IP is not configured"
            )

        dcq_task = self._fetch_dcq_local()
        cfg_task = self._transport.read_config(self._din)
        dcq, cfg = await asyncio.gather(dcq_task, cfg_task, return_exceptions=True)

        if isinstance(dcq, BaseException):
            if isinstance(dcq, PowerwallUnreachableError):
                raise dcq
            _LOGGER.warning("DeviceControllerQuery raised: %s", dcq)
            dcq = None
        if isinstance(cfg, BaseException):
            _LOGGER.debug("config.json read raised: %s", cfg)
            cfg = None

        if not dcq:
            raise PowerwallUnreachableError(
                "Gateway returned no DeviceControllerQuery data"
            )

        return _snapshot_from_dcq(dcq, cfg if isinstance(cfg, dict) else None)

    async def verify_pairing(self) -> int | None:
        """Check our key's state on the gateway via list_authorized_clients.

        Returns the state integer (2=pending, 3=verified) or None if
        we couldn't determine it. Matches on our specific public key.
        """
        our_pubkey_b64 = base64.b64encode(
            self._transport._public_key_der
        ).decode()

        if self._local_access_enabled and not is_loopback_host(self._host):
            try:
                payload = await self.list_authorized_clients()
            except Exception as err:
                _LOGGER.warning(
                    "verify_pairing: local authorized-client read failed; "
                    "falling back to Fleet API: %s",
                    err,
                )
            else:
                if payload is not None:
                    state = _matching_authorized_client_state(
                        payload.get("clients") or [], our_pubkey_b64
                    )
                    if state is not None:
                        _LOGGER.info(
                            "verify_pairing: found our key locally — state=%s",
                            state,
                        )
                        return state
                    _LOGGER.warning(
                        "verify_pairing: our key not found in %d local clients",
                        len(payload.get("clients") or []),
                    )
                    return None

        return await self._verify_pairing_cloud(our_pubkey_b64)

    async def _verify_pairing_cloud(self, our_pubkey_b64: str) -> int | None:
        """Fall back to the existing Fleet API authorized-client lookup."""
        if not self._fleet_api_base or not self._fleet_api_token or not self._energy_site_id:
            return None

        import aiohttp

        url = (
            f"{self._fleet_api_base}/api/1/energy_sites/"
            f"{self._energy_site_id}/command"
        )
        headers = {
            "Authorization": f"Bearer {self._fleet_api_token}",
            "Content-Type": "application/json",
        }
        payload = _authorization_command_payload(
            "list_authorized_clients_request",
            {"list_authorized_clients_request": {}},
        )

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

        state = _matching_authorized_client_state(clients, our_pubkey_b64)
        if state is not None:
            _LOGGER.info("verify_pairing: found our key via cloud — state=%s", state)
            return state

        _LOGGER.warning("verify_pairing: our key not found in %d clients", len(clients))
        return None

    async def set_grid_import_export(
        self,
        *,
        customer_preferred_export_rule: str | None = None,
        disallow_charge_from_grid_with_solar_installed: bool | None = None,
    ) -> bool:
        """Write local export and/or grid-charge policy in one config update."""
        updates: dict[str, Any] = {}
        if customer_preferred_export_rule is not None:
            if customer_preferred_export_rule not in ("battery_ok", "pv_only", "never"):
                raise ValueError(
                    "customer_preferred_export_rule must be battery_ok, pv_only, or never"
                )
            updates["site_info.customer_preferred_export_rule"] = (
                customer_preferred_export_rule
            )
        if disallow_charge_from_grid_with_solar_installed is not None:
            updates["site_info.disallow_charge_from_grid_with_solar_installed"] = bool(
                disallow_charge_from_grid_with_solar_installed
            )
        if not updates:
            raise ValueError("at least one grid import/export setting is required")
        return await self._transport.write_config(self._din, updates)

    async def schedule_max_backup(self, duration_seconds: int = 7200) -> bool:
        """Schedule a local manual max-backup event."""
        return await self._transport.schedule_manual_backup(
            self._din, duration_seconds
        )

    async def cancel_max_backup(self) -> bool:
        """Cancel the active local manual max-backup event."""
        return await self._transport.cancel_manual_backup(self._din)

    async def get_backup_events(self) -> dict[str, Any] | None:
        """Read local manual and scheduled backup events."""
        return await self._transport.get_backup_events(self._din)

    async def get_v1r_diagnostics(self) -> dict[str, Any]:
        """Read the safe Common API diagnostics published by the gateway."""
        system_info, networking, internet = await asyncio.gather(
            self._transport.get_system_info(self._din),
            self._transport.get_networking_status(self._din),
            self._transport.check_internet(self._din),
        )
        return {
            "system_info": system_info,
            "networking": networking,
            "internet": internet,
        }

    async def list_authorized_clients(self) -> dict[str, Any] | None:
        """Read authorized clients directly from the local gateway."""
        return await self._transport.list_authorized_clients(self._din)

    async def trigger_islanding(self) -> bool:
        """Send the explicit local black-start/islanding trigger."""
        return await self._transport.trigger_islanding(self._din)

    async def _wait_for_grid_state(
        self, *, off_grid: bool, timeout: float = 12.0
    ) -> bool:
        """Confirm a local island command from fresh gateway telemetry."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                snapshot = await self.get_snapshot()
            except Exception as err:
                _LOGGER.debug("grid-state readback failed while waiting: %s", err)
            else:
                is_off_grid = snapshot.grid_status in _ISLANDED_GRID_STATES
                if is_off_grid == off_grid:
                    return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(1.0)

    async def go_off_grid(self, *, mode_override: int | None = None) -> bool:
        """Physically disconnect from the grid (contactor open).

        Tries the local V1R set-island + black-start sequence first and accepts
        it only after fresh local telemetry confirms the contactor state. A
        gateway acknowledgement without actuation therefore falls through to
        the proven Fleet ``device_command`` relay path.

        Default mode=6 works for both PW2 and PW3. Use ``mode_override``
        to test alternative values.
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

        if self._local_access_enabled and not is_loopback_host(self._host):
            try:
                set_ok = await self._transport.set_island_mode(
                    self._din,
                    off_grid=True,
                    force=True,
                    mode_override=mode,
                )
                trigger_ok = await self.trigger_islanding() if set_ok else False
                if (set_ok or trigger_ok) and await self._wait_for_grid_state(
                    off_grid=True
                ):
                    _LOGGER.info("go_off_grid: confirmed via local V1R")
                    return True
                _LOGGER.warning(
                    "go_off_grid: local V1R did not confirm islanding; "
                    "falling back to cloud"
                )
            except Exception as err:
                _LOGGER.warning(
                    "go_off_grid: local V1R failed; falling back to cloud: %s",
                    err,
                )

        _LOGGER.info("go_off_grid: cloud signed device_command fallback (mode=%d)", mode)
        return await self._send_signed_device_command(
            off_grid=True, mode_override=mode,
        )

    async def reconnect_grid(self) -> bool:
        """Reconnect to the grid (contactor close)."""
        if not self._din:
            return False

        if self._local_access_enabled and not is_loopback_host(self._host):
            try:
                local_ok = await self._transport.set_island_mode(
                    self._din, off_grid=False, force=False
                )
                if local_ok and await self._wait_for_grid_state(off_grid=False):
                    _LOGGER.info("reconnect_grid: confirmed via local V1R")
                    return True
                _LOGGER.warning(
                    "reconnect_grid: local V1R did not confirm reconnection; "
                    "falling back to cloud"
                )
            except Exception as err:
                _LOGGER.warning(
                    "reconnect_grid: local V1R failed; falling back to cloud: %s",
                    err,
                )

        _LOGGER.info("reconnect_grid: cloud signed device_command fallback (mode=1)")
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

    async def fetch_device_controller_json(self) -> dict | None:
        """Fetch Tesla BMS JSON via signed DeviceControllerQuery + Fleet API relay.

        Builds a signed RoutableMessage wrapping the DeviceControllerQuery envelope,
        posts it to the Fleet API device_command endpoint, and returns the decoded
        JSON payload from the gateway response. Returns None on any failure.
        """
        if not (self._fleet_api_base and self._fleet_api_token and self._energy_site_id):
            return None
        if not (self._transport and self._din):
            return None

        import base64
        import aiohttp

        from .fleet_api_bms import (
            build_device_controller_query_envelope,
            parse_device_controller_response,
        )

        try:
            envelope = build_device_controller_query_envelope(self._din)
            # Use 300 s TTL to absorb Cloudflare → Tesla → Gateway round-trip latency.
            signed = self._transport.build_signed_bytes(
                envelope, self._din, ttl_seconds=300
            )
        except Exception as err:
            _LOGGER.error("fetch_device_controller_json: failed to build signed bytes: %s", err)
            return None

        msg_b64 = base64.b64encode(signed).decode()
        url = (
            f"{self._fleet_api_base}/api/1/energy_sites/"
            f"{self._energy_site_id}/device_command"
        )
        headers = {
            "Authorization": f"Bearer {self._fleet_api_token}",
            "Content-Type": "application/json",
        }
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
                    if resp.status != 200:
                        body_text = await resp.text()
                        _LOGGER.warning(
                            "fetch_device_controller_json: HTTP %d — %s",
                            resp.status, body_text[:400],
                        )
                        return None
                    body = await resp.json()
        except Exception as err:
            _LOGGER.error("fetch_device_controller_json: request error: %s", err)
            return None

        envelope_b64 = (body.get("response") or {}).get("message_envelope_as_bytes")
        if not envelope_b64:
            _LOGGER.warning(
                "fetch_device_controller_json: no message_envelope_as_bytes in response: %s",
                str(body)[:400],
            )
            return None

        try:
            result = parse_device_controller_response(base64.b64decode(envelope_b64))
            if result is None:
                _LOGGER.warning("fetch_device_controller_json: failed to extract text from envelope")
            return result
        except Exception as err:
            _LOGGER.warning("fetch_device_controller_json: decode error: %s", err)
            return None

    async def verify_paired(self) -> bool:
        """Best-effort check that the RSA key is still accepted.

        Sends a trivial TEDAPI read; if the gateway replies with
        MESSAGEFAULT_ERROR_UNKNOWN_KEY_ID the transport raises
        ``PowerwallSignatureError`` which we surface as "not paired".
        """
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


# DCQ "islanding.customerIslandMode" → grid_status string. Tesla's REST returned
# "SystemGridConnected" / "SystemIslandedActive" / etc.; the DCQ uses simpler
# enum names that we map back so downstream consumers (sensors that branch on
# the historical strings) keep working.
_DCQ_ISLAND_MODE_TO_GRID_STATUS = {
    "ISLAND_MODE_UNKNOWN": None,
    "OnGrid": "SystemGridConnected",
    "Backup": "SystemIslandedActive",
    "OffGrid": "SystemIslandedActive",
    "Normal": "SystemGridConnected",
}

def _snapshot_from_dcq(
    dcq: dict[str, Any],
    cfg: dict[str, Any] | None,
) -> PowerwallSnapshot:
    """Map a DeviceControllerQuery JSON + config.json into a PowerwallSnapshot.

    DCQ shape (see ``fleet_api_bms.DEVICE_CONTROLLER_QUERY``):
      control.meterAggregates: [{location: "site"|"battery"|"solar"|"load",
                                 realPowerW: float}, ...]
      control.systemStatus.{nominalFullPackEnergyWh, nominalEnergyRemainingWh}
      control.islanding.{customerIslandMode, contactorClosed, microGridOK,
                         gridOK, disableReasons}
      control.alerts.active: [str, ...]
      control.batteryBlocks: [{din, disableReasons}, ...]
      control.siteShutdown.{isShutDown, reasons}
    """
    control = dcq.get("control") or {}

    # Per-location power readings (watts).
    meters = {}
    for m in control.get("meterAggregates") or []:
        if not isinstance(m, dict) or not m.get("location"):
            continue
        meters[str(m["location"]).strip().lower()] = m

    def _watts(location: str) -> float | None:
        m = meters.get(location.strip().lower())
        if not isinstance(m, dict):
            return None
        return _float_or_none(m.get("realPowerW"))

    # SOC % from energy ratios. Either field missing → leave SOC None.
    # The Powerwall protects a 5% low-SOE reserve for cell health and won't
    # discharge past it. The Tesla app and Tesla cloud apps both report
    # operational SOC scaled across the usable 5–100% range; report the
    # same so PowerSync's reading matches the Tesla app exactly.
    sys_status = control.get("systemStatus") or {}
    full_wh = _float_or_none(sys_status.get("nominalFullPackEnergyWh"))
    rem_wh = _float_or_none(sys_status.get("nominalEnergyRemainingWh"))
    if full_wh and full_wh > 0 and rem_wh is not None:
        soc_pct = normalize_local_soc_percent((rem_wh / full_wh) * 100.0)
    else:
        soc_pct = None

    # Grid status: prefer customerIslandMode mapping, fall back to gridOK bool.
    islanding = control.get("islanding") or {}
    island_mode = islanding.get("customerIslandMode")
    grid_status = _DCQ_ISLAND_MODE_TO_GRID_STATUS.get(island_mode) if island_mode else None
    if grid_status is None:
        grid_ok = islanding.get("gridOK")
        if isinstance(grid_ok, bool):
            grid_status = "SystemGridConnected" if grid_ok else "SystemIslandedActive"

    # Operation mode + backup reserve from config.json (RSA-read, same
    # transport, fired in parallel).
    site_info = (cfg or {}).get("site_info") or {}
    # default_real_mode lives at the TOP LEVEL of config.json (confirmed by the
    # write path in __init__.py::handle_set_operation_mode, which writes and
    # reads it back undotted). Some gateways may also mirror it under
    # site_info, so keep that as a fallback.
    operation_mode = (cfg or {}).get("default_real_mode") or (
        site_info.get("default_real_mode") if isinstance(site_info, dict) else None
    )
    raw_backup_reserve_percent = _int_or_none(
        site_info.get("backup_reserve_percent") if isinstance(site_info, dict) else None
    )
    backup_reserve_percent = normalize_local_backup_reserve_percent(
        raw_backup_reserve_percent
    )
    disallow_grid_charging = (
        site_info.get("disallow_charge_from_grid_with_solar_installed")
        if isinstance(site_info, dict)
        else None
    )
    grid_charging_enabled = (
        not disallow_grid_charging
        if isinstance(disallow_grid_charging, bool)
        else None
    )
    grid_export_rule = (
        site_info.get("customer_preferred_export_rule")
        if isinstance(site_info, dict)
        else None
    )
    if grid_export_rule not in ("battery_ok", "pv_only", "never"):
        grid_export_rule = None

    # Alerts: DCQ flat list of names; coerce to dict shape consumers expect.
    alerts_active = control.get("alerts", {}).get("active") if isinstance(control.get("alerts"), dict) else None
    alerts: list[dict[str, Any]] | None = None
    if isinstance(alerts_active, list):
        alerts = [
            {"name": a} if isinstance(a, str) else a
            for a in alerts_active
            if isinstance(a, (str, dict))
        ]

    # Battery blocks — DCQ exposes per-pack DINs and disable reasons; SOC
    # data is via dedicated BMS path (`fetch_device_controller_json`) which
    # returns the richer cloud-relay payload. The local snapshot keeps the
    # raw block list for downstream consumers that just need a count.
    blocks_raw = control.get("batteryBlocks")
    battery_blocks = (
        [b for b in blocks_raw if isinstance(b, dict)]
        if isinstance(blocks_raw, list)
        else None
    )
    pw_count = len(battery_blocks) if battery_blocks else None

    # site_shutdown + system_island_state best-effort.
    site_shutdown = control.get("siteShutdown") or {}
    if site_shutdown.get("isShutDown"):
        system_island_state = "SystemIslandedActive"
    elif grid_status:
        system_island_state = grid_status
    else:
        system_island_state = None

    return PowerwallSnapshot(
        soc=soc_pct,
        solar_w=_watts("solar"),
        battery_w=_watts("battery"),
        grid_w=_watts("site"),
        load_w=_watts("load"),
        grid_status=grid_status,
        operation_mode=operation_mode,
        backup_reserve_percent=backup_reserve_percent,
        raw={"dcq": dcq, "config": cfg},
        grid_charging_enabled=grid_charging_enabled,
        grid_export_rule=grid_export_rule,
        system_island_state=system_island_state,
        pw_count=pw_count,
        total_pack_full_wh=full_wh,
        total_pack_remaining_wh=rem_wh,
        battery_blocks=battery_blocks,
        alerts=alerts,
    )
