"""Zaptec Cloud API client for Home Assistant PowerSync integration.

Standalone async client for controlling Zaptec EV chargers directly via
the Zaptec Cloud API (api.zaptec.com), without requiring the external
custom-components/zaptec HA integration.

Auth uses OAuth2 ROPC (Resource Owner Password Credentials):
  - POST /oauth/token with grant_type=password, username, password
  - Token expires in 3600s, no refresh token — must re-authenticate
"""

import asyncio
import logging
import time
from typing import Any, Optional

import aiohttp

_LOGGER = logging.getLogger(__name__)

ZAPTEC_API_BASE_URL = "https://api.zaptec.com"
ZAPTEC_TOKEN_URL = f"{ZAPTEC_API_BASE_URL}/oauth/token"

# Rate limits
_MIN_REQUEST_INTERVAL = 1.0  # 10 req/sec general, but be conservative
_MIN_CURRENT_UPDATE_INTERVAL = 900.0  # 15 minutes for installation current updates

# Zaptec charger commands
ZAPTEC_CMD_RESUME_CHARGING = 507
ZAPTEC_CMD_STOP_CHARGING = 506


class ZaptecCloudClient:
    """Async client for Zaptec Cloud API."""

    def __init__(
        self,
        username: str,
        password: str,
        session: Optional[aiohttp.ClientSession] = None,
    ):
        """Initialize Zaptec Cloud client.

        Args:
            username: Zaptec account email
            password: Zaptec account password
            session: Optional aiohttp session to reuse
        """
        self.username = username
        self.password = password
        self._session = session
        self._own_session = False
        self._last_request_time = 0.0
        self._last_current_update_time = 0.0

        # Token state
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._own_session = True
        return self._session

    async def close(self):
        """Close the session if we own it."""
        if self._own_session and self._session and not self._session.closed:
            await self._session.close()

    async def authenticate(self) -> None:
        """Authenticate with Zaptec Cloud API using OAuth2 ROPC.

        Raises:
            Exception: On authentication failure
        """
        session = await self._get_session()

        data = {
            "grant_type": "password",
            "username": self.username,
            "password": self.password,
            "scope": "openid",
        }

        async with session.post(
            ZAPTEC_TOKEN_URL,
            data=data,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if response.status != 200:
                text = await response.text()
                raise Exception(f"Zaptec auth failed (HTTP {response.status}): {text}")

            result = await response.json()
            self._access_token = result.get("access_token")
            expires_in = result.get("expires_in", 3600)
            # Expire 60s early to avoid edge cases
            self._token_expires_at = time.time() + expires_in - 60

            if not self._access_token:
                raise Exception("Zaptec auth: no access_token in response")

            _LOGGER.info("Zaptec Cloud: authenticated successfully")

    def _token_valid(self) -> bool:
        """Check if the current access token is still valid."""
        return bool(self._access_token and time.time() < self._token_expires_at)

    async def _ensure_authenticated(self) -> None:
        """Ensure we have a valid access token."""
        if not self._token_valid():
            await self.authenticate()

    async def _request(
        self,
        method: str,
        path: str,
        json_data: dict | None = None,
        retry_on_401: bool = True,
    ) -> Any:
        """Make an authenticated request to Zaptec API.

        Args:
            method: HTTP method (GET, POST, etc.)
            path: API path (e.g., /api/chargers)
            json_data: Optional JSON body
            retry_on_401: Whether to re-auth and retry on 401

        Returns:
            Parsed JSON response

        Raises:
            Exception: On HTTP or API errors
        """
        # Rate limiting
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < _MIN_REQUEST_INTERVAL:
            await asyncio.sleep(_MIN_REQUEST_INTERVAL - elapsed)

        await self._ensure_authenticated()

        url = f"{ZAPTEC_API_BASE_URL}{path}"
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

        session = await self._get_session()
        self._last_request_time = time.time()

        kwargs: dict[str, Any] = {
            "headers": headers,
            "timeout": aiohttp.ClientTimeout(total=30),
        }
        if json_data is not None:
            kwargs["json"] = json_data

        async with session.request(method, url, **kwargs) as response:
            # Handle 401 — re-authenticate and retry once
            if response.status == 401 and retry_on_401:
                _LOGGER.debug("Zaptec API: 401 received, re-authenticating")
                self._access_token = None
                await self.authenticate()
                return await self._request(
                    method, path, json_data, retry_on_401=False
                )

            # Handle 429 — respect Retry-After
            if response.status == 429:
                retry_after = response.headers.get("Retry-After", "60")
                try:
                    wait = min(300, max(1, int(retry_after)))
                except ValueError:
                    wait = 60
                _LOGGER.warning(
                    "Zaptec API: rate limited, waiting %ds", wait
                )
                await asyncio.sleep(wait)
                return await self._request(
                    method, path, json_data, retry_on_401=False
                )

            if response.status == 204:
                return {}

            if response.status not in (200, 201, 202):
                text = await response.text()
                raise Exception(f"Zaptec API HTTP {response.status}: {text}")

            # Some endpoints return empty body on success
            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                return await response.json()
            return {}

    async def get_installations(self) -> list[dict]:
        """Get list of installations for this account.

        Returns:
            List of installation dicts with Id, Name, etc.
        """
        result = await self._request("GET", "/api/installation")
        # API returns {Pages: [...], PageCount: N} or {Data: [...]}
        if isinstance(result, dict):
            data = result.get("Data", result.get("Pages", []))
            if isinstance(data, list):
                _LOGGER.info(
                    "Zaptec Cloud: found %d installation(s)", len(data)
                )
                return data
        if isinstance(result, list):
            return result
        return []

    async def get_chargers(self) -> list[dict]:
        """Get list of chargers for this account.

        Returns:
            List of charger dicts with Id, DeviceName, InstallationId, etc.
        """
        result = await self._request("GET", "/api/chargers")
        if isinstance(result, dict):
            data = result.get("Data", result.get("Pages", []))
            if isinstance(data, list):
                _LOGGER.info("Zaptec Cloud: found %d charger(s)", len(data))
                return data
        if isinstance(result, list):
            return result
        return []

    async def get_charger_state(self, charger_id: str) -> dict:
        """Get current state of a specific charger.

        Args:
            charger_id: Charger UUID

        Returns:
            Dict of state observations with StateId, ValueAsString, etc.
        """
        result = await self._request(
            "GET", f"/api/chargers/{charger_id}/state"
        )
        # Parse state observations into a flat dict
        state = {}
        if isinstance(result, list):
            for obs in result:
                state_id = obs.get("StateId")
                value = obs.get("ValueAsString", "")
                if state_id is not None:
                    state[state_id] = value
        elif isinstance(result, dict):
            # Could be wrapped in Data
            observations = result.get("Data", result.get("StateValues", []))
            if isinstance(observations, list):
                for obs in observations:
                    state_id = obs.get("StateId")
                    value = obs.get("ValueAsString", "")
                    if state_id is not None:
                        state[state_id] = value
            else:
                state = result
        return state

    async def resume_charging(self, charger_id: str) -> dict:
        """Resume/start charging on a charger.

        Args:
            charger_id: Charger UUID

        Returns:
            API response
        """
        _LOGGER.info("Zaptec Cloud: resuming charging on %s", charger_id)
        return await self._request(
            "POST",
            f"/api/chargers/{charger_id}/SendCommand/{ZAPTEC_CMD_RESUME_CHARGING}",
        )

    async def stop_charging(self, charger_id: str) -> dict:
        """Stop/pause charging on a charger.

        Args:
            charger_id: Charger UUID

        Returns:
            API response
        """
        _LOGGER.info("Zaptec Cloud: stopping charging on %s", charger_id)
        return await self._request(
            "POST",
            f"/api/chargers/{charger_id}/SendCommand/{ZAPTEC_CMD_STOP_CHARGING}",
        )

    async def set_installation_current(
        self, installation_id: str, amps: int
    ) -> dict:
        """Set available current for all phases on an installation.

        Note: Zaptec limits installation current updates to once per 15 minutes.

        Args:
            installation_id: Installation UUID
            amps: Available current in amps (applied to all 3 phases)

        Returns:
            API response

        Raises:
            Exception: If called too frequently (within 15 min window)
        """
        return await self.set_installation_current_phases(
            installation_id, amps, amps, amps
        )

    async def set_installation_current_phases(
        self,
        installation_id: str,
        phase1_amps: int,
        phase2_amps: int,
        phase3_amps: int,
    ) -> dict:
        """Set available current per phase on an installation.

        Note: Zaptec limits installation current updates to once per 15 minutes.

        Args:
            installation_id: Installation UUID
            phase1_amps: Phase 1 available current
            phase2_amps: Phase 2 available current
            phase3_amps: Phase 3 available current

        Returns:
            API response

        Raises:
            Exception: If called too frequently (within 15 min window)
        """
        now = time.time()
        elapsed = now - self._last_current_update_time
        if elapsed < _MIN_CURRENT_UPDATE_INTERVAL:
            remaining = int(_MIN_CURRENT_UPDATE_INTERVAL - elapsed)
            raise Exception(
                f"Zaptec installation current rate limit: "
                f"wait {remaining}s (15 min minimum between updates)"
            )

        payload = {
            "AvailableCurrentPhase1": phase1_amps,
            "AvailableCurrentPhase2": phase2_amps,
            "AvailableCurrentPhase3": phase3_amps,
        }

        _LOGGER.info(
            "Zaptec Cloud: setting installation %s current to %d/%d/%dA",
            installation_id,
            phase1_amps,
            phase2_amps,
            phase3_amps,
        )

        result = await self._request(
            "POST",
            f"/api/installation/{installation_id}/update",
            json_data=payload,
        )
        self._last_current_update_time = time.time()
        return result

    async def test_connection(self) -> tuple[bool, str]:
        """Test the connection to Zaptec Cloud API.

        Validates credentials by authenticating and listing chargers.

        Returns:
            Tuple of (success: bool, message: str)
        """
        try:
            await self.authenticate()
            chargers = await self.get_chargers()
            installations = await self.get_installations()

            charger_names = [
                c.get("DeviceName", c.get("Id", "?")) for c in chargers
            ]
            install_names = [
                i.get("Name", i.get("Id", "?")) for i in installations
            ]

            return True, (
                f"Connected. Found {len(chargers)} charger(s) "
                f"({', '.join(charger_names)}) and "
                f"{len(installations)} installation(s) "
                f"({', '.join(install_names)})."
            )

        except Exception as e:
            return False, str(e)

    def parse_charger_state(self, state: dict) -> dict:
        """Parse raw charger state observations into human-readable dict.

        Zaptec state IDs:
          120 = ChargerOperationMode (1=Disconnected, 2=Connected/Waiting, 3=Charging, 5=Error)
          507 = IsCharging
          513 = TotalChargePower (W)
          514 = TotalChargePowerSession (Wh)
          520 = PhaseA current
          521 = PhaseB current
          522 = PhaseC current
          710 = ChargerFirmwareVersion
          908 = CompletedSession (JSON)

        Args:
            state: Dict of StateId -> ValueAsString from get_charger_state

        Returns:
            Dict with parsed fields
        """
        parsed = {}

        # Operation mode
        op_mode_raw = state.get(120, state.get("120", ""))
        op_modes = {
            "0": "charging_paused",
            "1": "disconnected",
            "2": "connected_waiting",
            "3": "charging",
            "5": "error",
        }
        parsed["charger_operation_mode"] = op_modes.get(
            str(op_mode_raw), f"unknown_{op_mode_raw}"
        )

        # Power
        try:
            parsed["total_charge_power_w"] = float(
                state.get(513, state.get("513", 0))
            )
        except (ValueError, TypeError):
            parsed["total_charge_power_w"] = 0.0

        # Session energy
        try:
            parsed["session_energy_wh"] = float(
                state.get(514, state.get("514", 0))
            )
        except (ValueError, TypeError):
            parsed["session_energy_wh"] = 0.0

        # Phase currents
        for phase, sid in [("phase_a_current", 520), ("phase_b_current", 521), ("phase_c_current", 522)]:
            try:
                parsed[phase] = float(state.get(sid, state.get(str(sid), 0)))
            except (ValueError, TypeError):
                parsed[phase] = 0.0

        # Firmware
        parsed["firmware_version"] = state.get(710, state.get("710", ""))

        return parsed
