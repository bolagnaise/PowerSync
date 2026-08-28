"""Flow Power residential plan contracts and quota-aware price series."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
import hashlib
import json
from typing import Any, Mapping, Sequence

from .const import (
    FLOW_POWER_HAPPY_HOUR_START,
    FLOW_POWER_PLAN_EFFECTIVE_FROM,
    FLOW_POWER_PLAN_IDS,
    FLOW_POWER_PLAN_REGIONS,
    FLOW_POWER_PLAN_SCHEMA_VERSION,
    resolve_flow_power_happy_hour_end,
)
from .quota import QuotaLedger, QuotaRule, tariff_datetime

LEGACY_PLAN_ID = "legacy_unclassified"
ACCOUNT_SPECIFIC_PLAN_ID = "account_specific"
OFFICIAL_PLAN_IDS = {"happy_hour_2026", "four_free_2026", "flow_home_2026"}

_PLAN_REGIONS = {
    "happy_hour_2026": ("NSW", "QLD", "SA", "VIC"),
    "four_free_2026": ("NSW", "SA", "SEQ", "VIC"),
    "flow_home_2026": ("NSW", "QLD", "SA", "SEQ", "VIC"),
    LEGACY_PLAN_ID: ("NSW", "QLD", "SA", "SEQ", "VIC"),
    ACCOUNT_SPECIFIC_PLAN_ID: ("NSW", "QLD", "SA", "SEQ", "VIC"),
}


@dataclass(frozen=True)
class FlowPowerPlanSelection:
    """Writable, versioned plan selection stored in the config entry."""

    schema_version: int = FLOW_POWER_PLAN_SCHEMA_VERSION
    plan_id: str = LEGACY_PLAN_ID
    region: str | None = None
    effective_from: str = FLOW_POWER_PLAN_EFFECTIVE_FROM
    overrides: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["overrides"] = dict(self.overrides)
        return result


@dataclass(frozen=True)
class FlowPowerPlanSnapshot:
    """Resolved immutable contract, including its preserved legacy fallback."""

    selection: FlowPowerPlanSelection
    timezone_token: str
    legacy_export_rate_dollars: float
    legacy_happy_hour_end: str
    plan_hash: str

    @property
    def plan_id(self) -> str:
        return self.selection.plan_id


@dataclass(frozen=True)
class FlowPowerPriceSeries:
    """Settlement prices plus bounded optimizer-only marginal bonuses."""

    settlement_import: tuple[float, ...]
    settlement_export: tuple[float, ...]
    import_bonus: tuple[float, ...]
    export_bonus: tuple[float, ...]
    import_group_ids: tuple[str | None, ...]
    export_group_ids: tuple[str | None, ...]
    import_group_caps_kwh: Mapping[str, float]
    export_group_caps_kwh: Mapping[str, float]
    active_plan_ids: tuple[str, ...]

    @property
    def marginal_import(self) -> tuple[float, ...]:
        return tuple(max(0.0, base - bonus) for base, bonus in zip(
            self.settlement_import, self.import_bonus, strict=True
        ))

    @property
    def marginal_export(self) -> tuple[float, ...]:
        return tuple(base + bonus for base, bonus in zip(
            self.settlement_export, self.export_bonus, strict=True
        ))


def flow_power_plan_catalog() -> list[dict[str, Any]]:
    """Return the HA-owned catalog consumed by config surfaces and mobile."""
    summaries = {
        LEGACY_PLAN_ID: "Uses the saved Happy Hour rate and end time exactly as configured.",
        ACCOUNT_SPECIFIC_PLAN_ID: "Uses your account-specific Happy Hour rate and end time.",
        "happy_hour_2026": (
            "17:30-21:30 export: first 15 kWh/day at 35c NSW/QLD/SA or "
            "30c VIC, then 10c; 0c outside."
        ),
        "four_free_2026": (
            "11:00-15:00 import credit capped separately at 8 kWh each hour; "
            "17:30-21:30 export first 15 kWh/day at 20c NSW/SA/SEQ or 17c VIC, "
            "then 5c or 2c; 0c outside."
        ),
        "flow_home_2026": "Uncapped 2c/kWh export credit all day.",
    }
    return [
        {
            "plan_id": plan_id,
            "label": label,
            "regions": [
                {"value": region, "label": FLOW_POWER_PLAN_REGIONS[region]}
                for region in _PLAN_REGIONS[plan_id]
            ],
            "effective_from": (
                FLOW_POWER_PLAN_EFFECTIVE_FROM if plan_id in OFFICIAL_PLAN_IDS else None
            ),
            "summary": summaries[plan_id],
        }
        for plan_id, label in FLOW_POWER_PLAN_IDS.items()
    ]


def validate_flow_power_plan_selection(raw: object | None) -> FlowPowerPlanSelection:
    """Validate a complete selection atomically; absence retains legacy behavior."""
    if raw is None:
        return FlowPowerPlanSelection()
    if not isinstance(raw, Mapping):
        raise ValueError("Flow Power plan must be an object")
    schema_version = int(raw.get("schema_version", FLOW_POWER_PLAN_SCHEMA_VERSION))
    if schema_version != FLOW_POWER_PLAN_SCHEMA_VERSION:
        raise ValueError("Unsupported Flow Power plan schema version")
    plan_id = str(raw.get("plan_id") or LEGACY_PLAN_ID)
    if plan_id not in FLOW_POWER_PLAN_IDS:
        raise ValueError("Unsupported Flow Power plan")
    region_value = raw.get("region")
    region = str(region_value).upper() if region_value not in (None, "") else None
    if plan_id in OFFICIAL_PLAN_IDS and region is None:
        raise ValueError("A plan region is required for official Flow Power plans")
    if region is not None and region not in _PLAN_REGIONS[plan_id]:
        raise ValueError(f"Region {region} is not available for {plan_id}")
    effective_from = str(raw.get("effective_from") or FLOW_POWER_PLAN_EFFECTIVE_FROM)
    try:
        date.fromisoformat(effective_from)
    except ValueError as err:
        raise ValueError("Flow Power effective_from must be an ISO date") from err
    overrides = raw.get("overrides") or {}
    if not isinstance(overrides, Mapping):
        raise ValueError("Flow Power plan overrides must be an object")
    return FlowPowerPlanSelection(
        schema_version=schema_version,
        plan_id=plan_id,
        region=region,
        effective_from=effective_from,
        overrides=dict(overrides),
    )


def resolve_flow_power_plan(
    raw: object | None,
    *,
    timezone_token: str,
    legacy_export_rate_dollars: float,
    legacy_happy_hour_end: object | None,
) -> FlowPowerPlanSnapshot:
    """Resolve stored selection without ever inferring a plan from the date."""
    selection = validate_flow_power_plan_selection(raw)
    legacy_end = resolve_flow_power_happy_hour_end(legacy_happy_hour_end)
    legacy_rate = max(0.0, float(legacy_export_rate_dollars or 0.0))
    digest = flow_power_plan_hash(
        selection,
        timezone_token=timezone_token,
        legacy_export_rate_dollars=legacy_rate,
        legacy_happy_hour_end=legacy_end,
    )
    return FlowPowerPlanSnapshot(
        selection=selection,
        timezone_token=timezone_token,
        legacy_export_rate_dollars=legacy_rate,
        legacy_happy_hour_end=legacy_end,
        plan_hash=digest,
    )


def flow_power_plan_hash(
    selection: FlowPowerPlanSelection,
    *,
    timezone_token: str,
    legacy_export_rate_dollars: float,
    legacy_happy_hour_end: str,
) -> str:
    payload = {
        "selection": selection.to_dict(),
        "timezone": timezone_token,
        "legacy_export_rate_dollars": round(float(legacy_export_rate_dollars), 8),
        "legacy_happy_hour_end": legacy_happy_hour_end,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()[:24]


def flow_power_quota_rules(snapshot: FlowPowerPlanSnapshot) -> tuple[QuotaRule, ...]:
    """Build settlement rules for the explicitly selected official plan."""
    plan_id = snapshot.plan_id
    region = snapshot.selection.region
    timezone_token = snapshot.timezone_token
    if plan_id == "happy_hour_2026":
        bonus = 20.0 if region == "VIC" else 25.0
        return (_rule("flow_happy_hour_export", "export", timezone_token,
                      (("17:30", "21:30"),), 15.0, 10.0, bonus),)
    if plan_id == "four_free_2026":
        export_base = 2.0 if region == "VIC" else 5.0
        rules = [
            _rule("flow_4free_export", "export", timezone_token,
                  (("17:30", "21:30"),), 15.0, export_base, 15.0)
        ]
        rules.extend(
            _rule(f"flow_4free_import_{hour}", "import", timezone_token,
                  ((f"{hour:02d}:00", f"{hour + 1:02d}:00"),), 8.0, 0.0, 0.0)
            for hour in range(11, 15)
        )
        return tuple(rules)
    return ()


def flow_power_price_series(
    snapshot: FlowPowerPlanSnapshot,
    timestamps: Sequence[datetime],
    import_prices: Sequence[float],
    *,
    ledger: QuotaLedger | None = None,
) -> FlowPowerPriceSeries:
    """Build side-effect-free base/bonus arrays in dollars per kWh."""
    if len(timestamps) != len(import_prices):
        raise ValueError("timestamps and import prices must have equal lengths")
    rules = {rule.rule_id: rule for rule in flow_power_quota_rules(snapshot)}
    settled_import: list[float] = []
    settled_export: list[float] = []
    import_bonus: list[float] = []
    export_bonus: list[float] = []
    import_groups: list[str | None] = []
    export_groups: list[str | None] = []
    import_caps: dict[str, float] = {}
    export_caps: dict[str, float] = {}
    active_ids: list[str] = []
    ledger_day = ledger.state.tariff_day if ledger is not None else None
    confidence = ledger.state.confidence if ledger is not None else "unknown"

    for timestamp, import_price in zip(timestamps, import_prices, strict=True):
        local = tariff_datetime(timestamp, snapshot.timezone_token)
        plan_id = _active_plan_id(snapshot, local)
        active_ids.append(plan_id)
        base_export, bonus_export, export_rule_id = _export_terms(
            snapshot, plan_id, local, rules
        )
        import_rule_id = _matching_import_rule(plan_id, local)
        base_import = max(0.0, float(import_price))
        i_bonus = 0.0
        i_group = None
        e_group = None

        if import_rule_id is not None:
            i_group = f"{local.date().isoformat()}:{import_rule_id}"
            cap, available = _group_cap(
                rules[import_rule_id], local.date().isoformat(), ledger, ledger_day, confidence
            )
            import_caps[i_group] = cap
            if available:
                i_bonus = base_import
        if export_rule_id is not None:
            e_group = f"{local.date().isoformat()}:{export_rule_id}"
            cap, available = _group_cap(
                rules[export_rule_id], local.date().isoformat(), ledger, ledger_day, confidence
            )
            export_caps[e_group] = cap
            if not available:
                bonus_export = 0.0

        settled_import.append(base_import)
        settled_export.append(base_export / 100.0)
        import_bonus.append(i_bonus)
        export_bonus.append(bonus_export / 100.0)
        import_groups.append(i_group)
        export_groups.append(e_group)

    return FlowPowerPriceSeries(
        settlement_import=tuple(settled_import),
        settlement_export=tuple(settled_export),
        import_bonus=tuple(import_bonus),
        export_bonus=tuple(export_bonus),
        import_group_ids=tuple(import_groups),
        export_group_ids=tuple(export_groups),
        import_group_caps_kwh=import_caps,
        export_group_caps_kwh=export_caps,
        active_plan_ids=tuple(active_ids),
    )


def flow_power_provider_contract(
    snapshot: FlowPowerPlanSnapshot,
    *,
    at: datetime,
    import_price: float,
    ledger: QuotaLedger | None = None,
    planned_kwh: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Return a mobile/sensor-safe view of current contractual prices and quotas."""
    series = flow_power_price_series(snapshot, [at], [import_price], ledger=ledger)
    planned_kwh = planned_kwh or {}
    quotas: list[dict[str, Any]] = []
    for rule in flow_power_quota_rules(snapshot):
        settled = 0.0
        confidence = "unknown"
        reason = "quota telemetry has not established a baseline"
        if ledger is not None:
            settled = min(rule.daily_cap_kwh, ledger.state.settled_kwh.get(rule.rule_id, 0.0))
            confidence = ledger.state.confidence
            reason = ledger.state.reason
        quotas.append({
            "rule_id": rule.rule_id,
            "direction": rule.direction,
            "windows": [list(window) for window in rule.windows],
            "cap_kwh": rule.daily_cap_kwh,
            "settled_kwh": settled,
            "remaining_kwh": max(0.0, rule.daily_cap_kwh - settled),
            "planned_kwh": max(0.0, float(planned_kwh.get(rule.rule_id, 0.0))),
            "confidence": confidence,
            "reason": reason,
        })
    return {
        "schema_version": FLOW_POWER_PLAN_SCHEMA_VERSION,
        "plan": snapshot.selection.to_dict(),
        "plan_hash": snapshot.plan_hash,
        "prices": {
            "unit": "dollars_per_kwh",
            "settlement": {
                "import": series.settlement_import[0],
                "export": series.settlement_export[0],
            },
            "marginal": {
                "import": series.marginal_import[0],
                "export": series.marginal_export[0],
            },
        },
        "quotas": quotas,
        "telemetry": {
            "settlement_source": "pcc_energy_or_integrated_grid_power",
            "last_observed_at": _last_observed_at(ledger),
        },
    }


def _rule(rule_id: str, direction: str, timezone_token: str,
          windows: tuple[tuple[str, str], ...], cap: float,
          base: float, bonus: float) -> QuotaRule:
    return QuotaRule(rule_id=rule_id, direction=direction, timezone_token=timezone_token,
                     windows=windows, daily_cap_kwh=cap,
                     base_price_c_per_kwh=base, bonus_price_c_per_kwh=bonus)


def _active_plan_id(snapshot: FlowPowerPlanSnapshot, local: datetime) -> str:
    if snapshot.plan_id not in OFFICIAL_PLAN_IDS:
        return snapshot.plan_id
    if local.date() < date.fromisoformat(snapshot.selection.effective_from):
        return LEGACY_PLAN_ID
    return snapshot.plan_id


def _export_terms(snapshot: FlowPowerPlanSnapshot, plan_id: str, local: datetime,
                  rules: Mapping[str, QuotaRule]) -> tuple[float, float, str | None]:
    minute = local.hour * 60 + local.minute
    if plan_id in {LEGACY_PLAN_ID, ACCOUNT_SPECIFIC_PLAN_ID}:
        if _inside(minute, FLOW_POWER_HAPPY_HOUR_START, snapshot.legacy_happy_hour_end):
            return snapshot.legacy_export_rate_dollars * 100.0, 0.0, None
        return 0.0, 0.0, None
    if plan_id == "flow_home_2026":
        return 2.0, 0.0, None
    rule_id = "flow_happy_hour_export" if plan_id == "happy_hour_2026" else "flow_4free_export"
    rule = rules[rule_id]
    if rule.contains(local):
        return rule.base_price_c_per_kwh, rule.bonus_price_c_per_kwh, rule_id
    return 0.0, 0.0, None


def _matching_import_rule(plan_id: str, local: datetime) -> str | None:
    if plan_id != "four_free_2026":
        return None
    if 11 <= local.hour < 15:
        return f"flow_4free_import_{local.hour}"
    return None


def _group_cap(rule: QuotaRule, tariff_day: str, ledger: QuotaLedger | None,
               ledger_day: str | None, confidence: str) -> tuple[float, bool]:
    if ledger_day is not None and tariff_day > ledger_day:
        return rule.daily_cap_kwh, True
    if ledger is None or tariff_day != ledger_day or confidence == "unknown":
        return 0.0, False
    remaining = ledger.remaining_kwh(rule.rule_id)
    return remaining, remaining > 1e-9


def _inside(minute: int, start: str, end: str) -> bool:
    def parsed(value: str) -> int:
        hour, mins = (int(part) for part in value.split(":"))
        return hour * 60 + mins
    return parsed(start) <= minute < parsed(end)


def _last_observed_at(ledger: QuotaLedger | None) -> str | None:
    if ledger is None:
        return None
    values = [value for value in ledger.state.last_sample_at.values() if value]
    return max(values) if values else None
