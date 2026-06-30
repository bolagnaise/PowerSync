"""FoxESS Cloud API client for Home Assistant PowerSync integration.

Handles connection testing and schedule synchronization with FoxESS inverters
via the FoxESS Open API (https://www.foxesscloud.com).

Auth uses signature-based headers:
  - token: API key
  - timestamp: milliseconds since epoch
  - signature: MD5(path + literal "\\r\\n" + api_key + literal "\\r\\n" + timestamp)
  - lang: "en"
"""

import asyncio
import hashlib
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import aiohttp

from .const import (
    FOXESS_CLOUD_BASE_URL,
    FOXESS_MAX_SCHEDULE_PERIODS,
)

_LOGGER = logging.getLogger(__name__)

# FoxESS Open API rate limit: 1440 calls/day, min 1s between queries
_MIN_REQUEST_INTERVAL = 1.0
_MIN_WRITE_INTERVAL = 2.0

# FoxESS marks success with errno or code; values vary by endpoint/region.
_FOXESS_SUCCESS_CODES = (0, 200, "0", "200")


class FoxESSApiError(Exception):
    """FoxESS Open API error carrying the numeric/string error code."""

    def __init__(self, message: str, code: object = None) -> None:
        super().__init__(message)
        self.code = code


def _is_scheduler_blocks_work_mode(error: Exception) -> bool:
    """Detect FoxESS 44096 (WorkMode write blocked by an active schedule)."""
    if isinstance(error, FoxESSApiError) and str(error.code) == "44096":
        return True
    message = str(error).lower()
    return "44096" in message or "unsupported function code" in message


def _is_setting_unsupported(error: Exception) -> bool:
    """Detect FoxESS errors meaning a device setting key is not supported."""
    code = str(getattr(error, "code", "") or "")
    if code in ("40257", "42015"):
        return True
    message = str(error)
    return (
        "40257" in message
        or "42015" in message
        or "does not currently support" in message
        or "Parameters do not meet expectations" in message
    )


def _extract_device_sn(device: dict) -> str:
    """Return a FoxESS serial number from known Open API response keys."""
    return str(
        device.get("deviceSN")
        or device.get("sn")
        or device.get("deviceSn")
        or device.get("serialNumber")
        or ""
    ).strip()


def _parse_time_range(start: datetime, end: datetime) -> tuple[int, int, int, int]:
    """Convert datetimes to FoxESS scheduler hour/minute fields."""
    if end <= start:
        end = start + timedelta(minutes=1)
    return start.hour, start.minute, end.hour, end.minute


def _schedule_extra_params(
    *,
    min_soc: float = 10,
    fd_soc: float = 100,
    fd_pwr: float = 0,
    max_soc: float = 100,
    import_limit: Optional[float] = None,
    export_limit: Optional[float] = None,
    pv_limit: Optional[float] = None,
    reactive_power: Optional[float] = None,
) -> dict:
    """Return Scheduler V3 extraParam without overwriting omitted device limits."""
    params = {
        "minSocOnGrid": float(min_soc),
        "fdSoc": float(fd_soc),
        "fdPwr": float(fd_pwr),
        "maxSoc": float(max_soc),
    }
    if import_limit is not None:
        params["importLimit"] = float(import_limit)
    if export_limit is not None:
        params["exportLimit"] = float(export_limit)
    if pv_limit is not None:
        params["pvLimit"] = float(pv_limit)
    if reactive_power is not None:
        params["reactivePower"] = float(reactive_power)
    return params


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

        Signature = MD5(path + literal "\\r\\n" + api_key + literal "\\r\\n" + timestamp)

        Args:
            path: API path (e.g., /op/v0/device/list)

        Returns:
            Dict of auth headers: token, timestamp, signature, lang
        """
        timestamp = str(int(time.time() * 1000))
        sign_text = f"{path}\\r\\n{self.api_key}\\r\\n{timestamp}"
        signature = hashlib.md5(sign_text.encode("utf-8")).hexdigest()
        return {
            "token": self.api_key,
            "timestamp": timestamp,
            "signature": signature,
            "lang": "en",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        params: dict | None = None,
        write: bool = False,
    ) -> dict:
        """Make authenticated request to FoxESS API.

        Handles rate limiting (1 req/sec) and error checking.

        Args:
            path: API path (e.g., /op/v0/device/list)
            payload: JSON request body for POST requests
            params: Query parameters for GET requests
            write: Apply the write endpoint rate limit

        Returns:
            Response dict with 'result' key on success

        Raises:
            Exception on HTTP or API errors
        """
        # Rate limiting — wait if needed
        now = time.time()
        elapsed = now - self._last_request_time
        min_interval = _MIN_WRITE_INTERVAL if write else _MIN_REQUEST_INTERVAL
        if elapsed < min_interval:
            await asyncio.sleep(min_interval - elapsed)

        url = f"{FOXESS_CLOUD_BASE_URL}{path}"
        headers = self._generate_signature(path)
        headers["User-Agent"] = "PowerSync Home Assistant"

        session = await self._get_session()
        self._last_request_time = time.time()

        request_method = method.upper()
        kwargs = {
            "headers": headers,
            "timeout": aiohttp.ClientTimeout(total=30),
        }
        if request_method == "GET":
            kwargs["params"] = params or payload or {}
        else:
            kwargs["json"] = payload or {}

        async with session.request(request_method, url, **kwargs) as response:
            if response.status != 200:
                text = await response.text()
                raise FoxESSApiError(
                    f"FoxESS API HTTP {response.status}: {text}",
                    f"http_{response.status}",
                )

            data = await response.json()

            # FoxESS marks success via errno or code; accept 0/200 (and string forms).
            errno = data.get("errno", data.get("code", 0))
            if errno not in _FOXESS_SUCCESS_CODES:
                msg = data.get("msg", data.get("message", "Unknown error"))
                raise FoxESSApiError(f"FoxESS API error {errno}: {msg}", errno)

            for key in ("result", "data"):
                if data.get(key) is not None:
                    return data[key]
            return data

    async def _post(self, path: str, payload: dict, *, write: bool = False) -> dict:
        """Make authenticated POST request to FoxESS API."""
        return await self._request("POST", path, payload, write=write)

    async def _get(self, path: str, params: dict) -> dict:
        """Make authenticated GET request to FoxESS API."""
        return await self._request("GET", path, params=params)

    async def get_device_list(self) -> list[dict]:
        """Get list of devices for this account.

        Returns:
            List of device dicts with deviceSN, stationName, etc.
        """
        path = "/op/v0/device/list"
        payload = {"currentPage": 1, "pageSize": 10}
        result = await self._post(path, payload)
        if isinstance(result, dict):
            devices = (
                result.get("devices")
                or result.get("data")
                or result.get("list")
                or []
            )
        else:
            devices = result if isinstance(result, list) else []
        _LOGGER.info("FoxESS Cloud: found %d device(s)", len(devices))
        return devices

    async def get_real_data(self, sn: str = "") -> dict:
        """Get real-time data for a device.

        Args:
            sn: Device serial number (uses self.device_sn if empty)

        Returns:
            Dict with real-time device data
        """
        path = "/op/v1/device/real/query"
        device_sn = sn or self.device_sn
        payload = {
            "sns": [device_sn],
            "variables": [
                "pvPower",
                "gridConsumptionPower",
                "feedinPower",
                "meterPower",
                "loadsPower",
                "batPower",
                "invBatPower",
                "batChargePower",
                "batDischargePower",
                "SoC",
                "workMode",
                "generationPower",
                "chargePower",
                "dischargePower",
                "chargeEnergyToTal",
                "dischargeEnergyToTal",
            ],
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

        path = "/op/v3/device/scheduler/enable"
        payload = {"deviceSN": sn, "groups": [to_scheduler_v3_group(g) for g in groups]}
        _LOGGER.info("FoxESS Cloud: setting scheduler with %d groups for %s", len(groups), sn)
        return await self._post(path, payload, write=True)

    async def get_scheduler(self, sn: str = "") -> dict:
        """Get Scheduler V3 time segment information."""
        path = "/op/v3/device/scheduler/get"
        return await self._post(path, {"deviceSN": sn or self.device_sn})

    async def set_scheduler_v3(self, groups: list[dict], sn: str = "") -> dict:
        """Set Scheduler V3 time segment information."""
        return await self.set_scheduler(sn or self.device_sn, groups)

    async def set_scheduler_flag(self, enabled: bool, sn: str = "") -> dict:
        """Enable or disable the Scheduler V3 flag for a device."""
        path = "/op/v1/device/scheduler/set/flag"
        payload = {"deviceSN": sn or self.device_sn, "enable": 1 if enabled else 0}
        return await self._post(path, payload, write=True)

    async def get_device_setting(self, key: str, sn: str = "") -> dict:
        """Get a cloud device setting by key."""
        path = "/op/v0/device/setting/get"
        return await self._post(path, {"sn": sn or self.device_sn, "key": key})

    async def set_device_setting(self, key: str, value, sn: str = "") -> dict:
        """Set a cloud device setting by key."""
        path = "/op/v0/device/setting/set"
        return await self._post(
            path,
            {"sn": sn or self.device_sn, "key": key, "value": value},
            write=True,
        )

    async def set_device_setting_optional(self, key: str, value, sn: str = "") -> bool:
        """Set a setting, tolerating models that don't support the key.

        Returns True if applied, False if FoxESS rejected the key as unsupported.
        Other errors propagate.
        """
        try:
            await self.set_device_setting(key, value, sn)
            return True
        except FoxESSApiError as err:
            if _is_setting_unsupported(err):
                _LOGGER.debug("FoxESS setting %s unsupported on this device: %s", key, err)
                return False
            raise

    async def set_work_mode(self, mode: str, sn: str = "") -> dict:
        """Set device WorkMode, disabling an active schedule on 44096 then retrying.

        FoxESS rejects WorkMode writes with errno 44096 while Mode Scheduler is
        active. Mirror the cloud worker: disable the scheduler flag, then retry.
        """
        device_sn = sn or self.device_sn
        try:
            return await self.set_device_setting("WorkMode", mode, device_sn)
        except FoxESSApiError as err:
            if not _is_scheduler_blocks_work_mode(err):
                raise
            _LOGGER.warning(
                "FoxESS WorkMode blocked by active scheduler; disabling scheduler and retrying"
            )
            await self.set_scheduler_flag(False, device_sn)
            return await self.set_device_setting("WorkMode", mode, device_sn)

    async def get_battery_soc(self, sn: str = "") -> dict:
        """Get min SOC settings for the device battery."""
        path = "/op/v0/device/battery/soc/get"
        return await self._get(path, {"sn": sn or self.device_sn})

    async def set_battery_soc(
        self,
        min_soc: int,
        min_soc_on_grid: int | None = None,
        sn: str = "",
    ) -> dict:
        """Set min SOC settings for the device battery."""
        path = "/op/v0/device/battery/soc/set"
        value = int(max(0, min(100, min_soc)))
        payload = {
            "sn": sn or self.device_sn,
            "minSoc": value,
            "minSocOnGrid": int(max(0, min(100, min_soc_on_grid if min_soc_on_grid is not None else value))),
        }
        return await self._post(path, payload, write=True)

    async def get_module_list(self) -> list[dict]:
        """Get datalogger modules for this account."""
        path = "/op/v0/module/list"
        result = await self._post(path, {"currentPage": 1, "pageSize": 100})
        if isinstance(result, dict):
            return result.get("data") or result.get("list") or []
        return result if isinstance(result, list) else []

    async def send_modbus_command(
        self,
        module_sn: str,
        data: str,
        timeout: int = 10,
    ) -> dict:
        """Send a base64-encoded Modbus command through a FoxESS datalogger."""
        path = "/op/v0/module/modbus/commands"
        return await self._post(
            path,
            {"sn": module_sn, "timeout": timeout, "data": data},
            write=True,
        )

    async def force_charge(
        self,
        duration_minutes: int = 30,
        power_w: float = 0,
        target_soc: int = 100,
        min_soc: int = 10,
    ) -> dict:
        """Create an immediate ForceCharge Scheduler V3 group."""
        now = datetime.now()
        end = now + timedelta(minutes=max(1, duration_minutes))
        sh, sm, eh, em = _parse_time_range(now, end)
        group = {
            "startHour": sh,
            "startMinute": sm,
            "endHour": eh,
            "endMinute": em,
            "workMode": "ForceCharge",
            "extraParam": _schedule_extra_params(
                min_soc=min_soc,
                fd_soc=target_soc,
                fd_pwr=max(0, power_w),
                max_soc=target_soc,
            ),
        }
        return await self.set_scheduler_v3([group])

    async def force_discharge(
        self,
        duration_minutes: int = 30,
        power_w: float = 0,
        target_soc: int = 10,
        min_soc: int = 10,
    ) -> dict:
        """Create an immediate ForceDischarge Scheduler V3 group."""
        now = datetime.now()
        end = now + timedelta(minutes=max(1, duration_minutes))
        sh, sm, eh, em = _parse_time_range(now, end)
        group = {
            "startHour": sh,
            "startMinute": sm,
            "endHour": eh,
            "endMinute": em,
            "workMode": "ForceDischarge",
            "extraParam": _schedule_extra_params(
                min_soc=min_soc,
                fd_soc=max(target_soc, min_soc),
                fd_pwr=max(0, power_w),
            ),
        }
        return await self.set_scheduler_v3([group])

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
                found = any(_extract_device_sn(d) == self.device_sn for d in devices)
                if not found:
                    device_sns = [_extract_device_sn(d) or "?" for d in devices]
                    return False, (
                        f"Device '{self.device_sn}' not found. "
                        f"Available devices: {', '.join(device_sns)}"
                    )

            return True, f"Connected successfully. Found {len(devices)} device(s)."

        except Exception as e:
            return False, str(e)


def to_scheduler_v3_group(group: dict) -> dict:
    """Normalize legacy/internal scheduler groups to Scheduler V3 shape."""
    extra = group.get("extraParam")
    if not isinstance(extra, dict):
        extra = _schedule_extra_params(
            min_soc=group.get("minSocOnGrid", group.get("minsocongrid", 10)),
            fd_soc=group.get("fdSoc", group.get("fdsoc", 100)),
            fd_pwr=group.get("fdPwr", group.get("fdpwr", 0)),
            max_soc=group.get("maxSoc", group.get("maxsoc", 100)),
            import_limit=group.get("importLimit"),
            export_limit=group.get("exportLimit"),
            pv_limit=group.get("pvLimit"),
            reactive_power=group.get("reactivePower"),
        )

    return {
        "startHour": int(group.get("startHour", 0)),
        "startMinute": int(group.get("startMinute", 0)),
        "endHour": int(group.get("endHour", 23)),
        "endMinute": int(group.get("endMinute", 59)),
        "workMode": group.get("workMode", "SelfUse"),
        "extraParam": extra,
    }


def filter_public_scheduler_groups(groups: list[dict]) -> list[dict]:
    """Drop FoxCloud hidden full-day remaining-mode groups before reposting."""
    filtered = []
    for group in groups or []:
        if group.get("isRemainMode"):
            continue
        if (
            group.get("startHour") == 0
            and group.get("startMinute") == 0
            and group.get("endHour") == 23
            and group.get("endMinute") == 59
            and group.get("workMode") == "SelfUse"
        ):
            continue
        filtered.append(to_scheduler_v3_group(group))
    return filtered


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

        foxess_group = {
            "startHour": sh,
            "startMinute": sm,
            "endHour": eh,
            "endMinute": em,
            "workMode": mode,
            "extraParam": _schedule_extra_params(
                min_soc=min_soc,
                fd_soc=charge_soc if mode == "ForceCharge" else min_soc,
                fd_pwr=0,
                max_soc=charge_soc if mode == "ForceCharge" else 100,
            ),
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
