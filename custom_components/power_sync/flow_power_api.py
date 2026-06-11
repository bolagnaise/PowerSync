"""Flow Power KWatch public API client."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from math import isfinite
from typing import Any

import aiohttp
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

FLOW_POWER_API_BASE_URL = "https://api.kwatch.com.au/api/v1"


class FlowPowerAPIError(Exception):
    """Raised when the Flow Power API returns an unusable response."""


class FlowPowerAPIClient:
    """Client for Flow Power's KWatch API."""

    def __init__(
        self,
        api_key: str,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._api_key = api_key
        self._session = session
        self._owns_session = session is None

    async def close(self) -> None:
        """Close the owned HTTP session."""
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def _post(
        self,
        endpoint: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        """POST to a KWatch endpoint and return decoded JSON."""
        session = await self._get_session()
        url = f"{FLOW_POWER_API_BASE_URL}/{endpoint}"
        headers = {
            "x-api-key": self._api_key,
            "Accept": "application/json",
        }
        request_kwargs: dict[str, Any] = {
            "headers": headers,
            "timeout": aiohttp.ClientTimeout(total=30),
        }
        if payload is not None:
            request_kwargs["json"] = payload
        async with session.post(url, **request_kwargs) as resp:
            text = await resp.text()
            if resp.status == 401:
                raise FlowPowerAPIError("invalid_api_key")
            if resp.status == 403:
                body = text.strip()
                if "allowlist" in body.lower():
                    raise FlowPowerAPIError("host_not_allowlisted")
                raise FlowPowerAPIError("invalid_api_key")
            if resp.status >= 400:
                raise FlowPowerAPIError(f"api_status_{resp.status}")
            try:
                payload = await resp.json(content_type=None)
            except Exception as err:
                _LOGGER.debug(
                    "Flow Power API %s returned non-JSON response: %s",
                    endpoint,
                    text[:200],
                )
                raise FlowPowerAPIError("invalid_json") from err
            return self._decode_nested_json(payload, endpoint)

    @staticmethod
    def _decode_nested_json(payload: Any, endpoint: str) -> Any:
        """Decode KWatch responses that wrap JSON inside a JSON string."""
        decoded = payload
        for _ in range(3):
            if not isinstance(decoded, str):
                return decoded
            text = decoded.strip()
            if not text:
                return decoded
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                return decoded
            _LOGGER.debug("Flow Power API %s returned nested JSON string", endpoint)
        return decoded

    @staticmethod
    def _records(payload: Any, *keys: str) -> list[dict[str, Any]]:
        """Return a list of dict records from common API wrapper shapes."""
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in keys + ("data", "result", "results", "items", "value"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
                if isinstance(value, dict):
                    nested = FlowPowerAPIClient._records(value, *keys)
                    if nested:
                        return nested
            return [payload]
        return []

    @staticmethod
    def _first_number(record: dict[str, Any], *keys: str) -> float | None:
        for key in keys:
            value = record.get(key)
            if value in (None, ""):
                continue
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                continue
            if isfinite(parsed):
                return parsed
        return None

    @staticmethod
    def _first_text(record: dict[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = record.get(key)
            if value not in (None, ""):
                return str(value)
        return None

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=dt_util.UTC)
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        for fmt in (
            None,
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y/%m/%d %H:%M:%S",
        ):
            try:
                if fmt is None:
                    parsed = datetime.fromisoformat(text)
                else:
                    parsed = datetime.strptime(text, fmt)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_util.UTC)
            except ValueError:
                continue
        return None

    @staticmethod
    def _api_datetime(value: datetime | str) -> str:
        """Format a datetime value for KWatch DateTime payload fields."""
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    async def get_residential_sites(self) -> list[dict[str, Any]]:
        """Return residential sites available to the API key."""
        payload = await self._post("GetResidentialSites")
        sites = self._records(payload, "sites")
        return [
            {
                "nmi": self._first_text(site, "nmi", "NMI", "Nmi"),
                "networkTariff": self._first_text(
                    site,
                    "networkTariff",
                    "NetworkTariff",
                    "network_tariff",
                ),
                "raw": site,
            }
            for site in sites
            if self._first_text(site, "nmi", "NMI", "Nmi")
        ]

    async def get_residential_site(self, nmi: str) -> dict[str, Any] | None:
        """Return one residential site."""
        payload = await self._post("GetResidentialSite", {"nmi": nmi})
        records = self._records(payload, "site", "sites")
        return records[0] if records else None

    async def get_residential_site_summary(self, nmi: str) -> dict[str, Any] | None:
        """Return normalized account summary values for one NMI."""
        payload = await self._post("GetResidentialSiteSummary", {"nmi": nmi})
        records = self._records(payload, "summary")
        if not records:
            return None
        return normalize_site_summary(records[0])

    async def dispatch5mins(self, reg_name: str, period: float = 60) -> list[dict[str, Any]]:
        """Return 5-minute dispatch prices for a minutes-based period."""
        payload = await self._post(
            "dispatch5mins",
            {"regName": reg_name, "period": period},
        )
        return self._normalize_price_records(payload, duration=5)

    async def predispatch5mins(
        self,
        reg_name: str,
        period: float = 60,
    ) -> list[dict[str, Any]]:
        """Return 5-minute predispatch prices for a minutes-based period."""
        payload = await self._post(
            "predispatch5mins",
            {"regName": reg_name, "period": period},
        )
        return self._normalize_price_records(payload, duration=5)

    async def predispatch30mins(
        self,
        reg_name: str,
        period: float = 2,
    ) -> list[dict[str, Any]]:
        """Return 30-minute predispatch prices for a days-based period."""
        payload = await self._post(
            "predispatch30mins",
            {"regName": reg_name, "period": period},
        )
        return self._normalize_price_records(payload, duration=30)

    async def dispatch30mins(
        self,
        reg_name: str,
        period: float,
    ) -> list[dict[str, Any]]:
        """Return 30-minute dispatch prices for a days-based lookback period."""
        payload = await self._post(
            "dispatch30mins",
            {"regName": reg_name, "period": period},
        )
        return self._normalize_price_records(payload, duration=30)

    async def dispatch30mins_date_range(
        self,
        reg_name: str,
        start_date: datetime | str,
        end_date: datetime | str,
    ) -> list[dict[str, Any]]:
        """Return 30-minute dispatch prices for a date range."""
        payload = await self._post(
            "dispatch30minsDateRange",
            {
                "regName": reg_name,
                "startDate": self._api_datetime(start_date),
                "endDate": self._api_datetime(end_date),
            },
        )
        return self._normalize_price_records(payload, duration=30)

    async def predispatch_demand30mins(
        self,
        reg_name: str,
        period: float = 2,
    ) -> list[dict[str, Any]]:
        """Return 30-minute predispatch demand for a days-based period."""
        payload = await self._post(
            "PreDispatchDemand30mins",
            {"regName": reg_name, "period": period},
        )
        return self._normalize_quantity_records(payload, duration=30, unit="MW")

    async def dispatch_demand30mins(
        self,
        reg_name: str,
        period: float,
    ) -> list[dict[str, Any]]:
        """Return 30-minute dispatch demand for a days-based lookback period."""
        payload = await self._post(
            "DispatchDemand30mins",
            {"regName": reg_name, "period": period},
        )
        return self._normalize_quantity_records(payload, duration=30, unit="MW")

    async def quarter_ceiling_price(
        self,
        reg_name: str,
        quarter: int,
        start_date: datetime | str,
        end_date: datetime | str,
    ) -> list[dict[str, Any]]:
        """Return quarter ceiling prices for the supplied quarter and date range."""
        payload = await self._post(
            "QuarterCeilingPrice",
            {
                "regName": reg_name,
                "quarter": quarter,
                "startDate": self._api_datetime(start_date),
                "endDate": self._api_datetime(end_date),
            },
        )
        return self._normalize_price_records(payload, duration=30)

    def _normalize_price_records(
        self,
        payload: Any,
        *,
        duration: int,
    ) -> list[dict[str, Any]]:
        records = self._records(
            payload,
            "prices",
            "dispatch",
            "predispatch",
            "priceData",
            "PriceData",
        )
        normalized: list[dict[str, Any]] = []
        for record in records:
            price_mwh = self._first_number(
                record,
                "Value",            # KWatch {Key, Value} shape
                "value",
                "price",
                "Price",
                "rrp",
                "RRP",
                "Rrp",
                "dispatchPrice",
                "DispatchPrice",
            )
            if price_mwh is None:
                continue
            period_time = self._parse_time(
                self._first_text(
                    record,
                    "Key",              # KWatch {Key, Value} shape
                    "key",
                    "time",
                    "Time",
                    "timestamp",
                    "Timestamp",
                    "settlementDate",
                    "SettlementDate",
                    "dateTime",
                    "DateTime",
                    "periodDateTime",
                    "PeriodDateTime",
                )
            )
            if period_time is None:
                period_time = dt_util.utcnow()
            normalized.append(
                {
                    "nemTime": period_time.isoformat(),
                    "perKwh": price_mwh / 10.0,
                    "duration": duration,
                    "raw": record,
                }
            )

        if not normalized:
            _LOGGER.debug(
                "Flow Power API returned no parseable price records from shape %s",
                type(payload).__name__,
            )
            return []

        normalized.sort(key=lambda item: item["nemTime"])
        return normalized

    def _normalize_quantity_records(
        self,
        payload: Any,
        *,
        duration: int,
        unit: str,
    ) -> list[dict[str, Any]]:
        records = self._records(
            payload,
            "demand",
            "demands",
            "values",
            "priceData",
            "PriceData",
        )
        normalized: list[dict[str, Any]] = []
        for record in records:
            value = self._first_number(
                record,
                "Value",
                "value",
                "demand",
                "Demand",
                "demandMw",
                "DemandMw",
                "mw",
                "MW",
            )
            if value is None:
                continue
            period_time = self._parse_time(
                self._first_text(
                    record,
                    "Key",
                    "key",
                    "time",
                    "Time",
                    "timestamp",
                    "Timestamp",
                    "settlementDate",
                    "SettlementDate",
                    "dateTime",
                    "DateTime",
                    "periodDateTime",
                    "PeriodDateTime",
                )
            )
            if period_time is None:
                period_time = dt_util.utcnow()
            normalized.append(
                {
                    "nemTime": period_time.isoformat(),
                    "value": value,
                    "unit": unit,
                    "duration": duration,
                    "raw": record,
                }
            )

        if not normalized:
            _LOGGER.debug(
                "Flow Power API returned no parseable quantity records from shape %s",
                type(payload).__name__,
            )
            return []

        normalized.sort(key=lambda item: item["nemTime"])
        return normalized


def normalize_site_summary(user_obj: dict[str, Any]) -> dict[str, Any]:
    """Normalize KWatch residential summary fields to the portal sensor shape."""
    lwap = user_obj.get("LWAP")
    twap = user_obj.get("TWAP")
    lwap_imp = user_obj.get("LWAPImp")
    twap_imp = user_obj.get("TWAPImp")
    return {
        "lwap": lwap,
        "lwap_import": lwap_imp,
        "lwap_actual": user_obj.get("LWAPActual"),
        "lwap_import_actual": user_obj.get("LWAPImpActual"),
        "twap": twap,
        "twap_import": twap_imp,
        "avg_rrp": user_obj.get("AvgRRP"),
        "avg_usage_kw": user_obj.get("AvgUsage"),
        "avg_import_usage_kw": user_obj.get("AvgImpUsage"),
        "max_usage_kw": user_obj.get("MaxUsage"),
        "total_intervals": user_obj.get("TotalInterval"),
        "pea_30_days": user_obj.get("PEA30Days"),
        "pea_30_import": user_obj.get("PEA30ImportDays"),
        "pea_actual": user_obj.get("PEAActual"),
        "pea_target": user_obj.get("PEATarget"),
        "pea_actual_import": user_obj.get("PEAActualImport"),
        "pea_target_import": user_obj.get("PEATargetImport"),
        "bpea": user_obj.get("PEATarget"),
        "bpea_import": user_obj.get("PEATargetImport"),
        "cpea": (lwap or 0) - (twap or 0),
        "cpea_import": (lwap_imp or 0) - (twap_imp or 0),
        "site_losses_dlf": user_obj.get("SiteLosses"),
        "gst_multiplier": user_obj.get("GST"),
        "source": "api",
    }


def kwatch_prices_to_amber_format(
    prices: list[dict[str, Any]],
    *,
    interval_type: str,
    default_duration: int,
) -> list[dict[str, Any]]:
    """Convert normalized KWatch prices to Amber-compatible entries."""
    entries: list[dict[str, Any]] = []
    for price in prices:
        starts_at = FlowPowerAPIClient._parse_time(price.get("nemTime"))
        if starts_at is None:
            starts_at = dt_util.utcnow()
        duration = int(price.get("duration") or default_duration)
        ends_at = starts_at + timedelta(minutes=duration)
        price_cents = float(price["perKwh"])
        entries.append(
            {
                "nemTime": ends_at.isoformat(),
                "perKwh": price_cents,
                "channelType": "general",
                "type": interval_type,
                "duration": duration,
                "wholesaleKWHPrice": price_cents,
            }
        )
        entries.append(
            {
                "nemTime": ends_at.isoformat(),
                "perKwh": -price_cents,
                "channelType": "feedIn",
                "type": interval_type,
                "duration": duration,
                "wholesaleKWHPrice": price_cents,
            }
        )
    return entries
