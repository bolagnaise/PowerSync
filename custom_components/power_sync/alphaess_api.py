"""AlphaESS Cloud API client (openapi.alphaess.com).

Used as a telemetry fallback when Modbus TCP is unreachable, or as a
monitoring-only source for remote users. Modbus remains the only control path.

Auth:
  - App ID and App Secret issued from https://open.alphaess.com
  - Signature = SHA-512(AppID + AppSecret + Timestamp)
  - Headers: appId, timestamp, sign, Content-Type: application/json
  - Documented rate limit: 10 requests/min per endpoint
"""

import asyncio
import hashlib
import logging
import time
from typing import Optional

import aiohttp

from .const import ALPHAESS_CLOUD_BASE_URL

_LOGGER = logging.getLogger(__name__)

# 10 req/min per endpoint → min 6s between calls. Use 7s for headroom.
_MIN_REQUEST_INTERVAL = 7.0


class AlphaESSCloudError(Exception):
    """Raised when the AlphaESS Cloud API returns a non-success code."""


class AlphaESSCloudClient:
    """Async client for the AlphaESS Open API."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        serial: str = "",
        session: Optional[aiohttp.ClientSession] = None,
    ):
        """Initialize the client.

        Args:
            app_id: AppID from open.alphaess.com.
            app_secret: AppSecret from open.alphaess.com.
            serial: Inverter serial number (SYS_SN). May be left empty and
                resolved later via get_ess_list().
            session: Optional aiohttp session to reuse.
        """
        self.app_id = app_id
        self.app_secret = app_secret
        self.serial = serial
        self._session = session
        self._own_session = False
        self._last_request_time = 0.0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._own_session = True
        return self._session

    async def close(self) -> None:
        """Close the aiohttp session if we own it."""
        if self._own_session and self._session and not self._session.closed:
            await self._session.close()

    def _auth_headers(self) -> dict:
        """Build the AlphaESS signature headers.

        sign = SHA-512(AppID + AppSecret + Timestamp), lowercase hex.
        Timestamp is seconds since epoch (string).
        """
        timestamp = str(int(time.time()))
        sign_input = f"{self.app_id}{self.app_secret}{timestamp}".encode("utf-8")
        sign = hashlib.sha512(sign_input).hexdigest()
        return {
            "appId": self.app_id,
            "timestamp": timestamp,
            "sign": sign,
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
    ) -> dict:
        """Rate-limited authenticated request against the Open API.

        Returns the `data` field of the response on success.
        Raises AlphaESSCloudError on non-zero API error codes or HTTP failure.
        """
        elapsed = time.time() - self._last_request_time
        if elapsed < _MIN_REQUEST_INTERVAL:
            await asyncio.sleep(_MIN_REQUEST_INTERVAL - elapsed)

        url = f"{ALPHAESS_CLOUD_BASE_URL}{path}"
        headers = self._auth_headers()
        session = await self._get_session()
        self._last_request_time = time.time()

        kwargs = {
            "headers": headers,
            "timeout": aiohttp.ClientTimeout(total=30),
        }
        if params:
            kwargs["params"] = params
        if body is not None:
            kwargs["json"] = body

        async with session.request(method, url, **kwargs) as response:
            if response.status != 200:
                text = await response.text()
                raise AlphaESSCloudError(
                    f"AlphaESS API HTTP {response.status}: {text}"
                )

            data = await response.json()

            code = data.get("code", -1)
            if code != 200:
                msg = data.get("msg", "Unknown error")
                raise AlphaESSCloudError(f"AlphaESS API code {code}: {msg}")

            return data.get("data", data)

    # ---- Read endpoints ----

    async def get_ess_list(self) -> list[dict]:
        """List ESS systems on the account.

        Returns:
            List of dicts, each with at least `sysSn` and model info.
        """
        result = await self._request("GET", "/getEssList")
        if isinstance(result, list):
            return result
        # Some responses wrap the list
        if isinstance(result, dict) and "essList" in result:
            return result["essList"]
        return []

    async def get_last_power_data(self, sys_sn: str = "") -> dict:
        """Get the latest realtime snapshot for a system.

        Typical fields returned: ppv (PV W), pgrid (grid W, + import),
        pbat (battery W — AlphaESS sign conventions vary by endpoint;
        treat cautiously against Modbus as the source of truth), soc (%).
        """
        sn = sys_sn or self.serial
        if not sn:
            raise AlphaESSCloudError("No sysSn provided for get_last_power_data")
        return await self._request("GET", "/getLastPowerData", params={"sysSn": sn})

    async def get_one_date_energy(self, query_date: str, sys_sn: str = "") -> dict:
        """Get aggregated kWh totals for a given date (YYYY-MM-DD)."""
        sn = sys_sn or self.serial
        if not sn:
            raise AlphaESSCloudError("No sysSn provided for get_one_date_energy")
        return await self._request(
            "GET",
            "/getOneDateEnergyBySn",
            params={"sysSn": sn, "queryDate": query_date},
        )

    # ---- Write endpoints (schedule-based; no realtime dispatch) ----

    async def set_charge_config(
        self,
        grid_charge: bool,
        time_chae1: str,
        time_chae2: str,
        time_chaf1: str,
        time_chaf2: str,
        bat_high_cap: int,
        sys_sn: str = "",
    ) -> dict:
        """Update the battery charge configuration.

        AlphaCloud's charge scheduler uses two time windows (HH:mm).
        Passing empty strings disables a window.

        Args:
            grid_charge: Whether charging from grid is permitted.
            time_chae1, time_chae2: Charge window 1/2 END times (HH:mm).
            time_chaf1, time_chaf2: Charge window 1/2 START times (HH:mm).
            bat_high_cap: Upper SOC (%) at which charging stops.
            sys_sn: Override serial number.
        """
        sn = sys_sn or self.serial
        body = {
            "sysSn": sn,
            "gridCharge": 1 if grid_charge else 0,
            "timeChae1": time_chae1,
            "timeChae2": time_chae2,
            "timeChaf1": time_chaf1,
            "timeChaf2": time_chaf2,
            "batHighCap": bat_high_cap,
        }
        return await self._request("POST", "/updateChargeConfigInfo", body=body)

    async def set_discharge_config(
        self,
        ctr_dis: bool,
        time_dise1: str,
        time_dise2: str,
        time_disf1: str,
        time_disf2: str,
        bat_use_cap: int,
        sys_sn: str = "",
    ) -> dict:
        """Update the battery discharge configuration (mirror of charge).

        Args:
            ctr_dis: Whether scheduled discharge is enabled.
            time_dise1, time_dise2: Discharge window 1/2 END times (HH:mm).
            time_disf1, time_disf2: Discharge window 1/2 START times (HH:mm).
            bat_use_cap: Lower SOC (%) floor.
            sys_sn: Override serial number.
        """
        sn = sys_sn or self.serial
        body = {
            "sysSn": sn,
            "ctrDis": 1 if ctr_dis else 0,
            "timeDise1": time_dise1,
            "timeDise2": time_dise2,
            "timeDisf1": time_disf1,
            "timeDisf2": time_disf2,
            "batUseCap": bat_use_cap,
        }
        return await self._request("POST", "/updateDisChargeConfigInfo", body=body)

    # ---- Convenience / validation ----

    async def test_connection(self) -> tuple[bool, str]:
        """Validate credentials by listing systems.

        If `self.serial` is set, verifies it exists on the account.
        """
        try:
            systems = await self.get_ess_list()
            if not systems:
                return False, "Connected but no ESS systems found on the account"

            sns = [
                s.get("sysSn")
                for s in systems
                if isinstance(s, dict) and s.get("sysSn")
            ]
            if self.serial:
                if self.serial not in sns:
                    return False, (
                        f"Serial '{self.serial}' not found. "
                        f"Available: {', '.join(s for s in sns if s)}"
                    )
            elif len(sns) == 1:
                self.serial = sns[0]
            else:
                return False, (
                    "Enter the AlphaESS system serial number. "
                    f"Available: {', '.join(sns)}"
                )

            return True, f"Connected. Found {len(systems)} system(s)."

        except AlphaESSCloudError as e:
            return False, str(e)
        except Exception as e:
            return False, f"Unexpected error: {e}"
