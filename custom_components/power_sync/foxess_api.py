"""FoxESS Cloud API client for Home Assistant PowerSync integration.

Handles connection testing and schedule synchronization with FoxESS inverters
via the FoxESS Open API (https://www.foxesscloud.com).

Auth uses signature-based headers:
  - token: API key
  - timestamp: milliseconds since epoch
  - signature: MD5(path\\r\\n + api_key\\r\\n + timestamp)
  - lang: "en"
"""

import hashlib
import logging
import time
from typing import Optional

import aiohttp

from .const import (
    FOXESS_CLOUD_BASE_URL,
    FOXESS_MAX_SCHEDULE_PERIODS,
)

_LOGGER = logging.getLogger(__name__)

# FoxESS Open API rate limit: 1440 calls/day, min 1s between queries
_MIN_REQUEST_INTERVAL = 1.0


class FoxESSCloudClient:
    """Async client for FoxESS Open API."""

    def __init__(
        self,
        api_key: str,
        device_sn: str = "",
        session: Optional[aiohttp.ClientSession] = None,
    ):
        """Initialize FoxESS Cloud client.

        Args:
            api_key: API key from foxesscloud.com/user/center > API Management
            device_sn: Inverter serial number
            session: Optional aiohttp session to reuse
        """
        self.api_key = api_key
        self.device_sn = device_sn
        self._session = session
        self._own_session = False
        self._last_request_time = 0.0

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

    def _generate_signature(self, path: str) -> dict[str, str]:
        """Generate FoxESS Open API auth headers.

        Signature = MD5(path\\r\\n + api_key\\r\\n + timestamp)

        Args:
            path: API path (e.g., /op/v0/device/list)

        Returns:
            Dict of auth headers: token, timestamp, signature, lang
        """
        timestamp = str(int(time.time() * 1000))
        sign_text = f"{path}\r\n{self.api_key}\r\n{timestamp}"
        signature = hashlib.md5(sign_text.encode("utf-8")).hexdigest()
        return {
            "token": self.api_key,
            "timestamp": timestamp,
            "signature": signature,
            "lang": "en",
            "Content-Type": "application/json",
        }

    async def _post(self, path: str, payload: dict) -> dict:
        """Make authenticated POST request to FoxESS API.

        Handles rate limiting (1 req/sec) and error checking.

        Args:
            path: API path (e.g., /op/v0/device/list)
            payload: JSON request body

        Returns:
            Response dict with 'result' key on success

        Raises:
            Exception on HTTP or API errors
        """
        # Rate limiting — wait if needed
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < _MIN_REQUEST_INTERVAL:
            import asyncio
            await asyncio.sleep(_MIN_REQUEST_INTERVAL - elapsed)

        url = f"{FOXESS_CLOUD_BASE_URL}{path}"
        headers = self._generate_signature(path)

        session = await self._get_session()
        self._last_request_time = time.time()

        async with session.post(
            url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=30)
        ) as response:
            if response.status != 200:
                text = await response.text()
                raise Exception(f"FoxESS API HTTP {response.status}: {text}")

            data = await response.json()

            # FoxESS API uses errno: 0 = success
            errno = data.get("errno", -1)
            if errno != 0:
                msg = data.get("msg", "Unknown error")
                raise Exception(f"FoxESS API error {errno}: {msg}")

            return data.get("result", data)

    async def get_device_list(self) -> list[dict]:
        """Get list of devices for this account.

        Returns:
            List of device dicts with deviceSN, stationName, etc.
        """
        path = "/op/v0/device/list"
        payload = {"currentPage": 1, "pageSize": 10}
        result = await self._post(path, payload)
        devices = result.get("devices", []) if isinstance(result, dict) else []
        _LOGGER.info("FoxESS Cloud: found %d device(s)", len(devices))
        return devices

    async def get_real_data(self, sn: str = "") -> dict:
        """Get real-time data for a device.

        Args:
            sn: Device serial number (uses self.device_sn if empty)

        Returns:
            Dict with real-time device data
        """
        path = "/op/v0/device/real/query"
        device_sn = sn or self.device_sn
        payload = {
            "sn": device_sn,
            "variables": ["pvPower", "gridConsumptionPower", "loadsPower", "batPower", "SoC"],
        }
        return await self._post(path, payload)

    async def set_scheduler(self, sn: str, groups: list[dict]) -> dict:
        """Set time-of-use scheduler on the inverter.

        FoxESS supports up to 8 schedule groups. Each group defines a time window
        with work mode and battery charge/discharge settings.

        Args:
            sn: Device serial number
            groups: List of scheduler group dicts (max 8)

        Returns:
            API response dict
        """
        if len(groups) > FOXESS_MAX_SCHEDULE_PERIODS:
            _LOGGER.warning(
                "FoxESS scheduler limited to %d periods, truncating %d groups",
                FOXESS_MAX_SCHEDULE_PERIODS,
                len(groups),
            )
            groups = groups[:FOXESS_MAX_SCHEDULE_PERIODS]

        path = "/op/v0/device/scheduler/set"
        payload = {"sn": sn, "groups": groups}
        _LOGGER.info("FoxESS Cloud: setting scheduler with %d groups for %s", len(groups), sn)
        return await self._post(path, payload)

    async def test_connection(self) -> tuple[bool, str]:
        """Test the connection to FoxESS Cloud API.

        Validates the API key by calling get_device_list.

        Returns:
            Tuple of (success: bool, message: str)
        """
        try:
            devices = await self.get_device_list()

            # If device_sn is specified, check it exists
            if self.device_sn:
                found = any(d.get("deviceSN") == self.device_sn for d in devices)
                if not found:
                    device_sns = [d.get("deviceSN", "?") for d in devices]
                    return False, (
                        f"Device '{self.device_sn}' not found. "
                        f"Available devices: {', '.join(device_sns)}"
                    )

            return True, f"Connected successfully. Found {len(devices)} device(s)."

        except Exception as e:
            return False, str(e)


def convert_prices_to_foxess_schedule(
    buy_prices: list[dict],
    sell_prices: list[dict],
    charge_threshold_cents: float = 5.0,
    export_threshold_cents: float = 15.0,
    min_soc: int = 10,
    charge_soc: int = 100,
) -> list[dict]:
    """Convert Amber/Octopus price data to FoxESS scheduler groups.

    Takes 48 half-hour price slots and classifies each into a work mode,
    then merges adjacent slots with the same mode. Consolidates to max 8 groups.

    Args:
        buy_prices: List of {timeRange: "HH:MM-HH:MM", price: float} (import prices in cents)
        sell_prices: List of {timeRange: "HH:MM-HH:MM", price: float} (export prices in cents)
        charge_threshold_cents: Buy below this → CHARGE from grid
        export_threshold_cents: Sell above this → EXPORT (force discharge)
        min_soc: Minimum SOC on grid (%) for self-use periods
        charge_soc: Target SOC (%) for charge periods

    Returns:
        List of FoxESS scheduler group dicts ready for set_scheduler API
    """
    # Build a lookup of sell prices by time slot
    sell_lookup = {}
    for slot in sell_prices:
        tr = slot.get("timeRange", "")
        start = tr.split("-")[0] if "-" in tr else ""
        if start:
            sell_lookup[start] = slot.get("price", 0)

    # Classify each 30-min slot
    # Modes: "SelfUse" (1), "ForceCharge" (charge from grid), "ForceDischarge" (export)
    classified = []
    for slot in buy_prices:
        tr = slot.get("timeRange", "")
        parts = tr.split("-") if "-" in tr else ["", ""]
        start_str = parts[0]
        end_str = parts[1] if len(parts) > 1 else ""
        buy_price = slot.get("price", 0)
        sell_price = sell_lookup.get(start_str, 0)

        if buy_price <= charge_threshold_cents:
            mode = "ForceCharge"
        elif sell_price >= export_threshold_cents:
            mode = "ForceDischarge"
        else:
            mode = "SelfUse"

        classified.append({
            "start": start_str,
            "end": end_str,
            "mode": mode,
            "buy": buy_price,
            "sell": sell_price,
        })

    if not classified:
        _LOGGER.warning("No price slots to convert to FoxESS schedule")
        return []

    # Merge adjacent slots with same mode
    merged = []
    current = classified[0].copy()
    for slot in classified[1:]:
        if slot["mode"] == current["mode"]:
            current["end"] = slot["end"]
        else:
            merged.append(current)
            current = slot.copy()
    merged.append(current)

    # Consolidate to max FOXESS_MAX_SCHEDULE_PERIODS groups if needed
    while len(merged) > FOXESS_MAX_SCHEDULE_PERIODS:
        # Find the shortest group and merge it with an adjacent one
        min_idx = -1
        min_duration = float("inf")
        for i, group in enumerate(merged):
            try:
                sh, sm = map(int, group["start"].split(":"))
                eh, em = map(int, group["end"].split(":"))
                duration = (eh * 60 + em) - (sh * 60 + sm)
                if duration <= 0:
                    duration += 1440  # Handle midnight wrap
                if duration < min_duration:
                    min_duration = duration
                    min_idx = i
            except (ValueError, AttributeError):
                continue

        if min_idx < 0:
            break

        # Merge the shortest group with whichever neighbour has the same mode,
        # or the previous one if neither matches
        if min_idx > 0 and merged[min_idx - 1]["mode"] == merged[min_idx]["mode"]:
            merged[min_idx - 1]["end"] = merged[min_idx]["end"]
            merged.pop(min_idx)
        elif min_idx < len(merged) - 1 and merged[min_idx + 1]["mode"] == merged[min_idx]["mode"]:
            merged[min_idx]["end"] = merged[min_idx + 1]["end"]
            merged.pop(min_idx + 1)
        else:
            # No same-mode neighbour — absorb into previous group's mode
            if min_idx > 0:
                merged[min_idx - 1]["end"] = merged[min_idx]["end"]
                merged.pop(min_idx)
            else:
                merged[min_idx]["end"] = merged[min_idx + 1]["end"]
                merged[min_idx]["mode"] = merged[min_idx + 1]["mode"]
                merged.pop(min_idx + 1)

    # Convert to FoxESS scheduler group format
    groups = []
    for i, group in enumerate(merged):
        try:
            sh, sm = map(int, group["start"].split(":"))
            eh, em = map(int, group["end"].split(":"))
        except (ValueError, AttributeError):
            continue

        # Handle "24:00" end time → 23:59
        if eh >= 24:
            eh, em = 23, 59

        mode = group["mode"]

        # Map mode to FoxESS workMode values
        # workMode: "SelfUse" | "ForceCharge" | "ForceDischarge" | "Backup" | "FeedIn"
        foxess_group = {
            "enable": True,
            "startHour": sh,
            "startMinute": sm,
            "endHour": eh,
            "endMinute": em,
            "workMode": mode,
            "minSocOnGrid": min_soc,
            "fdSoc": charge_soc if mode == "ForceCharge" else min_soc,
            "fdPwr": 0,  # 0 = use inverter max
            "maxSoc": charge_soc if mode == "ForceCharge" else 100,
        }

        groups.append(foxess_group)
        _LOGGER.debug(
            "FoxESS schedule group %d: %02d:%02d-%02d:%02d mode=%s",
            i, sh, sm, eh, em, mode,
        )

    _LOGGER.info(
        "FoxESS schedule: %d groups (%d self-use, %d charge, %d discharge)",
        len(groups),
        sum(1 for g in groups if g["workMode"] == "SelfUse"),
        sum(1 for g in groups if g["workMode"] == "ForceCharge"),
        sum(1 for g in groups if g["workMode"] == "ForceDischarge"),
    )

    return groups
