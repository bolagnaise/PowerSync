"""Quota metadata for editable custom electricity tariffs.

A custom tariff can carry two daily allowances with the same shape:

* ``import_quota``: the first ``daily_cap_kwh`` imported inside the window
  each tariff day costs ``base - bonus`` (``bonus`` is a discount, capped at
  the base so the allowance is never negative).
* ``export_quota``: the first ``daily_cap_kwh`` exported inside the window
  each tariff day earns ``base + bonus`` (``bonus`` is a premium above the
  window rate that applies after the allowance).

Both rules settle in one :class:`QuotaLedger`, so they share one tariff day
and one telemetry confidence.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from datetime import datetime
from typing import Any

from .quota import Direction, QuotaLedger, QuotaRule, tariff_datetime

CUSTOM_TARIFF_IMPORT_RULE_ID = "custom_tariff_import_quota"
CUSTOM_TARIFF_EXPORT_RULE_ID = "custom_tariff_export_quota"


@dataclass(frozen=True)
class CustomTariffQuotaRules:
    """The validated allowance rules of one custom tariff."""

    import_rule: QuotaRule | None = None
    export_rule: QuotaRule | None = None

    @property
    def rules(self) -> tuple[QuotaRule, ...]:
        """Ledger rules, import first so an import-only ledger is unchanged."""
        return tuple(
            rule for rule in (self.import_rule, self.export_rule) if rule is not None
        )

    @property
    def timezone_token(self) -> str:
        return self.rules[0].timezone_token

    def rule(self, direction: Direction) -> QuotaRule | None:
        return self.import_rule if direction == "import" else self.export_rule


def _quota_rule(
    raw: Any,
    *,
    rule_id: str,
    direction: Direction,
    default_timezone: str,
) -> QuotaRule | None:
    if not isinstance(raw, dict) or raw.get("enabled", True) is False:
        return None

    try:
        daily_cap_kwh = float(raw.get("daily_cap_kwh"))
        base_price = float(raw.get("base_price_c_per_kwh"))
        if direction == "import":
            bonus_price = float(raw.get("bonus_price_c_per_kwh", base_price))
        else:
            # A premium has no natural default; an export allowance must
            # state how much more its first kWh earn.
            bonus_price = float(raw.get("bonus_price_c_per_kwh"))
    except (TypeError, ValueError):
        return None
    if daily_cap_kwh <= 0 or base_price < 0 or bonus_price <= 0:
        return None
    if direction == "import":
        bonus_price = min(base_price, bonus_price)

    windows: list[tuple[str, str]] = []
    raw_windows = raw.get("windows")
    if isinstance(raw_windows, list):
        for window in raw_windows:
            if (
                isinstance(window, (list, tuple))
                and len(window) == 2
                and all(isinstance(value, str) for value in window)
            ):
                windows.append((window[0], window[1]))
    if not windows:
        start = raw.get("start_time")
        end = raw.get("end_time")
        if isinstance(start, str) and isinstance(end, str):
            windows.append((start, end))
    if not windows:
        return None

    # QuotaRule validates time strings when it evaluates a window. Resolve
    # them now so malformed mobile/API payloads fail closed at configuration
    # discovery rather than during an optimizer run.
    try:
        rule = QuotaRule(
            rule_id=rule_id,
            direction=direction,
            timezone_token=str(
                raw.get("timezone_token") or default_timezone or "LOCAL"
            ),
            windows=tuple(windows),
            daily_cap_kwh=daily_cap_kwh,
            base_price_c_per_kwh=base_price,
            bonus_price_c_per_kwh=bonus_price,
            settlement_source="site_energy_summary",
        )
        for start, end in rule.windows:
            datetime.strptime(start, "%H:%M")
            datetime.strptime(end, "%H:%M")
    except (TypeError, ValueError):
        return None
    return rule


def custom_tariff_import_quota_rule(
    tariff: dict[str, Any] | None,
    *,
    default_timezone: str,
) -> QuotaRule | None:
    """Build a validated import quota rule from a custom tariff."""
    return _quota_rule(
        tariff.get("import_quota") if isinstance(tariff, dict) else None,
        rule_id=CUSTOM_TARIFF_IMPORT_RULE_ID,
        direction="import",
        default_timezone=default_timezone,
    )


def custom_tariff_export_quota_rule(
    tariff: dict[str, Any] | None,
    *,
    default_timezone: str,
) -> QuotaRule | None:
    """Build a validated premium export quota rule from a custom tariff."""
    return _quota_rule(
        tariff.get("export_quota") if isinstance(tariff, dict) else None,
        rule_id=CUSTOM_TARIFF_EXPORT_RULE_ID,
        direction="export",
        default_timezone=default_timezone,
    )


def custom_tariff_quota_rules(
    tariff: dict[str, Any] | None,
    *,
    default_timezone: str,
) -> CustomTariffQuotaRules | None:
    """Return the tariff's allowance rules, or ``None`` when it has none.

    One ledger keeps one tariff clock. An export allowance on a different
    clock from the import allowance is rejected rather than settled on the
    wrong day boundary.
    """
    import_rule = custom_tariff_import_quota_rule(
        tariff,
        default_timezone=default_timezone,
    )
    export_rule = custom_tariff_export_quota_rule(
        tariff,
        default_timezone=default_timezone,
    )
    if (
        import_rule is not None
        and export_rule is not None
        and export_rule.timezone_token != import_rule.timezone_token
    ):
        export_rule = None
    if import_rule is None and export_rule is None:
        return None
    return CustomTariffQuotaRules(import_rule=import_rule, export_rule=export_rule)


def custom_tariff_quota_bonus_series(
    rule: QuotaRule,
    ledger: QuotaLedger,
    timestamps: list[datetime],
) -> tuple[list[float], list[str | None], dict[str, float]]:
    """Return LP bonus prices ($/kWh), tariff-day groups and per-day caps.

    Today's cap is the ledger remainder; a later tariff day in the horizon
    gets the full daily allowance. Slots outside the window, or on a day with
    nothing left, carry no bonus and no group.
    """
    current_day = ledger.state.tariff_day
    bonus_price = rule.bonus_price_c_per_kwh / 100.0
    bonuses = [0.0] * len(timestamps)
    groups: list[str | None] = []
    caps: dict[str, float] = {}
    for idx, timestamp in enumerate(timestamps):
        if not rule.contains(timestamp):
            groups.append(None)
            continue
        day = tariff_datetime(timestamp, rule.timezone_token).date().isoformat()
        remaining_kwh = (
            ledger.remaining_kwh(rule.rule_id)
            if day == current_day
            else rule.daily_cap_kwh
        )
        if remaining_kwh <= 1e-9:
            groups.append(None)
            continue
        groups.append(day)
        bonuses[idx] = bonus_price
        caps[day] = remaining_kwh
    return bonuses, groups, caps


def custom_tariff_export_earnings(
    rule: QuotaRule,
    *,
    observed_at: datetime,
    export_kwh: float,
    eligible_kwh: float,
    tariff_export_price: float,
) -> float:
    """Return one interval's export earnings ($) under a premium allowance.

    Inside the window every exported kWh earns the window rate and the
    ledger-accepted kWh also earn the premium, so energy beyond the cap earns
    only the window rate. Outside the window the tariff rate applies; any
    eligible energy still pending from the window keeps its premium.
    """
    base_price = (
        rule.base_price_c_per_kwh / 100.0
        if rule.contains(observed_at)
        else tariff_export_price
    )
    return (
        max(0.0, export_kwh) * base_price
        + max(0.0, eligible_kwh) * (rule.bonus_price_c_per_kwh / 100.0)
    )


def _rule_identity(rule: QuotaRule) -> dict[str, Any]:
    return {
        "timezone_token": rule.timezone_token,
        "windows": rule.windows,
        "daily_cap_kwh": rule.daily_cap_kwh,
        "base_price_c_per_kwh": rule.base_price_c_per_kwh,
        "bonus_price_c_per_kwh": rule.bonus_price_c_per_kwh,
    }


def custom_tariff_quota_hash(
    tariff: dict[str, Any],
    rule: QuotaRule | None,
    export_rule: QuotaRule | None = None,
) -> str:
    """Return an identity hash that invalidates settlement after plan edits.

    An import-only tariff hashes exactly as it did before export allowances
    existed, so a saved ledger still restores after an upgrade.
    """
    payload: dict[str, Any] = {
        "name": tariff.get("name"),
        "utility": tariff.get("utility"),
        "rule": _rule_identity(rule) if rule is not None else None,
    }
    if export_rule is not None:
        payload["export_rule"] = _rule_identity(export_rule)
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def custom_tariff_quota_contract(
    tariff: dict[str, Any],
    rules: CustomTariffQuotaRules,
    ledger: QuotaLedger,
) -> dict[str, Any]:
    """Expose the custom tariff quotas with the common provider contract shape."""
    prices: dict[str, Any] = {}
    quotas: dict[str, Any] = {}
    for direction in ("import", "export"):
        rule = rules.rule(direction)
        if rule is None:
            continue
        bucket = ledger.bucket(rule.rule_id)
        prices[direction] = {
            "base_c_per_kwh": rule.base_price_c_per_kwh,
            "effective_c_per_kwh": bucket.effective_price_c_per_kwh,
        }
        quotas[direction] = {
            "rule_id": rule.rule_id,
            "cap_kwh": bucket.cap_kwh,
            "settled_kwh": bucket.settled_kwh,
            "remaining_kwh": bucket.remaining_kwh,
            "confidence": bucket.confidence,
            "windows": [list(window) for window in rule.windows],
        }
        if direction == "export":
            quotas[direction]["bonus_c_per_kwh"] = rule.bonus_price_c_per_kwh
    return {
        "schema_version": 1,
        "plan": {
            "plan_id": tariff.get("template_id") or tariff.get("name"),
            "display_name": tariff.get("name"),
            "provider": tariff.get("utility"),
            "timezone_token": rules.timezone_token,
            "source_kind": "custom_tariff",
        },
        "prices": prices,
        "quotas": quotas,
    }
