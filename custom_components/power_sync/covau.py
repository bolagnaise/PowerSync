"""CovaU SolarMax public-plan ingestion and marginal pricing."""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .quota import (
    MarginalBucket,
    QuotaLedger,
    QuotaLedgerState,
    QuotaRule,
    tariff_datetime,
)

COVAU_SCHEMA_VERSION = 1
COVAU_PARSER_VERSION = 2
COVAU_CDR_BASE_URL = "https://cdr.energymadeeasy.gov.au/covau/cds-au/v1/energy/plans"
COVAU_SOURCE_KIND = "aer_cdr"
COVAU_IMPORT_RULE_ID = "covau_solarmax_free_import"
COVAU_EXPORT_RULE_ID = "covau_solarmax_premium_export"
COVAU_BASE_IMPORT_PERIOD_ID = "covau_base_import"
COVAU_BASE_EXPORT_PERIOD_ID = "covau_base_export"

COVAU_STATE_TIMEZONES = {
    "NSW": "Australia/Sydney",
    "QLD": "Australia/Brisbane",
    "SA": "Australia/Adelaide",
}

SUPPORTED_SOLARMAX_PLANS: dict[str, dict[str, str]] = {
    "COV1117610MRE3@EME": {
        "distributor": "Ausgrid",
        "state": "NSW",
        "display_name": "SolarMax NSW Ausgrid Residential TOU",
    },
    "COV1117611MRE3@EME": {
        "distributor": "Endeavour Energy",
        "state": "NSW",
        "display_name": "SolarMax NSW Endeavour Residential TOU",
    },
    "COV1117612MRE3@EME": {
        "distributor": "Essential Energy",
        "state": "NSW",
        "display_name": "SolarMax NSW Essential Residential TOU",
    },
    "COV1117614MRE3@EME": {
        "distributor": "Energex",
        "state": "QLD",
        "display_name": "SolarMax QLD Energex Residential TOU",
    },
    "COV1117616MRE3@EME": {
        "distributor": "SA Power Networks",
        "state": "SA",
        "display_name": "SolarMax SA Residential TOU",
    },
}

# Retired IDs still identify the stored, immutable snapshots of existing sites.
RETIRED_SOLARMAX_PLAN_IDS = {
    "COV1117610MRE2@EME": "COV1117610MRE3@EME",
    "COV1117611MRE2@EME": "COV1117611MRE3@EME",
    "COV1117612MRE2@EME": "COV1117612MRE3@EME",
    "COV1117614MRE2@EME": "COV1117614MRE3@EME",
    "COV1117616MRE2@EME": "COV1117616MRE3@EME",
}


@dataclass(frozen=True)
class CovaURatePeriod:
    start: str
    end: str
    c_per_kwh: float


@dataclass(frozen=True)
class CovaUPlanSnapshot:
    schema_version: int
    parser_version: int
    plan_id: str
    display_name: str
    distributor: str
    state: str
    effective_date: str
    withdrawn_date: str | None
    timezone_token: str
    supply_c_per_day: float
    import_periods: tuple[CovaURatePeriod, ...]
    export_base_c_per_kwh: float
    free_import_start: str
    free_import_end: str
    free_import_cap_kwh: float
    premium_export_start: str
    premium_export_end: str
    premium_export_cap_kwh: float
    premium_export_total_c_per_kwh: float
    source_kind: str
    source_url: str
    source_last_updated: str | None
    content_hash: str
    manual: bool = False

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["import_periods"] = [asdict(period) for period in self.import_periods]
        return value

    @classmethod
    def from_dict(
        cls,
        raw: dict[str, Any],
        *,
        timezone_token: str | None = None,
    ) -> "CovaUPlanSnapshot":
        state = str(raw.get("state") or "")
        stored_timezone = str(raw.get("timezone_token") or "")
        effective_timezone = _local_timezone_token(
            timezone_token or stored_timezone,
            state,
        )
        stored_parser_version = int(
            raw.get("parser_version", COVAU_PARSER_VERSION)
        )
        content_hash = str(raw.get("content_hash") or "")
        if (
            effective_timezone != stored_timezone
            or stored_parser_version < COVAU_PARSER_VERSION
        ):
            content_hash = _migrated_content_hash(
                content_hash,
                effective_timezone,
            )
        return cls(
            schema_version=int(raw.get("schema_version", COVAU_SCHEMA_VERSION)),
            parser_version=COVAU_PARSER_VERSION,
            plan_id=str(raw["plan_id"]),
            display_name=str(raw.get("display_name") or raw["plan_id"]),
            distributor=str(raw.get("distributor") or ""),
            state=state,
            effective_date=str(raw.get("effective_date") or ""),
            withdrawn_date=raw.get("withdrawn_date"),
            timezone_token=effective_timezone,
            supply_c_per_day=float(raw.get("supply_c_per_day") or 0.0),
            import_periods=tuple(
                CovaURatePeriod(
                    start=str(period["start"]),
                    end=str(period["end"]),
                    c_per_kwh=float(period["c_per_kwh"]),
                )
                for period in raw.get("import_periods", [])
            ),
            export_base_c_per_kwh=float(raw.get("export_base_c_per_kwh") or 0.0),
            free_import_start=str(raw.get("free_import_start") or "11:00"),
            free_import_end=str(raw.get("free_import_end") or "14:00"),
            free_import_cap_kwh=float(raw.get("free_import_cap_kwh") or 0.0),
            premium_export_start=str(raw.get("premium_export_start") or "18:00"),
            premium_export_end=str(raw.get("premium_export_end") or "21:00"),
            premium_export_cap_kwh=float(raw.get("premium_export_cap_kwh") or 0.0),
            premium_export_total_c_per_kwh=float(
                raw.get("premium_export_total_c_per_kwh") or 0.0
            ),
            source_kind=str(raw.get("source_kind") or COVAU_SOURCE_KIND),
            source_url=str(raw.get("source_url") or ""),
            source_last_updated=raw.get("source_last_updated"),
            content_hash=content_hash,
            manual=bool(raw.get("manual", False)),
        )


def covau_plan_candidates(postcode: str | int | None) -> list[dict[str, str]]:
    """Return state-filtered candidates; distributor and plan still require confirmation."""
    if postcode is None or not str(postcode).strip():
        return [
            {"plan_id": plan_id, **metadata}
            for plan_id, metadata in SUPPORTED_SOLARMAX_PLANS.items()
        ]
    state = _state_for_postcode(postcode)
    if state is None:
        return []
    return [
        {"plan_id": plan_id, **metadata}
        for plan_id, metadata in SUPPORTED_SOLARMAX_PLANS.items()
        if metadata["state"] == state
    ]


async def async_fetch_covau_plan(hass: Any, plan_id: str) -> dict[str, Any]:
    """Fetch one public CDR product snapshot without account credentials."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    if plan_id not in SUPPORTED_SOLARMAX_PLANS:
        raise ValueError("Unsupported CovaU SolarMax plan")
    url = f"{COVAU_CDR_BASE_URL}/{quote(plan_id, safe='')}"
    response = await async_get_clientsession(hass).get(
        url,
        headers={"x-v": "3", "x-min-v": "3", "Accept": "application/json"},
    )
    response.raise_for_status()
    return await response.json()


def normalize_covau_plan(
    raw_response: dict[str, Any],
    expected_plan_id: str,
    *,
    timezone_token: str | None = None,
) -> CovaUPlanSnapshot:
    """Normalize an AER/CDR snapshot using the customer's local tariff clock."""
    data = raw_response.get("data") or {}
    plan_id = str(data.get("planId") or "")
    metadata = SUPPORTED_SOLARMAX_PLANS.get(
        RETIRED_SOLARMAX_PLAN_IDS.get(plan_id, plan_id)
    )
    if plan_id != expected_plan_id or metadata is None:
        raise ValueError("CDR response did not match the selected SolarMax plan")
    contract = data.get("electricityContract") or {}
    source_timezone_token = str(contract.get("timeZone") or "")
    if source_timezone_token not in {"AEST", "LOCAL"}:
        raise ValueError("SolarMax CDR plan has an unsupported source timezone")
    tariff_periods = contract.get("tariffPeriod") or []
    if not tariff_periods:
        raise ValueError("SolarMax plan has no tariff periods")
    tariff = tariff_periods[0]

    import_periods: list[CovaURatePeriod] = []
    free_start = free_end = None
    free_cap = 0.0
    for rate_group in tariff.get("timeOfUseRates") or []:
        rates = rate_group.get("rates") or []
        if not rates:
            continue
        base_rate = _gst_import_cents(rates[-1].get("unitPrice"))
        for window in rate_group.get("timeOfUse") or []:
            start = _normalize_start(window.get("startTime"))
            end = _normalize_end(window.get("endTime"))
            import_periods.append(CovaURatePeriod(start, end, base_rate))
            capped = next((item for item in rates if item.get("volume") is not None), None)
            if capped is not None:
                free_start, free_end = start, end
                free_cap = float(capped.get("volume") or 0.0)

    supply_c_per_day = _gst_import_cents(tariff.get("dailySupplyCharge"))
    export_base = None
    premium_total = None
    premium_cap = 0.0
    premium_start = premium_end = None
    for feed_in in contract.get("solarFeedInTariff") or []:
        for group in feed_in.get("timeVaryingTariffs") or []:
            rates = group.get("rates") or []
            windows = group.get("timeVariations") or []
            for item in rates:
                price = _export_cents(item.get("unitPrice"))
                volume = item.get("volume")
                if volume is None:
                    export_base = price if export_base is None else min(export_base, price)
                else:
                    premium_total = price
                    premium_cap = float(volume)
                    if windows:
                        premium_start = _normalize_start(windows[0].get("startTime"))
                        premium_end = _normalize_end(windows[0].get("endTime"))

    if not import_periods or free_start is None or free_end is None or free_cap <= 0:
        raise ValueError("SolarMax free-import quota was not present in the plan")
    if export_base is None or premium_total is None or premium_cap <= 0:
        raise ValueError("SolarMax premium-export quota was not present in the plan")
    if premium_total < export_base:
        raise ValueError("SolarMax premium export price is below its base rate")

    effective_timezone = _local_timezone_token(
        timezone_token,
        metadata["state"],
    )
    source_url = f"{COVAU_CDR_BASE_URL}/{quote(plan_id, safe='')}"
    canonical = json.dumps(
        {
            "source": raw_response,
            "timezone_token": effective_timezone,
            "parser_version": COVAU_PARSER_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return CovaUPlanSnapshot(
        schema_version=COVAU_SCHEMA_VERSION,
        parser_version=COVAU_PARSER_VERSION,
        plan_id=plan_id,
        display_name=str(data.get("displayName") or metadata["display_name"]),
        distributor=metadata["distributor"],
        state=metadata["state"],
        effective_date=str(data.get("effectiveFrom") or ""),
        withdrawn_date=data.get("effectiveTo"),
        timezone_token=effective_timezone,
        supply_c_per_day=round(supply_c_per_day, 6),
        import_periods=tuple(import_periods),
        export_base_c_per_kwh=round(export_base, 6),
        free_import_start=free_start,
        free_import_end=free_end,
        free_import_cap_kwh=free_cap,
        premium_export_start=premium_start or "18:00",
        premium_export_end=premium_end or "21:00",
        premium_export_cap_kwh=premium_cap,
        premium_export_total_c_per_kwh=round(premium_total, 6),
        source_kind=COVAU_SOURCE_KIND,
        source_url=source_url,
        source_last_updated=data.get("lastUpdated"),
        content_hash=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def validate_manual_covau_snapshot(
    raw: dict[str, Any],
    *,
    timezone_token: str | None = None,
) -> CovaUPlanSnapshot:
    """Validate a user-entered stepped-tariff fallback without plan substitution."""
    periods = tuple(
        CovaURatePeriod(
            _normalize_start(item.get("start")),
            _normalize_manual_end(item.get("end")),
            _positive(item.get("c_per_kwh"), "import price"),
        )
        for item in raw.get("import_periods", [])
    )
    if not periods:
        raise ValueError("At least one stepped import period is required")
    _validate_full_day(periods)
    plan_id = str(raw.get("plan_id") or "manual_covau_solarmax").strip()
    state = str(raw.get("state") or "")
    effective_timezone = _local_timezone_token(timezone_token, state)
    canonical = json.dumps(
        {
            "source": raw,
            "timezone_token": effective_timezone,
            "parser_version": COVAU_PARSER_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return CovaUPlanSnapshot(
        schema_version=COVAU_SCHEMA_VERSION,
        parser_version=COVAU_PARSER_VERSION,
        plan_id=plan_id,
        display_name=str(raw.get("display_name") or plan_id),
        distributor=str(raw.get("distributor") or "Manual"),
        state=state,
        effective_date=str(raw.get("effective_date") or ""),
        withdrawn_date=None,
        timezone_token=effective_timezone,
        supply_c_per_day=_positive(raw.get("supply_c_per_day"), "supply charge"),
        import_periods=periods,
        export_base_c_per_kwh=_positive(
            raw.get("export_base_c_per_kwh"), "base export price", allow_zero=True
        ),
        free_import_start=_normalize_start(raw.get("free_import_start")),
        free_import_end=_normalize_manual_end(raw.get("free_import_end")),
        free_import_cap_kwh=_positive(raw.get("free_import_cap_kwh"), "import cap"),
        premium_export_start=_normalize_start(raw.get("premium_export_start")),
        premium_export_end=_normalize_manual_end(raw.get("premium_export_end")),
        premium_export_cap_kwh=_positive(raw.get("premium_export_cap_kwh"), "export cap"),
        premium_export_total_c_per_kwh=_positive(
            raw.get("premium_export_total_c_per_kwh"), "premium export price"
        ),
        source_kind="manual",
        source_url="",
        source_last_updated=None,
        content_hash=hashlib.sha256(canonical.encode()).hexdigest(),
        manual=True,
    )


def covau_quota_rules(snapshot: CovaUPlanSnapshot) -> tuple[QuotaRule, QuotaRule]:
    import_base = import_price_c_per_kwh(
        snapshot,
        _window_probe(snapshot.free_import_start, snapshot.timezone_token),
    )
    return (
        QuotaRule(
            rule_id=COVAU_IMPORT_RULE_ID,
            direction="import",
            timezone_token=snapshot.timezone_token,
            windows=((snapshot.free_import_start, snapshot.free_import_end),),
            daily_cap_kwh=snapshot.free_import_cap_kwh,
            base_price_c_per_kwh=import_base,
            bonus_price_c_per_kwh=import_base,
            settlement_source="pcc_import_energy",
        ),
        QuotaRule(
            rule_id=COVAU_EXPORT_RULE_ID,
            direction="export",
            timezone_token=snapshot.timezone_token,
            windows=((snapshot.premium_export_start, snapshot.premium_export_end),),
            daily_cap_kwh=snapshot.premium_export_cap_kwh,
            base_price_c_per_kwh=snapshot.export_base_c_per_kwh,
            bonus_price_c_per_kwh=max(
                0.0,
                snapshot.premium_export_total_c_per_kwh - snapshot.export_base_c_per_kwh,
            ),
            settlement_source="pcc_export_energy",
        ),
    )


def covau_price_series(
    snapshot: CovaUPlanSnapshot,
    timestamps: Iterable[datetime],
    ledger: QuotaLedger,
) -> tuple[list[float], list[float], list[float], list[float], float, float]:
    """Return base $/kWh prices, bonus deltas and remaining daily caps."""
    import_bucket = ledger.bucket(COVAU_IMPORT_RULE_ID)
    export_bucket = ledger.bucket(COVAU_EXPORT_RULE_ID)
    bonus_enabled = ledger.state.confidence != "unknown"
    current_day = ledger.state.tariff_day
    imports: list[float] = []
    exports: list[float] = []
    import_bonus: list[float] = []
    export_bonus: list[float] = []
    import_rule, export_rule = covau_quota_rules(snapshot)
    for timestamp in timestamps:
        base_import = import_price_c_per_kwh(snapshot, timestamp) / 100.0
        imports.append(base_import)
        exports.append(snapshot.export_base_c_per_kwh / 100.0)
        future_day = (
            current_day is not None
            and tariff_datetime(timestamp, snapshot.timezone_token).date().isoformat()
            > current_day
        )
        import_bonus.append(
            import_rule.bonus_price_c_per_kwh / 100.0
            if bonus_enabled
            and (future_day or import_bucket.remaining_kwh > 1e-9)
            and import_rule.contains(timestamp)
            else 0.0
        )
        export_bonus.append(
            export_rule.bonus_price_c_per_kwh / 100.0
            if bonus_enabled
            and (future_day or export_bucket.remaining_kwh > 1e-9)
            and export_rule.contains(timestamp)
            else 0.0
        )
    return (
        imports,
        exports,
        import_bonus,
        export_bonus,
        import_bucket.remaining_kwh if bonus_enabled else 0.0,
        export_bucket.remaining_kwh if bonus_enabled else 0.0,
    )


def import_price_c_per_kwh(snapshot: CovaUPlanSnapshot, timestamp: datetime) -> float:
    local = tariff_datetime(timestamp, snapshot.timezone_token)
    minute = local.hour * 60 + local.minute
    for period in snapshot.import_periods:
        if _minute_in_window(minute, period.start, period.end):
            return period.c_per_kwh
    raise ValueError(f"No CovaU import rate covers {local.isoformat()}")


def covau_tariff_schedule(
    snapshot: CovaUPlanSnapshot,
    ledger: QuotaLedger,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the live CovaU contract in the generic dashboard schedule shape."""
    now = now or datetime.now().astimezone()
    ledger.advance_to(now)
    local_now = tariff_datetime(now, snapshot.timezone_token)
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    timestamps = [day_start + timedelta(minutes=30 * slot) for slot in range(48)]
    (
        import_prices,
        export_prices,
        import_bonuses,
        export_bonuses,
        _,
        _,
    ) = covau_price_series(snapshot, timestamps, ledger)

    buy_prices: dict[str, float] = {}
    sell_prices: dict[str, float] = {}
    for timestamp, import_price, export_price, import_bonus, export_bonus in zip(
        timestamps,
        import_prices,
        export_prices,
        import_bonuses,
        export_bonuses,
        strict=True,
    ):
        period_key = f"PERIOD_{timestamp.hour:02d}_{timestamp.minute:02d}"
        buy_prices[period_key] = round(max(0.0, import_price - import_bonus), 6)
        sell_prices[period_key] = round(export_price + export_bonus, 6)

    import_remaining = ledger.remaining_kwh(COVAU_IMPORT_RULE_ID)
    export_remaining = ledger.remaining_kwh(COVAU_EXPORT_RULE_ID)
    unavailable_quota_windows = {}
    for direction, start, end, remaining in (
        ("import", snapshot.free_import_start, snapshot.free_import_end, import_remaining),
        ("export", snapshot.premium_export_start, snapshot.premium_export_end, export_remaining),
    ):
        if ledger.state.confidence == "unknown":
            reason = "unverified"
        elif remaining <= 1e-9:
            reason = "exhausted"
        else:
            continue
        unavailable_quota_windows[direction] = {
            "start": start,
            "end": end,
            "reason": reason,
        }
    refresh_key = ":".join(
        (
            str(ledger.state.tariff_day or local_now.date().isoformat()),
            snapshot.content_hash,
            ledger.state.confidence,
            f"{import_remaining:.4f}",
            f"{export_remaining:.4f}",
        )
    )
    return {
        "currency": "AUD",
        "buy_prices": buy_prices,
        "sell_prices": sell_prices,
        "utility": "CovaU",
        "plan_name": snapshot.display_name,
        "price_source": "covau_aer_cdr",
        "last_sync": refresh_key,
        "unavailable_quota_windows": unavailable_quota_windows,
    }


def covau_provider_contract(
    snapshot: CovaUPlanSnapshot,
    ledger: QuotaLedger,
    *,
    planned_import_kwh: float = 0.0,
    planned_export_kwh: float = 0.0,
    now: datetime | None = None,
    import_energy_entity: str | None = None,
    export_energy_entity: str | None = None,
) -> dict[str, Any]:
    now = now or datetime.now().astimezone()
    ledger.advance_to(now)
    import_bucket = ledger.bucket(COVAU_IMPORT_RULE_ID, planned_import_kwh)
    export_bucket = ledger.bucket(COVAU_EXPORT_RULE_ID, planned_export_kwh)
    import_rule, export_rule = covau_quota_rules(snapshot)
    import_base = import_price_c_per_kwh(snapshot, now)
    import_effective = (
        import_bucket.effective_price_c_per_kwh
        if import_rule.contains(now)
        else import_base
    )
    export_effective = (
        export_bucket.effective_price_c_per_kwh
        if export_rule.contains(now)
        else snapshot.export_base_c_per_kwh
    )
    import_period = (
        import_rule.rule_id
        if import_effective != import_base
        else COVAU_BASE_IMPORT_PERIOD_ID
    )
    export_period = (
        export_rule.rule_id
        if export_effective != snapshot.export_base_c_per_kwh
        else COVAU_BASE_EXPORT_PERIOD_ID
    )
    return {
        "schema_version": COVAU_SCHEMA_VERSION,
        "plan": {
            "plan_id": snapshot.plan_id,
            "display_name": snapshot.display_name,
            "distributor": snapshot.distributor,
            "effective_date": snapshot.effective_date,
            "timezone_token": snapshot.timezone_token,
            "source_kind": snapshot.source_kind,
            "source_url": snapshot.source_url,
            "source_last_updated": snapshot.source_last_updated,
            "parser_version": snapshot.parser_version,
            "content_hash": snapshot.content_hash,
            "manual": snapshot.manual,
        },
        "prices": {
            "import": {
                "c_per_kwh": round(import_effective, 4),
                "base_c_per_kwh": round(import_base, 4),
                "period": import_period,
            },
            "export": {
                "c_per_kwh": round(export_effective, 4),
                "base_c_per_kwh": round(snapshot.export_base_c_per_kwh, 4),
                "period": export_period,
            },
        },
        "tariff_schedule": covau_tariff_schedule(snapshot, ledger, now=now),
        "tariff_day": ledger.state.tariff_day,
        "settlement_confidence": ledger.state.confidence,
        "settlement_reason": ledger.state.reason,
        "quotas": {
            "import": {
                **_bucket_contract(import_bucket),
                "rule_id": import_rule.rule_id,
                "window_start": snapshot.free_import_start,
                "window_end": snapshot.free_import_end,
                "base_c_per_kwh": round(import_rule.base_price_c_per_kwh, 4),
                "bonus_c_per_kwh": round(import_rule.bonus_price_c_per_kwh, 4),
            },
            "export": {
                **_bucket_contract(export_bucket),
                "rule_id": export_rule.rule_id,
                "window_start": snapshot.premium_export_start,
                "window_end": snapshot.premium_export_end,
                "base_c_per_kwh": round(export_rule.base_price_c_per_kwh, 4),
                "bonus_c_per_kwh": round(export_rule.bonus_price_c_per_kwh, 4),
            },
        },
        "import_energy_entity": import_energy_entity,
        "export_energy_entity": export_energy_entity,
    }


class CovaUQuotaRuntime:
    """Entry-scoped measured settlement independent of Smart Optimization."""

    def __init__(
        self,
        hass: Any,
        entry: Any,
        snapshot: CovaUPlanSnapshot,
        *,
        grid_power_kw_getter: Callable[[], float | None],
        import_energy_entity: str | None = None,
        export_energy_entity: str | None = None,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.snapshot = snapshot
        self.grid_power_kw_getter = grid_power_kw_getter
        self.import_energy_entity = import_energy_entity or None
        self.export_energy_entity = export_energy_entity or None
        self.ledger = QuotaLedger(covau_quota_rules(snapshot))
        self.restored_from_store = False
        self._pending_settled = {"import": 0.0, "export": 0.0}
        self._store: Any = None
        self._unsubs: list[Callable[[], None]] = []
        self._lock = asyncio.Lock()

    async def async_start(self) -> None:
        """Restore state, subscribe to meters and begin continuity sampling."""
        from homeassistant.helpers.event import (
            async_track_state_change_event,
            async_track_time_interval,
        )
        from homeassistant.helpers.storage import Store

        self._store = Store(
            self.hass,
            2,
            f"power_sync.covau_quota.{self.entry.entry_id}",
        )
        raw = await self._store.async_load()
        restored_state = None
        if (
            isinstance(raw, dict)
            and raw.get("provider") == "covau"
            and raw.get("plan_content_hash") == self.snapshot.content_hash
            and isinstance(raw.get("quota_state_v2"), dict)
        ):
            restored_state = raw["quota_state_v2"]
        else:
            # One-time migration from the optimizer-owned cost store used by
            # the first quota implementation. The new runtime subsequently
            # owns settlement even when Smart Optimization is disabled.
            legacy = await Store(
                self.hass,
                1,
                f"power_sync.costs.{self.entry.entry_id}",
            ).async_load()
            legacy_quota = legacy.get("quota_state_v2") if isinstance(legacy, dict) else None
            if (
                isinstance(legacy_quota, dict)
                and legacy_quota.get("provider", "covau") == "covau"
                and legacy_quota.get(
                    "plan_content_hash", self.snapshot.content_hash
                )
                == self.snapshot.content_hash
            ):
                restored_state = legacy_quota

        if isinstance(restored_state, dict):
            self.ledger = QuotaLedger(
                covau_quota_rules(self.snapshot),
                QuotaLedgerState.from_dict(restored_state),
            )
            self.restored_from_store = True

        entities = [
            entity_id
            for entity_id in (
                self.import_energy_entity,
                self.export_energy_entity,
            )
            if entity_id
        ]
        if entities:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass,
                    entities,
                    self._async_meter_changed,
                )
            )
        self._unsubs.append(
            async_track_time_interval(
                self.hass,
                self._async_periodic_sample,
                timedelta(seconds=30),
            )
        )
        await self.async_sample()

    async def async_stop(self) -> None:
        while self._unsubs:
            self._unsubs.pop()()
        if self._store is not None:
            await self._store.async_save(self._state_to_save())

    async def _async_meter_changed(self, _event: Any) -> None:
        await self.async_sample()

    async def _async_periodic_sample(self, _now: datetime) -> None:
        await self.async_sample(now=_now)

    async def async_sample(self, *, now: datetime | None = None) -> dict[str, float]:
        """Settle the current measured sample exactly once per direction."""
        observed_at = _aware_utc(now or datetime.now(timezone.utc))
        async with self._lock:
            try:
                grid_power_kw = self.grid_power_kw_getter()
            except Exception:
                grid_power_kw = None
            settled: dict[str, float] = {"import": 0.0, "export": 0.0}
            for direction, entity_id in (
                ("import", self.import_energy_entity),
                ("export", self.export_energy_entity),
            ):
                if entity_id:
                    state = self.hass.states.get(entity_id)
                    total_kwh = _energy_state_kwh(state)
                    if total_kwh is None:
                        self.ledger.mark_unknown(
                            f"{direction} cumulative energy meter unavailable"
                        )
                        continue
                    meter_observed_at = observed_at
                    previous_total = self.ledger.state.last_meter_kwh.get(direction)
                    previous_at = _parse_iso_datetime(
                        self.ledger.state.last_sample_at.get(direction)
                    )
                    state_updated = _optional_aware_utc(
                        getattr(state, "last_updated", None)
                    )
                    if (
                        previous_total is not None
                        and abs(total_kwh - previous_total) > 1e-12
                        and previous_at is not None
                        and state_updated is not None
                        and state_updated > previous_at
                    ):
                        meter_observed_at = min(state_updated, observed_at)
                    # A successful read is a sample at `observed_at` even when
                    # the monotonic total has not numerically changed.
                    settled[direction] = self.ledger.observe_cumulative(
                        direction,
                        total_kwh,
                        meter_observed_at,
                    )
                    continue

                if grid_power_kw is None:
                    self.ledger.mark_unknown("PCC power telemetry unavailable")
                    continue
                direction_kw = (
                    max(0.0, grid_power_kw)
                    if direction == "import"
                    else max(0.0, -grid_power_kw)
                )
                settled[direction] = self.ledger.observe_power(
                    direction,
                    direction_kw * 1000.0,
                    observed_at,
                )

            for direction in ("import", "export"):
                self._pending_settled[direction] += settled[direction]
            if self._store is not None:
                self._store.async_delay_save(self._state_to_save, 5)
            return settled

    def consume_pending_settled(self) -> dict[str, float]:
        """Return measured eligible deltas since the last cost/planning read."""
        value = dict(self._pending_settled)
        self._pending_settled = {"import": 0.0, "export": 0.0}
        return value

    def adopt_legacy_state(self, state: QuotaLedgerState) -> bool:
        """Import the old optimizer-owned store only on first migration."""
        if self.restored_from_store:
            return False
        if any(self.ledger.state.last_sample_at.values()):
            return False
        self.ledger = QuotaLedger(covau_quota_rules(self.snapshot), state)
        return True

    def contract(
        self,
        *,
        planned_import_kwh: float = 0.0,
        planned_export_kwh: float = 0.0,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        return covau_provider_contract(
            self.snapshot,
            self.ledger,
            planned_import_kwh=planned_import_kwh,
            planned_export_kwh=planned_export_kwh,
            now=now,
            import_energy_entity=self.import_energy_entity,
            export_energy_entity=self.export_energy_entity,
        )

    def _state_to_save(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "provider": "covau",
            "plan_id": self.snapshot.plan_id,
            "plan_content_hash": self.snapshot.content_hash,
            "quota_state_v2": self.ledger.state.to_dict(),
        }


def _energy_state_kwh(state: Any) -> float | None:
    if state is None or str(getattr(state, "state", "")).lower() in {
        "",
        "none",
        "unknown",
        "unavailable",
    }:
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    unit = str(
        (getattr(state, "attributes", {}) or {}).get("unit_of_measurement") or "kWh"
    ).strip().lower()
    if unit == "wh":
        value /= 1000.0
    elif unit == "mwh":
        value *= 1000.0
    elif unit != "kwh":
        return None
    return max(0.0, value)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _optional_aware_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return _aware_utc(value)


def _parse_iso_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _aware_utc(value)
    if not value:
        return None
    try:
        return _aware_utc(datetime.fromisoformat(str(value)))
    except (TypeError, ValueError):
        return None


def _bucket_contract(bucket: MarginalBucket) -> dict[str, float]:
    return {
        "cap_kwh": round(bucket.cap_kwh, 4),
        "settled_kwh": round(bucket.settled_kwh, 4),
        "remaining_kwh": round(bucket.remaining_kwh, 4),
        "planned_kwh": round(bucket.planned_kwh, 4),
    }


def _gst_import_cents(value: Any) -> float:
    return round(float(value) * 100.0 * 1.1, 6)


def _export_cents(value: Any) -> float:
    return round(float(value) * 100.0, 6)


def _normalize_start(value: Any) -> str:
    text = str(value or "").strip()
    if not _valid_hhmm(text, allow_24=False):
        raise ValueError(f"Invalid tariff start time: {value}")
    return text


def _normalize_end(value: Any) -> str:
    text = str(value or "").strip()
    if not _valid_hhmm(text, allow_24=False):
        raise ValueError(f"Invalid tariff end time: {value}")
    hour, minute = (int(part) for part in text.split(":"))
    total = hour * 60 + minute + 1
    return "24:00" if total >= 1440 else f"{total // 60:02d}:{total % 60:02d}"


def _normalize_manual_end(value: Any) -> str:
    text = str(value or "").strip()
    if not _valid_hhmm(text, allow_24=True):
        raise ValueError(f"Invalid tariff end time: {value}")
    return text


def _valid_hhmm(value: str, *, allow_24: bool) -> bool:
    try:
        hour, minute = (int(part) for part in value.split(":"))
    except (TypeError, ValueError):
        return False
    return (0 <= hour <= (24 if allow_24 else 23)) and 0 <= minute < 60 and not (
        hour == 24 and minute != 0
    )


def _minute(value: str) -> int:
    hour, minute = (int(part) for part in value.split(":"))
    return hour * 60 + minute


def _minute_in_window(minute: int, start: str, end: str) -> bool:
    start_min = _minute(start)
    end_min = _minute(end)
    if end_min <= start_min:
        return minute >= start_min or minute < end_min
    return start_min <= minute < end_min


def _window_probe(start: str, timezone_token: str) -> datetime:
    hour, minute = (int(part) for part in start.split(":"))
    try:
        tariff_timezone = ZoneInfo(timezone_token)
    except ZoneInfoNotFoundError:
        tariff_timezone = timezone.utc
    return datetime(
        2026,
        1,
        1,
        hour,
        minute,
        tzinfo=tariff_timezone,
    )


def _local_timezone_token(token: Any, state: str) -> str:
    """Resolve CovaU wall-clock periods to an IANA customer timezone."""
    value = str(token or "").strip()
    if not value or value.upper() in {"AEST", "LOCAL"}:
        value = COVAU_STATE_TIMEZONES.get(state, "LOCAL")
    if value == "LOCAL":
        return value
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as err:
        raise ValueError(f"Invalid CovaU local timezone: {value}") from err
    return value


def _migrated_content_hash(content_hash: str, timezone_token: str) -> str:
    """Invalidate fixed-AEST settlement state when adopting local tariff time."""
    migration_key = (
        f"{content_hash}|parser:{COVAU_PARSER_VERSION}|timezone:{timezone_token}"
    )
    return hashlib.sha256(migration_key.encode()).hexdigest()


def _positive(value: Any, label: str, *, allow_zero: bool = False) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as err:
        raise ValueError(f"Invalid {label}") from err
    if parsed < 0 or (not allow_zero and parsed == 0):
        raise ValueError(f"Invalid {label}")
    return parsed


def _validate_full_day(periods: tuple[CovaURatePeriod, ...]) -> None:
    intervals = sorted((_minute(item.start), _minute(item.end)) for item in periods)
    if not intervals or intervals[0][0] != 0 or intervals[-1][1] != 1440:
        raise ValueError("Import periods must cover the complete tariff day")
    cursor = 0
    for start, end in intervals:
        if start != cursor or end <= start:
            raise ValueError("Import periods must be contiguous and non-overlapping")
        cursor = end


def _state_for_postcode(postcode: str | int | None) -> str | None:
    try:
        value = int(str(postcode).strip())
    except (TypeError, ValueError):
        return None
    if 2000 <= value <= 2999:
        return "NSW"
    if 4000 <= value <= 4999:
        return "QLD"
    if 5000 <= value <= 5999:
        return "SA"
    return None
