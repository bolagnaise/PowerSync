"""Confirmed Tesla grid-charging writes shared by control paths."""

from __future__ import annotations

import asyncio
from enum import Enum
import logging
import time
from typing import Any, Awaitable, Callable, NamedTuple

import aiohttp


_LOGGER = logging.getLogger(__name__)

_TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}
_ACCEPTED_HTTP_STATUSES = {200, 201, 202}


class TeslaGridWriteStatus(str, Enum):
    """Result of a Tesla grid-charging write and direct readback."""

    APPLIED = "applied"
    ACCEPTED_FIELD_ABSENT = "accepted_field_absent"
    ACCEPTED_UNCONFIRMED = "accepted_unconfirmed"
    REJECTED = "rejected"
    TRANSPORT_ERROR = "transport_error"


class TeslaGridWriteOutcome(NamedTuple):
    """Structured Tesla grid-charging result."""

    status: TeslaGridWriteStatus
    http_status: int | None = None
    detail: str | None = None

    @property
    def applied(self) -> bool:
        """Return whether Tesla readback confirmed the desired state."""
        return self.status is TeslaGridWriteStatus.APPLIED


def _optional_bool(value: Any) -> bool | None:
    """Return a bool for API booleans/strings, or None when unknown."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
        return None
    return bool(value)


def tesla_grid_charging_enabled_from_site_info(
    site_info: dict[str, Any],
) -> bool | None:
    """Extract Tesla's grid-charging state from either site_info shape."""
    components = site_info.get("components", {})
    disallow = (
        components.get("disallow_charge_from_grid_with_solar_installed")
        if isinstance(components, dict)
        else None
    )
    if disallow is None:
        disallow = site_info.get(
            "disallow_charge_from_grid_with_solar_installed"
        )
    parsed = _optional_bool(disallow)
    return None if parsed is None else not parsed


def tesla_grid_charging_field_present(
    site_info: dict[str, Any],
) -> bool:
    """Return whether site_info includes Tesla's grid-charging field."""
    field = "disallow_charge_from_grid_with_solar_installed"
    components = site_info.get("components")
    return (
        isinstance(components, dict)
        and field in components
    ) or field in site_info


def tesla_site_info_has_structure(site_info: dict[str, Any]) -> bool:
    """Return whether a payload resembles Tesla's site_info response.

    An empty or unrelated HTTP-200 object is not evidence that a particular
    field is unavailable.  Require another stable site_info marker before the
    field-absence compatibility outcome can be used.
    """
    components = site_info.get("components")
    component_markers = {
        "battery",
        "grid_status",
        "load_meter",
        "site_meter",
        "solar",
        "disallow_charge_from_grid_with_solar_installed",
    }
    return (
        isinstance(components, dict)
        and bool(component_markers.intersection(components))
    ) or any(
        field in site_info
        for field in (
            "default_real_mode",
            "backup_reserve_percent",
            "site_name",
            "timezone",
            "site_id",
        )
    )


async def _response_json(response: Any) -> dict[str, Any] | None:
    try:
        payload = await response.json()
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


async def _response_text(response: Any) -> str:
    try:
        return str(await response.text())[:200]
    except Exception:
        return ""


def _response_rejection(payload: dict[str, Any] | None) -> str | None:
    if not payload:
        return None
    response_data = payload.get("response", payload)
    if not isinstance(response_data, dict) or "result" not in response_data:
        return None
    if response_data.get("result") is not False:
        return None
    return str(response_data.get("reason") or "Tesla rejected the write")


def _retry_after_seconds(response: Any) -> float | None:
    headers = getattr(response, "headers", None) or {}
    try:
        value = float(headers.get("Retry-After"))
    except (TypeError, ValueError):
        return None
    return max(0.0, min(value, 5.0))


async def async_set_tesla_grid_charging_confirmed(
    session: Any,
    api_base_url: str,
    site_id: str,
    headers: dict[str, str],
    enabled: bool,
    *,
    confirmation_deadline: float = 10.0,
    poll_offsets: tuple[float, ...] = (0.5, 1.5, 3.5, 5.5, 7.5),
    max_post_attempts: int = 3,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    is_current: Callable[[], bool] | None = None,
) -> TeslaGridWriteOutcome:
    """Write Tesla grid charging and confirm it through direct site_info reads.

    Transport failures may retry the POST. Once Tesla accepts a POST, this
    function only polls readback and never duplicates the accepted command.
    Reads are deliberately direct and uncached so a coordinator cache or prior
    ``_site_info_fetch_failed`` latch cannot produce a false confirmation.
    """
    api_base_url = api_base_url.rstrip("/")
    write_url = f"{api_base_url}/api/1/energy_sites/{site_id}/grid_import_export"
    read_url = f"{api_base_url}/api/1/energy_sites/{site_id}/site_info"
    payload = {
        "disallow_charge_from_grid_with_solar_installed": not bool(enabled)
    }

    last_status: int | None = None
    last_detail: str | None = None
    retry_delay = 0.0
    for attempt in range(1, max(1, max_post_attempts) + 1):
        if is_current is not None and not is_current():
            return TeslaGridWriteOutcome(
                TeslaGridWriteStatus.TRANSPORT_ERROR,
                detail="superseded",
            )
        if attempt > 1:
            await sleep(retry_delay)
            if is_current is not None and not is_current():
                return TeslaGridWriteOutcome(
                    TeslaGridWriteStatus.TRANSPORT_ERROR,
                    detail="superseded",
                )
        try:
            async with session.post(
                write_url,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as response:
                last_status = response.status
                response_payload = await _response_json(response)
                if response.status in _ACCEPTED_HTTP_STATUSES:
                    rejection = _response_rejection(response_payload)
                    if rejection is not None:
                        return TeslaGridWriteOutcome(
                            TeslaGridWriteStatus.REJECTED,
                            response.status,
                            rejection,
                        )
                    break

                last_detail = await _response_text(response)
                if response.status not in _TRANSIENT_HTTP_STATUSES:
                    return TeslaGridWriteOutcome(
                        TeslaGridWriteStatus.REJECTED,
                        response.status,
                        last_detail or "Tesla rejected the write",
                    )
                retry_delay = _retry_after_seconds(response) or min(
                    2 ** (attempt - 1), 5.0
                )
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            last_detail = str(err) or err.__class__.__name__
            retry_delay = min(2 ** (attempt - 1), 5.0)
    else:
        return TeslaGridWriteOutcome(
            TeslaGridWriteStatus.TRANSPORT_ERROR,
            last_status,
            last_detail or "Tesla write transport failed",
        )

    accepted_at = monotonic()
    deadline_at = accepted_at + max(0.0, confirmation_deadline)
    read_attempts = 0
    valid_site_info_reads = 0
    field_absent_reads = 0
    invalid_site_info_read = False
    for offset in poll_offsets:
        if is_current is not None and not is_current():
            return TeslaGridWriteOutcome(
                TeslaGridWriteStatus.ACCEPTED_UNCONFIRMED,
                last_status,
                "superseded",
            )

        target_at = accepted_at + max(0.0, offset)
        remaining_before_poll = deadline_at - monotonic()
        if remaining_before_poll <= 0:
            break
        if read_attempts and monotonic() > target_at:
            continue
        delay = target_at - monotonic()
        if delay > 0:
            await sleep(min(delay, remaining_before_poll))
        if monotonic() >= deadline_at:
            break
        if is_current is not None and not is_current():
            return TeslaGridWriteOutcome(
                TeslaGridWriteStatus.ACCEPTED_UNCONFIRMED,
                last_status,
                "superseded",
            )

        read_attempts += 1
        request_timeout = max(0.1, min(3.0, deadline_at - monotonic()))
        try:
            async with session.get(
                read_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=request_timeout),
            ) as response:
                if response.status == 200:
                    response_payload = await _response_json(response)
                    site_info = (
                        response_payload.get("response", response_payload)
                        if response_payload
                        else None
                    )
                    observed = (
                        tesla_grid_charging_enabled_from_site_info(site_info)
                        if isinstance(site_info, dict)
                        else None
                    )
                    if (
                        isinstance(site_info, dict)
                        and tesla_site_info_has_structure(site_info)
                    ):
                        valid_site_info_reads += 1
                        if not tesla_grid_charging_field_present(site_info):
                            field_absent_reads += 1
                    else:
                        invalid_site_info_read = True
                    if observed is bool(enabled):
                        latency = monotonic() - accepted_at
                        _LOGGER.info(
                            "Tesla grid charging %s confirmed for site %s after %.1fs (%d reads)",
                            "enabled" if enabled else "disabled",
                            site_id,
                            latency,
                            read_attempts,
                        )
                        return TeslaGridWriteOutcome(
                            TeslaGridWriteStatus.APPLIED,
                            last_status,
                        )
                    continue

                if response.status in (401, 403, 404):
                    return TeslaGridWriteOutcome(
                        TeslaGridWriteStatus.REJECTED,
                        response.status,
                        await _response_text(response)
                        or "site_info readback was rejected",
                    )
        except (asyncio.TimeoutError, aiohttp.ClientError):
            continue

    if is_current is not None and not is_current():
        return TeslaGridWriteOutcome(
            TeslaGridWriteStatus.ACCEPTED_UNCONFIRMED,
            last_status,
            "superseded",
        )

    if (
        valid_site_info_reads >= 2
        and field_absent_reads == valid_site_info_reads
        and not invalid_site_info_read
    ):
        _LOGGER.warning(
            "Tesla accepted grid charging %s for site %s but %d direct site_info "
            "readback(s) omitted the grid-charging field",
            "enable" if enabled else "disable",
            site_id,
            valid_site_info_reads,
        )
        return TeslaGridWriteOutcome(
            TeslaGridWriteStatus.ACCEPTED_FIELD_ABSENT,
            last_status,
            "direct site_info readback omitted the grid-charging field",
        )

    _LOGGER.warning(
        "Tesla accepted grid charging %s for site %s but direct readback did not confirm within %.1fs",
        "enable" if enabled else "disable",
        site_id,
        confirmation_deadline,
    )
    return TeslaGridWriteOutcome(
        TeslaGridWriteStatus.ACCEPTED_UNCONFIRMED,
        last_status,
        "direct site_info readback did not confirm the requested state",
    )
