"""Shared Flow Power pricing source selection and PEA calculations."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any, Mapping

from .const import (
    CONF_FP_TWAP_OVERRIDE,
    FLOW_POWER_BENCHMARK,
    FLOW_POWER_GST,
    FLOW_POWER_MARKET_AVG,
)


@dataclass(frozen=True)
class FlowPowerPricingContext:
    """Effective account inputs used for Flow Power PEA pricing."""

    twap: float
    twap_source: str
    bpea: float
    bpea_source: str
    gst_multiplier: float
    gst_source: str
    account_data_active: bool


def _as_float(value: Any) -> float | None:
    """Return a finite float, or None when the value is not numeric."""
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _first_number(*values: Any) -> float | None:
    for value in values:
        parsed = _as_float(value)
        if parsed is not None:
            return parsed
    return None


def _tracker_twap(domain_data: Mapping[str, Any]) -> float | None:
    tracker = domain_data.get("flow_power_twap_tracker")
    return _as_float(getattr(tracker, "twap", None))


def _gst_multiplier(value: Any) -> float | None:
    parsed = _as_float(value)
    if parsed is None:
        return None
    if 0 < parsed < 1:
        return 1 + parsed
    if parsed > 2:
        return 1 + (parsed / 100)
    return parsed


def _account_data(domain_data: Mapping[str, Any]) -> Mapping[str, Any]:
    data = domain_data.get("flow_power_account_data")
    return data if isinstance(data, Mapping) else {}


def _preferred_account_bpea(account: Mapping[str, Any]) -> float | None:
    """Pick the most reliable API account BPEA value for import pricing."""
    bpea_import = _as_float(account.get("bpea_import"))
    bpea = _as_float(account.get("bpea"))

    if bpea_import is not None and bpea_import > 0:
        return bpea_import
    if bpea is not None:
        return bpea
    return bpea_import


def resolve_flow_power_pricing_context(
    options: Mapping[str, Any] | None,
    data: Mapping[str, Any] | None,
    domain_data: Mapping[str, Any] | None,
) -> FlowPowerPricingContext:
    """Resolve effective Flow Power PEA inputs.

    Priority for TWAP is:
      1. explicit PowerSync override,
      2. PowerSync rolling raw wholesale TWAP,
      3. hardcoded fallback.

    Flow Power account TWAP from Web Data API data is exposed on account
    sensors, but it is not used for import PEA pricing because it already
    includes account/network effects that the v2 formula models separately.
    """
    options = options or {}
    data = data or {}
    domain_data = domain_data or {}
    account = _account_data(domain_data)

    override = _first_number(
        options.get(CONF_FP_TWAP_OVERRIDE),
        data.get(CONF_FP_TWAP_OVERRIDE),
    )
    tracker_twap = _tracker_twap(domain_data)

    if override is not None:
        twap = override
        twap_source = "override"
    elif tracker_twap is not None:
        twap = tracker_twap
        twap_source = "dynamic"
    else:
        twap = FLOW_POWER_MARKET_AVG
        twap_source = "fallback"

    account_bpea = _preferred_account_bpea(account)
    if account_bpea is not None:
        bpea = account_bpea
        bpea_source = "api"
    else:
        bpea = FLOW_POWER_BENCHMARK
        bpea_source = "default"

    account_gst = _gst_multiplier(account.get("gst_multiplier"))
    if account_gst is not None:
        gst_multiplier = account_gst
        gst_source = "api"
    else:
        gst_multiplier = FLOW_POWER_GST
        gst_source = "default"

    return FlowPowerPricingContext(
        twap=twap,
        twap_source=twap_source,
        bpea=bpea,
        bpea_source=bpea_source,
        gst_multiplier=gst_multiplier,
        gst_source=gst_source,
        account_data_active=bool(account),
    )


def calculate_flow_power_pea(
    wholesale_cents: float,
    pricing: FlowPowerPricingContext,
    *,
    tariff_rate: float | None = None,
    avg_daily_tariff: float | None = None,
    custom_pea: float | None = None,
) -> float:
    """Calculate Flow Power PEA in c/kWh for one interval."""
    if custom_pea is not None:
        return custom_pea

    if tariff_rate is not None and avg_daily_tariff is not None:
        return (
            pricing.gst_multiplier * wholesale_cents
            + tariff_rate
            - pricing.gst_multiplier * pricing.twap
            - avg_daily_tariff
            - pricing.bpea
        )

    return wholesale_cents - pricing.twap - pricing.bpea
