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
    portal_active: bool


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


def _portal_data(domain_data: Mapping[str, Any]) -> Mapping[str, Any]:
    data = domain_data.get("flow_power_portal_data")
    return data if isinstance(data, Mapping) else {}


def _preferred_portal_bpea(portal: Mapping[str, Any]) -> float | None:
    """Pick the most reliable portal/API BPEA value for import pricing."""
    bpea_import = _as_float(portal.get("bpea_import"))
    bpea = _as_float(portal.get("bpea"))

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
      2. Flow Power account import TWAP from KWatch/portal data,
      3. PowerSync rolling wholesale TWAP,
      4. hardcoded fallback.

    Flow Power's live account price uses account-level import TWAP when it is
    available. The local rolling TWAP remains a fallback for AEMO/direct setups
    that do not have KWatch or portal account data.
    """
    options = options or {}
    data = data or {}
    domain_data = domain_data or {}
    portal = _portal_data(domain_data)

    override = _first_number(
        options.get(CONF_FP_TWAP_OVERRIDE),
        data.get(CONF_FP_TWAP_OVERRIDE),
    )
    portal_twap = _first_number(portal.get("twap_import"), portal.get("twap"))
    tracker_twap = _tracker_twap(domain_data)

    if override is not None:
        twap = override
        twap_source = "override"
    elif portal_twap is not None:
        twap = portal_twap
        twap_source = "portal"
    elif tracker_twap is not None:
        twap = tracker_twap
        twap_source = "dynamic"
    else:
        twap = FLOW_POWER_MARKET_AVG
        twap_source = "fallback"

    portal_bpea = _preferred_portal_bpea(portal)
    if portal_bpea is not None:
        bpea = portal_bpea
        bpea_source = "portal"
    else:
        bpea = FLOW_POWER_BENCHMARK
        bpea_source = "default"

    portal_gst = _gst_multiplier(portal.get("gst_multiplier"))
    if portal_gst is not None:
        gst_multiplier = portal_gst
        gst_source = "portal"
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
        portal_active=bool(portal),
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
