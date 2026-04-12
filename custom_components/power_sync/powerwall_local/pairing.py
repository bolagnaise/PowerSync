"""RSA key pairing for Powerwall local control.

This module runs the "register a public key with Tesla so the gateway will
accept signed commands from us" handshake. The user must physically toggle
the gateway's DC isolator OFF then ON within a short window as Tesla's
physical-presence proof, after which Tesla Fleet API flips the key's state
from PENDING -> VERIFIED (state 3) and local v1r calls start working.

Flow overview:
    1. Generate RSA-4096 keypair (async via executor; ~1-3s on typical HA host).
    2. Call Tesla Fleet API ``add_authorized_client_request`` with the DER-
       encoded public key, using the already-present Fleet API token from
       the PowerSync config entry.
    3. Start polling ``list_authorized_clients_request`` for state transition
       to VERIFIED.
    4. Surface progress via a status object the mobile app polls.
    5. On success persist the PEM-encoded private key + DIN into the
       config entry ``data`` dict (HA handles encryption at rest).

The ``PowerwallPairingManager`` holds at most one pairing in progress per
config entry — the mobile app polls ``status()`` and can ``cancel()``.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import aiohttp
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from .exceptions import PowerwallPairingError

_LOGGER = logging.getLogger(__name__)

_DEFAULT_WINDOW_SECONDS = 120
_POLL_INTERVAL_SECONDS = 4
_KEY_STATE_VERIFIED = 3


class PairingState(str, Enum):
    """State machine for one pairing attempt."""

    IDLE = "idle"
    GENERATING_KEY = "generating_key"
    REGISTERING = "registering"
    WAITING_FOR_TOGGLE = "waiting_for_toggle"
    VERIFIED = "verified"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass
class PairingStatus:
    """Serializable snapshot of the current pairing attempt."""

    state: PairingState = PairingState.IDLE
    message: str = ""
    started_at: float | None = None
    expires_at: float | None = None
    din: str | None = None
    energy_site_id: int | None = None
    error: str | None = None
    key_state: int | None = None  # 1=PENDING 2=PENDING_VERIFICATION 3=VERIFIED

    def to_dict(self) -> dict[str, Any]:
        now = time.time()
        remaining = None
        if self.expires_at is not None:
            remaining = max(0, int(self.expires_at - now))
        return {
            "state": self.state.value,
            "message": self.message,
            "started_at": self.started_at,
            "expires_at": self.expires_at,
            "remaining_seconds": remaining,
            "din": self.din,
            "energy_site_id": self.energy_site_id,
            "error": self.error,
            "key_state": self.key_state,
        }


@dataclass
class PairingResult:
    """Output on successful pairing — persisted by the caller."""

    private_key_pem: bytes
    public_key_der: bytes
    din: str
    energy_site_id: int
    fleet_api_base: str = ""


@dataclass
class _Task:
    """Internal task record so cancel() can abort a running attempt."""

    task: asyncio.Task[Any]
    started_at: float = field(default_factory=time.time)


class PowerwallPairingManager:
    """Orchestrates one pairing attempt at a time for a given config entry.

    The manager is async-safe but not re-entrant: calling ``start`` while a
    pairing is running will raise ``PowerwallPairingError``. The mobile app
    should call ``cancel`` first if it needs to restart.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        fleet_api_base: str,
        fleet_api_token: str,
        *,
        energy_site_id: int | None = None,
        window_seconds: int = _DEFAULT_WINDOW_SECONDS,
        on_success: Any = None,
    ) -> None:
        self._session = session
        self._fleet_api_base = fleet_api_base.rstrip("/")
        self._token = fleet_api_token
        self._energy_site_id = energy_site_id
        self._window = window_seconds
        self._on_success = on_success

        self._status = PairingStatus()
        self._task_record: _Task | None = None
        self._lock = asyncio.Lock()
        self._result: PairingResult | None = None

    def status(self) -> PairingStatus:
        return self._status

    def result(self) -> PairingResult | None:
        return self._result

    @property
    def is_running(self) -> bool:
        return self._task_record is not None and not self._task_record.task.done()

    async def start(self) -> PairingStatus:
        """Kick off a pairing attempt. Returns the initial status immediately."""
        async with self._lock:
            if self.is_running:
                raise PowerwallPairingError("A pairing attempt is already running")
            self._status = PairingStatus(
                state=PairingState.GENERATING_KEY,
                message="Generating RSA key pair…",
                started_at=time.time(),
                expires_at=time.time() + self._window,
            )
            self._result = None
            task = asyncio.create_task(self._run(), name="powerwall_pair")
            self._task_record = _Task(task=task)
            return self._status

    async def cancel(self) -> PairingStatus:
        """Abort any in-flight pairing attempt."""
        if not self.is_running:
            return self._status
        assert self._task_record is not None
        self._task_record.task.cancel()
        try:
            await self._task_record.task
        except (asyncio.CancelledError, Exception):
            pass
        if self._status.state not in (
            PairingState.VERIFIED,
            PairingState.FAILED,
            PairingState.TIMEOUT,
        ):
            self._status.state = PairingState.CANCELLED
            self._status.message = "Pairing cancelled"
        return self._status

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            # Step 1: key generation on a worker thread — blocking CPU work.
            private_key, public_key_der = await loop.run_in_executor(
                None, _generate_rsa_4096
            )
            private_pem = private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )

            # Step 2: discover site + DIN. The caller may provide
            # energy_site_id from the existing config entry but the DIN
            # must always come from the products endpoint because it's the
            # full {part_number}--{serial_number} that the TEDAPI v1r
            # transport needs for TLV signature personalization. Using
            # just the serial (or an empty string) causes
            # MESSAGEFAULT_ERROR_WRONG_PERSONALIZATION at the gateway.
            energy_site_id = self._energy_site_id
            din: str | None = None
            fetched_site_id, fetched_din = await self._fetch_first_energy_site()
            if energy_site_id is None:
                energy_site_id = fetched_site_id
            if energy_site_id is None:
                self._fail("No energy site found on this Tesla account")
                return
            din = fetched_din
            self._status.energy_site_id = energy_site_id
            self._status.din = din

            # Step 3: POST add_authorized_client_request.
            self._status.state = PairingState.REGISTERING
            self._status.message = "Registering key with Tesla Fleet API…"
            register_resp = await self._register_key(
                energy_site_id, public_key_der
            )
            if register_resp is None:
                self._fail("Tesla Fleet API rejected the registration request")
                return

            # Tesla cloud sometimes reports state=VERIFIED immediately if the
            # user toggled the switch recently (grace window from a prior
            # pairing attempt or Netzero). But cloud-verified does NOT mean
            # the gateway itself has accepted the key — the gateway only
            # confirms after a physical toggle that happens AFTER our key
            # was registered. Trusting the cloud auto-verify led to a
            # "client authorization not verified" error on the first real
            # TEDAPI command. Always require the physical toggle regardless
            # of what the cloud says.
            state = _extract_client_state(register_resp)
            self._status.key_state = state

            # Step 4: ask the user to toggle the switch, poll for state change.
            self._status.state = PairingState.WAITING_FOR_TOGGLE
            self._status.message = (
                "Toggle your Powerwall DC isolator OFF then ON now. "
                "(PW2: right side of unit. PW3: left side, under cover flap.)"
            )
            verified = await self._poll_until_verified(energy_site_id)
            if verified:
                await self._complete(private_pem, public_key_der, din or "", energy_site_id)
                return

            if self._status.state != PairingState.CANCELLED:
                self._status.state = PairingState.TIMEOUT
                self._status.message = (
                    "Pairing window expired without confirmation. "
                    "Please try again and toggle the Powerwall switch within the window."
                )
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.exception("Pairing failed")
            self._fail(f"Pairing error: {err}")

    def _fail(self, reason: str) -> None:
        self._status.state = PairingState.FAILED
        self._status.message = reason
        self._status.error = reason

    async def _complete(
        self,
        private_pem: bytes,
        public_der: bytes,
        din: str,
        energy_site_id: int,
    ) -> None:
        self._status.state = PairingState.VERIFIED
        self._status.message = "Paired — local control enabled"
        self._status.key_state = _KEY_STATE_VERIFIED
        self._result = PairingResult(
            private_key_pem=private_pem,
            public_key_der=public_der,
            din=din,
            energy_site_id=energy_site_id,
            fleet_api_base=self._fleet_api_base,
        )
        if self._on_success is not None:
            try:
                await self._on_success(self._result)
            except Exception as err:
                _LOGGER.error("on_success callback failed: %s", err)

    async def _fetch_first_energy_site(self) -> tuple[int | None, str | None]:
        """List products on the Tesla account and return the first energy site."""
        url = f"{self._fleet_api_base}/api/1/products"
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            async with self._session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return None, None
                data = await resp.json()
        except aiohttp.ClientError as err:
            raise PowerwallPairingError(f"Tesla products lookup failed: {err}") from err

        for product in data.get("response", []) or []:
            if "energy_site_id" in product:
                return (
                    int(product["energy_site_id"]),
                    product.get("gateway_id") or product.get("gateway_din"),
                )
        return None, None

    async def _register_key(
        self, energy_site_id: int, public_key_der: bytes
    ) -> dict[str, Any] | None:
        """Send the add_authorized_client_request grpc command."""
        b64 = base64.b64encode(public_key_der).decode()
        payload = {
            "command_properties": {
                "message": {
                    "authorization": {
                        "add_authorized_client_request": {
                            "key_type": 1,
                            "public_key": b64,
                            "authorized_client_type": 1,
                            "description": "PowerSync Local Client",
                        }
                    }
                },
                "identifier_type": 1,
            },
            "command_type": "grpc_command",
        }
        url = (
            f"{self._fleet_api_base}/api/1/energy_sites/"
            f"{energy_site_id}/command"
        )
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        try:
            async with self._session.post(
                url, json=payload, headers=headers
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    _LOGGER.warning(
                        "Fleet API register failed (%s): %s", resp.status, text[:300]
                    )
                    return None
                return await resp.json()
        except aiohttp.ClientError as err:
            raise PowerwallPairingError(f"Fleet API register error: {err}") from err

    async def _poll_until_verified(self, energy_site_id: int) -> bool:
        """Poll list_authorized_clients until state=VERIFIED or we run out of time."""
        url = (
            f"{self._fleet_api_base}/api/1/energy_sites/"
            f"{energy_site_id}/command"
        )
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        payload = {
            "command_properties": {
                "message": {
                    "authorization": {"list_authorized_clients_request": {}}
                },
                "identifier_type": 1,
            },
            "command_type": "grpc_command",
        }
        while True:
            if (
                self._status.expires_at is not None
                and time.time() > self._status.expires_at
            ):
                return False
            try:
                async with self._session.post(
                    url, json=payload, headers=headers
                ) as resp:
                    if resp.status != 200:
                        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                        continue
                    data = await resp.json()
            except aiohttp.ClientError as err:
                _LOGGER.debug("Fleet API poll error: %s", err)
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                continue

            state = _extract_client_state(data)
            if state is not None:
                self._status.key_state = state
            if state == _KEY_STATE_VERIFIED:
                return True
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)


def _generate_rsa_4096() -> tuple[rsa.RSAPrivateKey, bytes]:
    """Blocking RSA-4096 keygen — caller must run in executor."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    public_key_der = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.PKCS1,
    )
    return private_key, public_key_der


def _extract_client_state(resp: dict[str, Any] | None) -> int | None:
    """Pull the client state field out of Fleet API's layered grpc_command response.

    Tesla returns varying key-case layouts across firmware versions, so we
    check both camelCase and snake_case. Returns None if the field isn't
    present — the poller treats that as "keep waiting".
    """
    if not isinstance(resp, dict):
        return None
    try:
        msg = resp["response"]["message"]["Payload"]["Authorization"]["Message"]
    except (KeyError, TypeError):
        try:
            msg = resp["response"]["message"]["payload"]["authorization"]["message"]
        except (KeyError, TypeError):
            return None

    for key in ("AddAuthorizedClientResponse", "add_authorized_client_response"):
        if key in msg:
            client = msg[key].get("client") or msg[key].get("Client")
            if client:
                state = client.get("state") or client.get("State")
                if state is not None:
                    try:
                        return int(state)
                    except (TypeError, ValueError):
                        return None

    for key in ("ListAuthorizedClientsResponse", "list_authorized_clients_response"):
        if key in msg:
            clients = (
                msg[key].get("clients")
                or msg[key].get("Clients")
                or []
            )
            # Most recently added is usually last — walk in reverse for speed.
            for client in reversed(clients):
                state = client.get("state") or client.get("State")
                if state is not None:
                    try:
                        return int(state)
                    except (TypeError, ValueError):
                        continue
    return None
