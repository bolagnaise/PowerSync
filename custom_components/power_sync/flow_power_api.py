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
            if resp.status == 401 or resp.status == 403:
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
                value = FlowPowerAPIClient._get_value(payload, key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
                if isinstance(value, dict):
                    nested = FlowPowerAPIClient._records(value, *keys)
                    if nested:
                        return nested
            return [payload]
        return []

    @staticmethod
    def _mapping_price_records(payload: Any) -> list[dict[str, Any]]:
        """Extract timestamp->price mappings from nested endpoint payloads."""
        if isinstance(payload, list):
            for item in payload:
                records = FlowPowerAPIClient._mapping_price_records(item)
                if records:
                    return records
            return []

        if not isinstance(payload, dict):
            return []

        records: list[dict[str, Any]] = []
        for key, value in payload.items():
            timestamp = FlowPowerAPIClient._parse_time(key)
            if timestamp is None:
                continue

            price = None
            if isinstance(value, dict):
                price = FlowPowerAPIClient._first_number(
                    value,
                    "price",
                    "Price",
                    "priceMwh",
                    "price_mwh",
                    "rrp",
                    "RRP",
                    "Rrp",
                    "value",
                    "Value",
                    "dispatchPrice",
                    "DispatchPrice",
                )
            else:
                try:
                    price = float(value)
                except (TypeError, ValueError):
                    price = None

            if price is None or not isfinite(price):
                continue

            records.append({"key": key, "price": price, "raw": value})

        if records:
            records.sort(
                key=lambda item: (
                    FlowPowerAPIClient._parse_time(item["key"])
                    or datetime.min.replace(tzinfo=dt_util.UTC)
                )
            )
            return records

        for value in payload.values():
            records = FlowPowerAPIClient._mapping_price_records(value)
            if records:
                return records
        return []

    @staticmethod
    def _normalize_key(key: str) -> str:
        return "".join(ch for ch in key.lower() if ch.isalnum())

    @staticmethod
    def _get_value(record: dict[str, Any], key: str) -> Any:
        if key in record:
            return record[key]

        wanted = FlowPowerAPIClient._normalize_key(key)
        for record_key, value in record.items():
            if FlowPowerAPIClient._normalize_key(str(record_key)) == wanted:
                return value
        return None

    @staticmethod
    def _first_number(record: dict[str, Any], *keys: str) -> float | None:
        for key in keys:
            value = FlowPowerAPIClient._get_value(record, key)
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
            value = FlowPowerAPIClient._get_value(record, key)
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
            "%Y/%m/%d %H:%M",
            "%d/%m/%Y %H:%M:%S",
            "%d/%m/%Y %H:%M",
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
    def _align_to_interval(value: datetime, duration: int) -> datetime:
        value = value if value.tzinfo else value.replace(tzinfo=dt_util.UTC)
        minute = (value.minute // duration) * duration
        return value.replace(minute=minute, second=0, microsecond=0)

    async def get_residential_sites(self) -> list[dict[str, Any]]:
        """Return residential sites available to the API key."""
        payload = await self._post("GetResidentialSites", {})
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
        """Return 5-minute dispatch prices in $/MWh."""
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
        """Return 5-minute predispatch prices in $/MWh."""
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
        """Return 30-minute predispatch prices in $/MWh."""
        payload = await self._post(
            "predispatch30mins",
            {"regName": reg_name, "period": period},
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
        fallback_start = self._align_to_interval(dt_util.utcnow(), duration)
        for idx, record in enumerate(records):
            explicit_time = self._parse_time(
                self._first_text(
                    record,
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
                    "intervalDateTime",
                    "IntervalDateTime",
                    "forecastDateTime",
                    "ForecastDateTime",
                    "tradingInterval",
                    "TradingInterval",
                    "tradingIntervalStart",
                    "TradingIntervalStart",
                    "startTime",
                    "StartTime",
                    "key",
                    "Key",
                )
            )
            if explicit_time is not None:
                fallback_start = explicit_time - timedelta(minutes=duration * idx)
                break
        inferred_timestamps = 0
        for idx, record in enumerate(records):
            price_mwh = self._first_number(
                record,
                "price",
                "Price",
                "priceMwh",
                "price_mwh",
                "rrp",
                "RRP",
                "Rrp",
                "value",
                "Value",
                "dispatchPrice",
                "DispatchPrice",
            )
            if price_mwh is None:
                continue
            period_time = self._parse_time(
                self._first_text(
                    record,
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
                    "intervalDateTime",
                    "IntervalDateTime",
                    "forecastDateTime",
                    "ForecastDateTime",
                    "tradingInterval",
                    "TradingInterval",
                    "tradingIntervalStart",
                    "TradingIntervalStart",
                    "startTime",
                    "StartTime",
                    "key",
                    "Key",
                )
            )
            if period_time is None:
                period_time = fallback_start + timedelta(minutes=duration * idx)
                inferred_timestamps += 1
            normalized.append(
                {
                    "nemTime": period_time.isoformat(),
                    "perKwh": price_mwh / 10.0,
                    "wholesaleKWHPrice": price_mwh / 1000.0,
                    "price_mwh": price_mwh,
                    "duration": duration,
                    "raw": record,
                }
            )

        if not normalized:
            mapped_records = self._mapping_price_records(payload)
            if mapped_records and mapped_records != records:
                return self._normalize_price_records(mapped_records, duration=duration)

        if inferred_timestamps:
            _LOGGER.debug(
                "Flow Power API inferred %d/%d price timestamps from response order",
                inferred_timestamps,
                len(normalized),
            )

        normalized.sort(key=lambda item: item["nemTime"])
        return normalized


def normalize_site_summary(user_obj: dict[str, Any]) -> dict[str, Any]:
    """Normalize KWatch residential summary fields to the portal sensor shape."""
    def number(key: str) -> float | None:
        value = user_obj.get(key)
        if value in (None, ""):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if isfinite(parsed) else None

    lwap = number("LWAP")
    twap = number("TWAP")
    lwap_imp = number("LWAPImp")
    twap_imp = number("TWAPImp")
    return {
        "lwap": lwap,
        "lwap_import": lwap_imp,
        "lwap_actual": number("LWAPActual"),
        "lwap_import_actual": number("LWAPImpActual"),
        "twap": twap,
        "twap_import": twap_imp,
        "avg_rrp": number("AvgRRP"),
        "avg_usage_kw": number("AvgUsage"),
        "avg_import_usage_kw": number("AvgImpUsage"),
        "max_usage_kw": number("MaxUsage"),
        "total_intervals": number("TotalInterval"),
        "pea_30_days": number("PEA30Days"),
        "pea_30_import": number("PEA30ImportDays"),
        "pea_actual": number("PEAActual"),
        "pea_target": number("PEATarget"),
        "pea_actual_import": number("PEAActualImport"),
        "pea_target_import": number("PEATargetImport"),
        "bpea": number("PEATarget"),
        "bpea_import": number("PEATargetImport"),
        "cpea": (lwap or 0) - (twap or 0),
        "cpea_import": (lwap_imp or 0) - (twap_imp or 0),
        "site_losses_dlf": number("SiteLosses"),
        "gst_multiplier": number("GST"),
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
