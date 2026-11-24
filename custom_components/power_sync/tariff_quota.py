"""Quota metadata for editable custom electricity tariffs."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from .quota import QuotaLedger, QuotaRule

CUSTOM_TARIFF_IMPORT_RULE_ID = "custom_tariff_import_quota"


def custom_tariff_import_quota_rule(
    tariff: dict[str, Any] | None,
    *,
    default_timezone: str,
) -> QuotaRule | None:
    """Build a validated import quota rule from a custom tariff."""
    raw = tariff.get("import_quota") if isinstance(tariff, dict) else None
    if not isinstance(raw, dict) or raw.get("enabled", True) is False:
        return None

    try:
        daily_cap_kwh = float(raw.get("daily_cap_kwh"))
        base_price = float(raw.get("base_price_c_per_kwh"))
        bonus_price = float(raw.get("bonus_price_c_per_kwh", base_price))
    except (TypeError, ValueError):
        return None
    if daily_cap_kwh <= 0 or base_price < 0 or bonus_price <= 0:
        return None

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
            rule_id=CUSTOM_TARIFF_IMPORT_RULE_ID,
            direction="import",
            timezone_token=str(
                raw.get("timezone_token") or default_timezone or "LOCAL"
            ),
            windows=tuple(windows),
            daily_cap_kwh=daily_cap_kwh,
            base_price_c_per_kwh=base_price,
            bonus_price_c_per_kwh=min(base_price, bonus_price),
            settlement_source="site_energy_summary",
        )
        for start, end in rule.windows:
            datetime.strptime(start, "%H:%M")
            datetime.strptime(end, "%H:%M")
    except (TypeError, ValueError):
        return None
    return rule


def custom_tariff_import_cost(
    rule: QuotaRule,
    *,
    observed_at: datetime,
    import_kwh: float,
    eligible_kwh: float,
    tariff_import_price: float,
) -> float:
    """Return one interval's import cost ($) under a capped import allowance.

    Inside the window every imported kWh costs the window rate and the
    ledger-accepted kWh are discounted by the bonus. Outside the window the
    tariff's own rate for the slot applies; any eligible energy still pending
    from the window keeps its discount.
    """
    base_price = (
        rule.base_price_c_per_kwh / 100.0
        if rule.contains(observed_at)
        else tariff_import_price
    )
    return (
        max(0.0, import_kwh) * base_price
        - max(0.0, eligible_kwh) * (rule.bonus_price_c_per_kwh / 100.0)
    )


def custom_tariff_quota_hash(
    tariff: dict[str, Any],
    rule: QuotaRule,
) -> str:
    """Return an identity hash that invalidates settlement after plan edits."""
    payload = {
        "name": tariff.get("name"),
        "utility": tariff.get("utility"),
        "rule": {
            "timezone_token": rule.timezone_token,
            "windows": rule.windows,
            "daily_cap_kwh": rule.daily_cap_kwh,
            "base_price_c_per_kwh": rule.base_price_c_per_kwh,
            "bonus_price_c_per_kwh": rule.bonus_price_c_per_kwh,
        },
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def custom_tariff_quota_contract(
    tariff: dict[str, Any],
    rule: QuotaRule,
    ledger: QuotaLedger,
) -> dict[str, Any]:
    """Expose the custom tariff quota with the common provider contract shape."""
    bucket = ledger.bucket(CUSTOM_TARIFF_IMPORT_RULE_ID)
    return {
        "schema_version": 1,
        "plan": {
            "plan_id": tariff.get("template_id") or tariff.get("name"),
            "display_name": tariff.get("name"),
            "provider": tariff.get("utility"),
            "timezone_token": rule.timezone_token,
            "source_kind": "custom_tariff",
        },
        "prices": {
            "import": {
                "base_c_per_kwh": rule.base_price_c_per_kwh,
                "effective_c_per_kwh": bucket.effective_price_c_per_kwh,
            },
        },
        "quotas": {
            "import": {
                "rule_id": rule.rule_id,
                "cap_kwh": bucket.cap_kwh,
                "settled_kwh": bucket.settled_kwh,
                "remaining_kwh": bucket.remaining_kwh,
                "confidence": bucket.confidence,
                "windows": [list(window) for window in rule.windows],
            },
        },
    }
